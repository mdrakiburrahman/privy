"""Code execution backends for the privy RelayServer.

Two strategies:

* ``run_subprocess``  — spawns a fresh ``bash -lc`` or ``python -c``; truly
  stateless, works for ``kind="bash"`` and ``kind="python"``. This is the
  default and the only option that can run shell commands (``pip install`` etc).
* ``run_inprocess_python`` — executes inside the current interpreter via
  ``exec()``. Shares globals across calls so Fabric notebook objects (e.g.
  ``spark``) are visible. Python only.

Either strategy can also be run **asynchronously** as a *job*
(``action="submit"`` + ``action="poll"``): the request returns a ``job_id``
immediately and the work continues in the background. This is what makes work
that runs longer than Azure Relay's ~60s per-request response deadline possible
— see :func:`submit_job` / :func:`poll_job`.

Output capture is per-thread (:class:`_StreamRouter`) rather than a global
``contextlib.redirect_stdout``, so concurrent executions neither serialize
behind one another nor steal each other's stdout.
"""

from __future__ import annotations

import ctypes
import io
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from privy.protocol import DEFAULT_POLL_WAIT_S, MAX_POLL_WAIT_S, ExecRequest, ExecResponse

# A single globals dict shared across all inprocess invocations; mirrors how
# users already think about a long-lived Fabric notebook kernel.
_INPROCESS_GLOBALS: dict[str, Any] = {"__name__": "__privy_inprocess__"}
_INPROCESS_LOCK = threading.Lock()

#: Historically every inprocess ``exec`` held :data:`_INPROCESS_LOCK` for its
#: whole duration, so two callers could never run Python at the same time. That
#: was only needed because stdout/stderr were captured with the process-global
#: ``contextlib.redirect_stdout``. Capture is now per-thread, so executions run
#: concurrently by default. Set ``PRIVY_SERIALIZE_INPROCESS=1`` to restore the
#: old one-at-a-time behaviour.
_SERIALIZE_INPROCESS = os.environ.get("PRIVY_SERIALIZE_INPROCESS", "").strip().lower() in (
    "1",
    "true",
    "yes",
)


def seed_inprocess_globals(mapping: dict[str, Any]) -> None:
    """Merge ``mapping`` into the shared inprocess globals.

    Lets the host notebook (e.g. a Fabric cell) expose its own live objects —
    most commonly ``spark``/``sc`` — to code later submitted with
    ``mode="inprocess"``. Safe to call repeatedly (e.g. on notebook restart).
    """
    with _INPROCESS_LOCK:
        _INPROCESS_GLOBALS.update(mapping)


def execute(req: ExecRequest) -> ExecResponse:
    """Dispatch an ExecRequest to the right backend and return an ExecResponse."""
    start = time.monotonic()
    try:
        if req.action == "submit":
            if req.mode == "inprocess" and req.kind != "python":
                return _invalid_mode(start)
            return submit_job(req)
        if req.action == "poll":
            return poll_job(req.job_id or "", req.wait_s)
        if req.action == "cancel":
            return cancel_job(req.job_id or "")
        if req.mode == "inprocess":
            if req.kind != "python":
                return _invalid_mode(start)
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


def _invalid_mode(start: float) -> ExecResponse:
    return ExecResponse.from_output(
        exit_code=2,
        stdout=b"",
        stderr=b"inprocess mode is only valid for kind='python'\n",
        duration_ms=int((time.monotonic() - start) * 1000),
        error="invalid_mode",
    )


def _run_subprocess(
    kind: str,
    code: str,
    timeout_s: float,
    start: float,
    on_proc: Callable[[subprocess.Popen], None] | None = None,
) -> ExecResponse:
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

    if on_proc is not None:
        on_proc(proc)

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


class _StreamRouter:
    """A ``sys.stdout``/``sys.stderr`` stand-in that routes writes per thread.

    Threads that registered a buffer get their own output; everything else
    (notebook internals, Spark's own logging threads, …) still reaches the real
    stream. This replaces ``contextlib.redirect_stdout``, which is
    process-global and therefore forced every execution to run under one lock.
    """

    def __init__(self, original: Any) -> None:
        self._original = original
        self._buffers: dict[int, Any] = {}
        self._lock = threading.Lock()

    def register(self, buf: Any) -> None:
        with self._lock:
            self._buffers[threading.get_ident()] = buf

    def unregister(self) -> None:
        with self._lock:
            self._buffers.pop(threading.get_ident(), None)

    def _target(self) -> Any:
        return self._buffers.get(threading.get_ident(), self._original)

    def write(self, data: str) -> int:
        return self._target().write(data)

    def writelines(self, lines: Any) -> None:
        target = self._target()
        for line in lines:
            target.write(line)

    def flush(self) -> None:
        try:
            self._target().flush()
        except Exception:  # pragma: no cover - a closed buffer must not kill user code
            pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._original.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", "utf-8")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


