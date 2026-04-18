"""Code execution backends for the privy RelayServer.

Two strategies:

* ``run_subprocess``  — spawns a fresh ``bash -lc`` or ``python -c``; truly
  stateless, works for ``kind="bash"`` and ``kind="python"``. This is the
  default and the only option that can run shell commands (``pip install`` etc).
* ``run_inprocess_python`` — executes inside the current interpreter via
  ``exec()`` with redirected stdout/stderr. Shares globals across calls so
  Fabric notebook objects (e.g. ``spark``) are visible. Python only.
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import os
import subprocess
import sys
import threading
import time
import traceback
from typing import Any

from privy.protocol import ExecRequest, ExecResponse

# A single globals dict shared across all inprocess invocations; mirrors how
# users already think about a long-lived Fabric notebook kernel.
_INPROCESS_GLOBALS: dict[str, Any] = {"__name__": "__privy_inprocess__"}
_INPROCESS_LOCK = threading.Lock()


def execute(req: ExecRequest) -> ExecResponse:
    """Dispatch an ExecRequest to the right backend and return an ExecResponse."""
    start = time.monotonic()
    try:
        if req.mode == "inprocess":
            if req.kind != "python":
                return ExecResponse.from_output(
                    exit_code=2,
                    stdout=b"",
                    stderr=b"inprocess mode is only valid for kind='python'\n",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    error="invalid_mode",
                )
            return _run_inprocess_python(req.code, req.timeout_s, start)
        return _run_subprocess(req.kind, req.code, req.timeout_s, start)
    except Exception as exc:  # pragma: no cover - safety net
        return ExecResponse.from_output(
            exit_code=1,
            stdout=b"",
            stderr=("executor error: " + traceback.format_exc()).encode("utf-8", "replace"),
            duration_ms=int((time.monotonic() - start) * 1000),
            error=type(exc).__name__,
        )


def _run_subprocess(kind: str, code: str, timeout_s: float, start: float) -> ExecResponse:
    if kind == "python":
        argv = [sys.executable, "-u", "-c", code]
    elif kind == "bash":
        argv = ["bash", "-lc", code]
    else:  # pragma: no cover - guarded by protocol
        raise ValueError(f"invalid kind: {kind!r}")

    env = dict(os.environ)
    # Force unbuffered text so partial output is not lost on timeout.
    env.setdefault("PYTHONUNBUFFERED", "1")

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        return ExecResponse.from_output(
            exit_code=127,
            stdout=b"",
            stderr=f"{exc}\n".encode(),
            duration_ms=int((time.monotonic() - start) * 1000),
            error="not_found",
        )

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            stdout, stderr = b"", b""

    return ExecResponse.from_output(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout or b"",
        stderr=stderr or b"",
        duration_ms=int((time.monotonic() - start) * 1000),
        timed_out=timed_out,
        error="timeout" if timed_out else None,
    )


def _run_inprocess_python(code: str, timeout_s: float, start: float) -> ExecResponse:
    """Run ``code`` inside this interpreter, capturing stdout/stderr.

    A worker thread does the ``exec``; the caller waits up to ``timeout_s``.
    On timeout we attempt to raise ``KeyboardInterrupt`` into the worker via
    ``PyThreadState_SetAsyncExc``. This is best-effort (won't interrupt a
    blocking C call) but good enough for typical user code.
    """
    stdout_buf = io.BytesIO()
    stderr_buf = io.BytesIO()
    stdout_text = io.TextIOWrapper(stdout_buf, encoding="utf-8", write_through=True)
    stderr_text = io.TextIOWrapper(stderr_buf, encoding="utf-8", write_through=True)

    result: dict[str, Any] = {"exit_code": 0, "error": None}

    def target() -> None:
        with _INPROCESS_LOCK:
            try:
                with contextlib.redirect_stdout(stdout_text), contextlib.redirect_stderr(stderr_text):
                    try:
                        compiled = compile(code, "<privy-inprocess>", "exec")
                        exec(compiled, _INPROCESS_GLOBALS)
                    except SystemExit as exc:
                        code_val = exc.code
                        result["exit_code"] = (
                            int(code_val) if isinstance(code_val, int) else (0 if code_val is None else 1)
                        )
                    except BaseException:  # noqa: BLE001 — capture user errors
                        traceback.print_exc()
                        result["exit_code"] = 1
                        result["error"] = "exception"
            finally:
                stdout_text.flush()
                stderr_text.flush()

    worker = threading.Thread(target=target, name="privy-inprocess", daemon=True)
    worker.start()
    worker.join(timeout=timeout_s)

    timed_out = worker.is_alive()
    if timed_out:
        _try_async_raise(worker, KeyboardInterrupt)
        worker.join(timeout=5)
        result["error"] = "timeout"
        result["exit_code"] = 1

    return ExecResponse.from_output(
        exit_code=result["exit_code"],
        stdout=stdout_buf.getvalue(),
        stderr=stderr_buf.getvalue(),
        duration_ms=int((time.monotonic() - start) * 1000),
        timed_out=timed_out,
        error=result["error"],
    )


def _try_async_raise(thread: threading.Thread, exc_type: type[BaseException]) -> None:
    tid = thread.ident
    if tid is None:
        return
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), ctypes.py_object(exc_type))
    if res > 1:  # pragma: no cover — undo if we hit the wrong thread
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