_ROUTERS: dict[str, _StreamRouter] = {}
_ROUTER_LOCK = threading.Lock()


def _ensure_routers() -> tuple[_StreamRouter, _StreamRouter]:
    """Install the stdout/stderr routers, re-installing if they were replaced.

    Hosts legitimately swap ``sys.stdout`` out from under us — a Fabric
    notebook does it per cell, pytest does it per test. Re-wrapping whatever is
    current (while carrying over live registrations) keeps capture correct
    instead of silently leaking user output to the console.
    """
    with _ROUTER_LOCK:
        for name in ("stdout", "stderr"):
            current = getattr(sys, name)
            router = _ROUTERS.get(name)
            if router is current:
                continue
            new_router = _StreamRouter(current)
            if router is not None:
                new_router._buffers.update(router._buffers)
            _ROUTERS[name] = new_router
            setattr(sys, name, new_router)
        return _ROUTERS["stdout"], _ROUTERS["stderr"]


class _InprocessRun:
    """One ``exec`` of user code on its own thread, with per-thread capture."""

    def __init__(self, code: str) -> None:
        self._code = code
        self._stdout_buf = io.BytesIO()
        self._stderr_buf = io.BytesIO()
        self._stdout_text = io.TextIOWrapper(self._stdout_buf, encoding="utf-8", write_through=True)
        self._stderr_text = io.TextIOWrapper(self._stderr_buf, encoding="utf-8", write_through=True)
        self.exit_code = 0
        self.error: str | None = None
        self.thread = threading.Thread(target=self._target, name="privy-inprocess", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float | None) -> bool:
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()

    def interrupt(self) -> None:
        _try_async_raise(self.thread, KeyboardInterrupt)

    def output(self) -> tuple[bytes, bytes]:
        return self._stdout_buf.getvalue(), self._stderr_buf.getvalue()

    def _target(self) -> None:
        out_router, err_router = _ensure_routers()
        lock = _INPROCESS_LOCK if _SERIALIZE_INPROCESS else _NULL_LOCK
        with lock:
            out_router.register(self._stdout_text)
            err_router.register(self._stderr_text)
            try:
                try:
                    compiled = compile(self._code, "<privy-inprocess>", "exec")
                    exec(compiled, _INPROCESS_GLOBALS)
                except SystemExit as exc:
                    code_val = exc.code
                    self.exit_code = (
                        int(code_val) if isinstance(code_val, int) else (0 if code_val is None else 1)
                    )
                except BaseException:  # noqa: BLE001 — capture user errors
                    traceback.print_exc(file=self._stderr_text)
                    self.exit_code = 1
                    self.error = "exception"
            finally:
                self._stdout_text.flush()
                self._stderr_text.flush()
                out_router.unregister()
                err_router.unregister()


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return False


_NULL_LOCK = _NullLock()


def _run_inprocess_python(
    code: str,
    timeout_s: float,
    start: float,
    on_run: Callable[[_InprocessRun], None] | None = None,
) -> ExecResponse:
    """Run ``code`` inside this interpreter, capturing stdout/stderr.

    A worker thread does the ``exec``; the caller waits up to ``timeout_s``.
    On timeout we attempt to raise ``KeyboardInterrupt`` into the worker via
    ``PyThreadState_SetAsyncExc``. This is best-effort (won't interrupt a
    blocking C call) but good enough for typical user code.

    ``on_run`` receives the :class:`_InprocessRun` as soon as it starts so an
    async job can keep a handle on it for cancellation.
    """
    run = _InprocessRun(code)
    run.start()
    if on_run is not None:
        on_run(run)

    finished = run.join(timeout=timeout_s)
    timed_out = not finished
    if timed_out:
        run.interrupt()
        run.join(timeout=5)
        run.error = "timeout"
        run.exit_code = 1

    stdout, stderr = run.output()
    return ExecResponse.from_output(
        exit_code=run.exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((time.monotonic() - start) * 1000),
        timed_out=timed_out,
        error=run.error,
    )


def _try_async_raise(thread: threading.Thread, exc_type: type[BaseException]) -> None:
    tid = thread.ident
    if tid is None:
        return
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), ctypes.py_object(exc_type))
    if res > 1:  # pragma: no cover — undo if we hit the wrong thread
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)


# ---------------------------------------------------------------------------
# Async jobs
#
# Azure Relay expects a listener to answer a request within ~60s; anything
# slower fails the *transport* with a 504 even though the work is fine. Jobs
# decouple the two: ``submit`` starts the work and returns a handle in
# milliseconds, and each ``poll`` is its own short request. Polls block
# server-side (long-poll) until the job finishes or ``wait_s`` elapses, so the
# client learns about completion within milliseconds while still making very
# few relay round-trips.
# ---------------------------------------------------------------------------


class _Job:
    def __init__(self, req: ExecRequest) -> None:
        self.id = uuid.uuid4().hex
        self.request = req
        self.done = threading.Event()
        self.response: ExecResponse | None = None
        self.cancelled = False
        self.created_at = time.monotonic()
        self.finished_at: float | None = None
        self._run: _InprocessRun | None = None
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._target, name=f"privy-job-{self.id[:8]}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _target(self) -> None:
        start = time.monotonic()
        try:
            if self.request.mode == "inprocess":
                resp = _run_inprocess_python(
                    self.request.code,
                    self.request.timeout_s,
                    start,
                    on_run=self._adopt_run,
                )
            else:
                resp = _run_subprocess(
                    self.request.kind,
                    self.request.code,
                    self.request.timeout_s,
                    start,
                    on_proc=self._adopt_proc,
                )
        except Exception as exc:  # noqa: BLE001 - safety net
            resp = ExecResponse.from_output(
                exit_code=1,
                stdout=b"",
                stderr=("job error: " + traceback.format_exc()).encode("utf-8", "replace"),
                duration_ms=int((time.monotonic() - start) * 1000),
                error=type(exc).__name__,
            )
        self.response = resp
        self.finished_at = time.monotonic()
        self.done.set()

    def _adopt_run(self, run: _InprocessRun) -> None:
        self._run = run

    def _adopt_proc(self, proc: subprocess.Popen) -> None:
        self._proc = proc

    def cancel(self) -> None:
        self.cancelled = True
        if self._run is not None:
            self._run.interrupt()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:  # pragma: no cover
                pass


_JOBS: dict[str, _Job] = {}
_JOBS_LOCK = threading.Lock()

#: How long a finished job's result is retained after the last poll could have
#: read it. Generous: a client that briefly loses the relay can still collect.
_JOB_RETENTION_S = float(os.environ.get("PRIVY_JOB_RETENTION_S", "3600"))


def _reap_jobs() -> None:
    now = time.monotonic()
    with _JOBS_LOCK:
        stale = [
            jid
            for jid, job in _JOBS.items()
            if job.finished_at is not None and (now - job.finished_at) > _JOB_RETENTION_S
        ]
        for jid in stale:
            _JOBS.pop(jid, None)


def submit_job(req: ExecRequest) -> ExecResponse:
    """Start ``req`` in the background and answer immediately with its id."""
    _reap_jobs()
    job = _Job(req)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    job.start()
    return ExecResponse.from_output(
        exit_code=0,
        stdout=b"",
        stderr=b"",
        duration_ms=0,
        job_id=job.id,
        state="running",
    )


def poll_job(job_id: str, wait_s: float = DEFAULT_POLL_WAIT_S) -> ExecResponse:
    """Wait up to ``wait_s`` for a job, then report its state (and output)."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return ExecResponse.from_output(
            exit_code=1,
            stdout=b"",
            stderr=f"unknown job: {job_id}\n".encode(),
            duration_ms=0,
            error="unknown_job",
            job_id=job_id,
            state="missing",
        )

    job.done.wait(timeout=max(0.0, min(wait_s, MAX_POLL_WAIT_S)))
    if not job.done.is_set():
        return ExecResponse.from_output(
            exit_code=0,
            stdout=b"",
            stderr=b"",
            duration_ms=int((time.monotonic() - job.created_at) * 1000),
            job_id=job_id,
            state="running",
        )

    resp = job.response
    assert resp is not None  # set before ``done``
    resp.job_id = job_id
    resp.state = "cancelled" if job.cancelled else "done"
    return resp


def cancel_job(job_id: str) -> ExecResponse:
    """Best-effort interrupt of a running job; always forgets the handle."""
    with _JOBS_LOCK:
        job = _JOBS.pop(job_id, None)
    if job is None:
        return ExecResponse.from_output(
            exit_code=1,
            stdout=b"",
            stderr=f"unknown job: {job_id}\n".encode(),
            duration_ms=0,
            error="unknown_job",
            job_id=job_id,
            state="missing",
        )
    job.cancel()
    return ExecResponse.from_output(
        exit_code=0,
        stdout=b"",
        stderr=b"",
        duration_ms=0,
        job_id=job_id,
        state="cancelled",
    )
