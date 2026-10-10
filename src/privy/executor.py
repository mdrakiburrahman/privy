"""Code execution backends for the privy RelayServer.

Two strategies:

* ``run_subprocess``  — spawns a fresh Bash, PowerShell, or Python process;
  truly stateless and the only option that can run shell commands
  (``pip install`` etc).
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
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from privy._relay import RELAY_SECRET_ENV_VARS
from privy.protocol import (
    DEFAULT_POLL_WAIT_S,
    MAX_POLL_WAIT_S,
    MAX_STREAM_CHUNK_BYTES,
    ExecRequest,
    ExecResponse,
    b64encode_bytes,
)

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

_POWERSHELL_STDIN_BOOTSTRAP = (
    "$utf8=[Text.UTF8Encoding]::new($false);"
    "[Console]::OutputEncoding=$utf8;"
    "$OutputEncoding=$utf8;"
    "$stream=[Console]::OpenStandardInput();"
    "$memory=[IO.MemoryStream]::new();"
    "$stream.CopyTo($memory);"
    "$source=[Text.Encoding]::UTF8.GetString($memory.ToArray());"
    "$source += [Environment]::NewLine + "
    "'$privySuccess=$?;$privyExitCode=$LASTEXITCODE;"
    "if (-not $privySuccess) {"
    "if ($null -ne $privyExitCode -and $privyExitCode -ne 0) { exit $privyExitCode };"
    "exit 1"
    "}';"
    "& ([ScriptBlock]::Create($source))"
)
_WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_CLOSE = 0x00002000
_WINDOWS_PROCESS_SET_QUOTA = 0x0100
_WINDOWS_PROCESS_TERMINATE = 0x0001
_PROCESS_JOB_LOCK = threading.Lock()


class _WindowsIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _WindowsBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _WindowsExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WindowsBasicLimitInformation),
        ("IoInfo", _WindowsIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _windows_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.TerminateJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def _attach_process_tree(proc: subprocess.Popen) -> None:
    if os.name != "nt":
        return
    kernel32 = _windows_kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    info = _WindowsExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_CLOSE
    try:
        if not kernel32.SetInformationJobObject(
            job,
            _WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        process = kernel32.OpenProcess(
            _WINDOWS_PROCESS_SET_QUOTA | _WINDOWS_PROCESS_TERMINATE,
            False,
            proc.pid,
        )
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(job, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        kernel32.CloseHandle(job)
        raise

    with _PROCESS_JOB_LOCK:
        proc._privy_windows_job = job


def _take_windows_job(proc: subprocess.Popen):
    if os.name != "nt":
        return None
    with _PROCESS_JOB_LOCK:
        job = getattr(proc, "_privy_windows_job", None)
        proc._privy_windows_job = None
    return job


def _release_process_tree(proc: subprocess.Popen) -> None:
    job = _take_windows_job(proc)
    if job:
        _windows_kernel32().CloseHandle(job)


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    job = _take_windows_job(proc)
    if job:
        kernel32 = _windows_kernel32()
        kernel32.TerminateJobObject(job, 1)
        kernel32.CloseHandle(job)
    elif proc.poll() is None:
        proc.kill()


def seed_inprocess_globals(mapping: dict[str, Any]) -> None:
    """Merge ``mapping`` into the shared inprocess globals.

    Lets the host notebook (e.g. a Fabric cell) expose its own live objects —
    most commonly ``spark``/``sc`` — to code later submitted with
    ``mode="inprocess"``. Safe to call repeatedly (e.g. on notebook restart).
    """
    with _INPROCESS_LOCK:
        _INPROCESS_GLOBALS.update(mapping)


# ---------------------------------------------------------------------------
# Incremental output: per-stream append-only buffers + optional listener echo
# ---------------------------------------------------------------------------


class _OffsetError(Exception):
    """A poll asked for bytes that were already freed or do not exist yet."""


class _OutputBuffer:
    """Append-only byte buffer for one output stream of one run.

    Offsets are absolute (bytes since the run started). ``trim`` frees the
    bytes the client has acknowledged, so a streamed hour of output does not
    accumulate in memory. All buffers of a job share one condition so a
    long-poll wakes as soon as *either* stream grows or the job ends.
    """

    def __init__(self, cond: threading.Condition, stream: str, echo_id: str | None = None) -> None:
        self._cond = cond
        self._stream = stream
        self._echo_id = echo_id
        self._data = bytearray()
        self._base = 0

    @property
    def end(self) -> int:
        return self._base + len(self._data)

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._cond:
            self._data += data
            self._cond.notify_all()
        if self._echo_id is not None:
            _echo_chunk(self._echo_id, self._stream, data)

    def close(self) -> None:
        if self._echo_id is not None:
            _echo_chunk(self._echo_id, self._stream, None)

    def read(self, offset: int, max_bytes: int) -> bytes:
        """Bytes in ``[offset, offset+max_bytes)``. Caller must hold the condition."""
        if offset < self._base:
            raise _OffsetError(f"offset {offset} was already acknowledged (oldest kept: {self._base})")
        if offset > self.end:
            raise _OffsetError(f"offset {offset} is beyond the output produced so far ({self.end})")
        start = offset - self._base
        return bytes(self._data[start : start + max_bytes])

    def trim(self, offset: int) -> None:
        """Free bytes below ``offset``. Caller must hold the condition."""
        drop = min(offset, self.end) - self._base
        if drop > 0:
            del self._data[:drop]
            self._base += drop

    def snapshot(self) -> bytes:
        with self._cond:
            return bytes(self._data)


class _BufferTextWriter:
    """Minimal text file object that appends UTF-8 to an :class:`_OutputBuffer`."""

    def __init__(self, buf: _OutputBuffer) -> None:
        self._buf = buf

    def write(self, data: str) -> int:
        # Same newline translation the TextIOWrapper this replaced applied.
        text = data.replace("\n", os.linesep) if os.linesep != "\n" else data
        self._buf.append(text.encode("utf-8", "replace"))
        return len(data)

    def flush(self) -> None:
        pass


_ECHO_ENABLED = False
_ECHO_QUEUE: queue.Queue = queue.Queue(maxsize=4096)
_ECHO_LOCK = threading.Lock()
_ECHO_THREAD: threading.Thread | None = None
_ECHO_DROPPED = 0
_ECHO_MAX_PARTIAL_LINE = 64 * 1024


def set_echo_output(enabled: bool) -> None:
    """Copy each run's output to this process's stdout/stderr, line-prefixed by job id."""
    global _ECHO_ENABLED, _ECHO_THREAD
    with _ECHO_LOCK:
        _ECHO_ENABLED = bool(enabled)
        if enabled and _ECHO_THREAD is None:
            _ECHO_THREAD = threading.Thread(target=_echo_loop, name="privy-echo", daemon=True)
            _ECHO_THREAD.start()


def _echo_chunk(echo_id: str, stream: str, data: bytes | None) -> None:
    """Queue ``data`` for echo; never blocks (a slow log sink drops chunks instead)."""
    global _ECHO_DROPPED
    if not _ECHO_ENABLED:
        return
    try:
        _ECHO_QUEUE.put_nowait((echo_id, stream, data))
    except queue.Full:
        _ECHO_DROPPED += 1


def _echo_write(stream: str, echo_id: str, lines: list[bytes]) -> None:
    target = sys.stderr if stream == "stderr" else sys.stdout
    prefix = f"[{echo_id}] "
    text = "".join(prefix + line.decode("utf-8", "replace") + "\n" for line in lines)
    try:
        target.write(text)
        target.flush()
    except Exception:  # noqa: BLE001 - a broken log sink must never affect jobs
        pass


def _echo_loop() -> None:
    global _ECHO_DROPPED
    partial: dict[tuple[str, str], bytes] = {}
    reported = 0
    while True:
        echo_id, stream, data = _ECHO_QUEUE.get()
        key = (echo_id, stream)
        if data is None:
            rest = partial.pop(key, b"")
            if rest:
                _echo_write(stream, echo_id, [rest])
        else:
            pending = partial.get(key, b"") + data
            *lines, rest = pending.split(b"\n")
            if len(rest) > _ECHO_MAX_PARTIAL_LINE:
                lines.append(rest)
                rest = b""
            if lines:
                _echo_write(stream, echo_id, lines)
            if rest:
                partial[key] = rest
            else:
                partial.pop(key, None)
        dropped = _ECHO_DROPPED
        if dropped != reported:
            _echo_write(
                "stderr",
                echo_id,
                [f"... {dropped - reported} output chunk(s) dropped (log sink too slow)".encode()],
            )
            reported = dropped


def execute(req: ExecRequest) -> ExecResponse:
    """Dispatch an ExecRequest to the right backend and return an ExecResponse."""
    start = time.monotonic()
    try:
        if req.action == "submit":
            if req.mode == "inprocess" and req.kind != "python":
                return _invalid_mode(start)
            return submit_job(req)
        if req.action == "poll":
            return poll_job(req.job_id or "", req.wait_s, req.stdout_offset, req.stderr_offset)
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


def _python_executable() -> str:
    """Interpreter to use for ``mode="subprocess"``.

    In the PyInstaller binary ``sys.executable`` is the binary itself, so a real
    Python from PATH is required; otherwise only bash and ``mode="inprocess"``
    work on that box.
    """
    if getattr(sys, "frozen", False):
        found = shutil.which("python3") or shutil.which("python")
        if not found:
            raise FileNotFoundError(
                "no python3 on PATH — the privy binary cannot run mode='subprocess' "
                "python; use mode='inprocess' or kind='bash'"
            )
        return found
    return sys.executable


def _bash_executable() -> str:
    if os.name == "nt":
        roots = []
        configured_root = os.environ.get("GIT_INSTALL_ROOT")
        if configured_root:
            roots.append(configured_root)

        git = shutil.which("git")
        if git:
            git_parent = os.path.dirname(git)
            if os.path.basename(git_parent).lower() in ("bin", "cmd"):
                roots.append(os.path.dirname(git_parent))

        program_files = os.environ.get("ProgramFiles")
        if program_files:
            roots.append(os.path.join(program_files, "Git"))

        for root in dict.fromkeys(roots):
            for relative_path in (("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
                candidate = os.path.join(root, *relative_path)
                if os.path.isfile(candidate):
                    return candidate

    found = shutil.which("bash")
    if not found:
        raise FileNotFoundError("Bash is not installed or is not on PATH")
    return found


def _powershell_executable() -> str:
    found = shutil.which("pwsh") or shutil.which("powershell") or shutil.which("powershell.exe")
    if not found:
        raise FileNotFoundError("PowerShell is not installed or is not on PATH")
    return found


#: Variables PyInstaller rewrites for its own bundled libraries. Leaking them
#: into a child makes system binaries (e.g. `az` → system python3) load privy's
#: bundled libpython/libssl and segfault. PyInstaller stashes the pre-launch
#: value in ``<VAR>_ORIG``, so restore that when present and drop it otherwise.
_PYI_LEAKED_VARS = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "LIBPATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

_EXECUTION_SECRET_ENV_VARS = (
    *RELAY_SECRET_ENV_VARS,
    "BASE64_ENV",
    "STORAGE_KEY",
)


def _child_env() -> dict[str, str]:
    """Environment for subprocesses, scrubbed of PyInstaller's runtime tweaks."""
    env = dict(os.environ)
    for var in _EXECUTION_SECRET_ENV_VARS:
        env.pop(var, None)
    if getattr(sys, "frozen", False):
        for var in _PYI_LEAKED_VARS:
            original = env.pop(f"{var}_ORIG", None)
            if original:
                env[var] = original
            else:
                env.pop(var, None)
        env.pop("_MEIPASS2", None)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and env.get("PATH"):
            env["PATH"] = os.pathsep.join(
                p for p in env["PATH"].split(os.pathsep) if p and not p.startswith(meipass)
            )
    return env


def _run_subprocess(
    kind: str,
    code: str,
    timeout_s: float,
    start: float,
    on_proc: Callable[[subprocess.Popen], None] | None = None,
    out: _OutputBuffer | None = None,
    err: _OutputBuffer | None = None,
) -> ExecResponse:
    """Run a subprocess, pumping its output into buffers as it is produced.

    With ``out``/``err`` supplied (async jobs) the response carries no output —
    the caller owns the buffers. Otherwise private buffers are used and the
    full output is returned in the response.
    """
    collect = out is None or err is None
    if collect:
        cond = threading.Condition()
        echo_id = uuid.uuid4().hex[:8] if _ECHO_ENABLED else None
        out = _OutputBuffer(cond, "stdout", echo_id)
        err = _OutputBuffer(cond, "stderr", echo_id)
    assert out is not None and err is not None
    env = _child_env()
    # Force unbuffered text so partial output is not lost on timeout.
    env.setdefault("PYTHONUNBUFFERED", "1")

    proc: subprocess.Popen | None = None
    stdin_payload: bytes | None = None
    try:
        if kind == "python":
            argv = [_python_executable(), "-u", "-c", code]
        elif kind == "bash":
            argv = [_bash_executable(), "-lc", code]
        elif kind == "powershell":
            argv = [
                _powershell_executable(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _POWERSHELL_STDIN_BOOTSTRAP,
            ]
            stdin_payload = code.encode("utf-8")
        else:  # pragma: no cover - guarded by protocol
            raise ValueError(f"invalid kind: {kind!r}")

        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
            env=env,
            close_fds=True,
        )
        _attach_process_tree(proc)
    except FileNotFoundError as exc:
        return ExecResponse.from_output(
            exit_code=127,
            stdout=b"",
            stderr=f"{exc}\n".encode(),
            duration_ms=int((time.monotonic() - start) * 1000),
            error="not_found",
        )
    except OSError as exc:
        if proc is not None:
            _terminate_process_tree(proc)
        return ExecResponse.from_output(
            exit_code=126,
            stdout=b"",
            stderr=f"{exc}\n".encode(),
            duration_ms=int((time.monotonic() - start) * 1000),
            error="process_setup",
        )

    if on_proc is not None:
        on_proc(proc)

    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, out), name="privy-stdout", daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, err), name="privy-stderr", daemon=True),
    ]
    if stdin_payload is not None:
        threads.append(
            threading.Thread(target=_feed_stdin, args=(proc, stdin_payload), name="privy-stdin", daemon=True)
        )
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        # Readers hit EOF as soon as the process exits; a background child that
        # inherited the pipes must not hold the result hostage.
        for thread in threads:
            thread.join(timeout=2.0)
    finally:
        _release_process_tree(proc)
        out.close()
        err.close()

    return ExecResponse.from_output(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=out.snapshot() if collect else b"",
        stderr=err.snapshot() if collect else b"",
        duration_ms=int((time.monotonic() - start) * 1000),
        timed_out=timed_out,
        error="timeout" if timed_out else None,
    )


def _pump(stream: Any, buf: _OutputBuffer) -> None:
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            buf.append(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:  # pragma: no cover
            pass


def _feed_stdin(proc: subprocess.Popen, payload: bytes) -> None:
    try:
        assert proc.stdin is not None
        proc.stdin.write(payload)
        proc.stdin.close()
    except (OSError, ValueError):
        pass


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

    def __init__(
        self,
        code: str,
        out: _OutputBuffer | None = None,
        err: _OutputBuffer | None = None,
    ) -> None:
        self._code = code
        if out is None or err is None:
            cond = threading.Condition()
            echo_id = uuid.uuid4().hex[:8] if _ECHO_ENABLED else None
            out = _OutputBuffer(cond, "stdout", echo_id)
            err = _OutputBuffer(cond, "stderr", echo_id)
        self.out = out
        self.err = err
        self._stdout_text = _BufferTextWriter(out)
        self._stderr_text = _BufferTextWriter(err)
        self.exit_code = 0
        self.error: str | None = None
        self._state_lock = threading.Lock()
        self._cancel_requested = False
        self.thread = threading.Thread(target=self._target, name="privy-inprocess", daemon=True)

    def start(self) -> bool:
        with self._state_lock:
            if self._cancel_requested:
                return False
            self.thread.start()
            return True

    def join(self, timeout: float | None) -> bool:
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()

    def interrupt(self) -> None:
        with self._state_lock:
            self._cancel_requested = True
            started = self.thread.ident is not None
        if started:
            _try_async_raise(self.thread, KeyboardInterrupt)

    def output(self) -> tuple[bytes, bytes]:
        return self.out.snapshot(), self.err.snapshot()

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
                out_router.unregister()
                err_router.unregister()
                self.out.close()
                self.err.close()


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
    on_run: Callable[[_InprocessRun], bool] | None = None,
    out: _OutputBuffer | None = None,
    err: _OutputBuffer | None = None,
) -> ExecResponse:
    """Run ``code`` inside this interpreter, capturing stdout/stderr.

    A worker thread does the ``exec``; the caller waits up to ``timeout_s``.
    On timeout we attempt to raise ``KeyboardInterrupt`` into the worker via
    ``PyThreadState_SetAsyncExc``. This is best-effort (won't interrupt a
    blocking C call) but good enough for typical user code.

    ``on_run`` receives the :class:`_InprocessRun` as soon as it starts so an
    async job can keep a handle on it for cancellation.
    """
    collect = out is None or err is None
    run = _InprocessRun(code, out, err)
    if on_run is not None and not on_run(run):
        return ExecResponse.from_output(
            exit_code=130,
            stdout=b"",
            stderr=b"",
            duration_ms=int((time.monotonic() - start) * 1000),
            error="cancelled",
        )
    if not run.start():
        return ExecResponse.from_output(
            exit_code=130,
            stdout=b"",
            stderr=b"",
            duration_ms=int((time.monotonic() - start) * 1000),
            error="cancelled",
        )

    finished = run.join(timeout=timeout_s)
    timed_out = not finished
    if timed_out:
        run.interrupt()
        run.join(timeout=5)
        run.error = "timeout"
        run.exit_code = 1

    stdout, stderr = run.output() if collect else (b"", b"")
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
        self.done: threading.Event | None = threading.Event()
        self.response: ExecResponse | None = None
        self.cancelled = False
        self.created_at = time.monotonic()
        self.finished_at: float | None = None
        #: Shared wake-up for long-polls: signalled on new output and on completion.
        self.cond = threading.Condition()
        self.streaming = req.stream
        echo_id = self.id[:8] if _ECHO_ENABLED else None
        self.out = _OutputBuffer(self.cond, "stdout", echo_id)
        self.err = _OutputBuffer(self.cond, "stderr", echo_id)
        self._run: _InprocessRun | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = threading.Thread(
            target=self._target,
            name=f"privy-job-{self.id[:8]}",
            daemon=True,
        )

    def start(self) -> None:
        assert self._thread is not None
        self._thread.start()

    def _target(self) -> None:
        start = time.monotonic()
        with self._lock:
            cancelled = self.cancelled
        if cancelled:
            resp = ExecResponse.from_output(
                exit_code=130,
                stdout=b"",
                stderr=b"",
                duration_ms=0,
                error="cancelled",
            )
        else:
            try:
                if self.request.mode == "inprocess":
                    resp = _run_inprocess_python(
                        self.request.code,
                        self.request.timeout_s,
                        start,
                        on_run=self._adopt_run,
                        out=self.out,
                        err=self.err,
                    )
                else:
                    resp = _run_subprocess(
                        self.request.kind,
                        self.request.code,
                        self.request.timeout_s,
                        start,
                        on_proc=self._adopt_proc,
                        out=self.out,
                        err=self.err,
                    )
            except Exception as exc:  # noqa: BLE001 - safety net
                resp = ExecResponse.from_output(
                    exit_code=1,
                    stdout=b"",
                    stderr=("job error: " + traceback.format_exc()).encode("utf-8", "replace"),
                    duration_ms=int((time.monotonic() - start) * 1000),
                    error=type(exc).__name__,
                )

        self.out.close()
        self.err.close()
        if not self.streaming:
            # Legacy shape: the final response carries the complete output.
            resp.stdout_b64 = b64encode_bytes(self.out.snapshot() + resp.stdout)
            resp.stderr_b64 = b64encode_bytes(self.err.snapshot() + resp.stderr)
        with self._lock:
            self.response = resp
            self.finished_at = time.monotonic()
            self._run = None
            self._proc = None
            self._thread = None
            done = self.done
            self.done = None
        with self.cond:
            self.cond.notify_all()
        if done is not None:
            done.set()
        _reap_jobs()

    def _adopt_run(self, run: _InprocessRun) -> bool:
        with self._lock:
            self._run = run
            return not self.cancelled

    def _adopt_proc(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._proc = proc
            cancelled = self.cancelled
        if cancelled:
            _terminate_process_tree(proc)

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            run = self._run
            proc = self._proc
        if run is not None:
            run.interrupt()
        if proc is not None:
            try:
                _terminate_process_tree(proc)
            except Exception:  # pragma: no cover
                pass


_JOBS: dict[str, _Job] = {}
_JOBS_LOCK = threading.Lock()

#: How long a finished job's result is retained after the last poll could have
#: read it. Generous: a client that briefly loses the relay can still collect.
_JOB_RETENTION_S = float(os.environ.get("PRIVY_JOB_RETENTION_S", "3600"))
_MAX_RETAINED_JOB_RESULTS = max(1, int(os.environ.get("PRIVY_MAX_RETAINED_JOB_RESULTS", "1024")))


def _reap_jobs() -> None:
    now = time.monotonic()
    with _JOBS_LOCK:
        stale = {
            jid
            for jid, job in _JOBS.items()
            if job.finished_at is not None and (now - job.finished_at) > _JOB_RETENTION_S
        }
        completed = sorted(
            (
                (job.finished_at, jid)
                for jid, job in _JOBS.items()
                if job.finished_at is not None and jid not in stale
            ),
            key=lambda item: item[0],
        )
        overflow = max(0, len(completed) - _MAX_RETAINED_JOB_RESULTS)
        stale.update(jid for _, jid in completed[:overflow])
        for jid in stale:
            _JOBS.pop(jid, None)


def submit_job(req: ExecRequest) -> ExecResponse:
    """Start ``req`` in the background and answer immediately with its id."""
    _reap_jobs()
    job = _Job(req)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    job.start()
    resp = ExecResponse.from_output(
        exit_code=0,
        stdout=b"",
        stderr=b"",
        duration_ms=0,
        job_id=job.id,
        state="running",
    )
    if req.stream:
        resp.stream = True
    return resp


def poll_job(
    job_id: str,
    wait_s: float = DEFAULT_POLL_WAIT_S,
    stdout_offset: int | None = None,
    stderr_offset: int | None = None,
) -> ExecResponse:
    """Wait up to ``wait_s`` for a job, then report its state (and output).

    For streaming jobs the offsets are the client's watermark: the response
    carries the next chunk of output after them (plus the new offsets) and the
    job is reported ``done`` only once every byte has been handed over.
    """
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

    if job.streaming:
        return _poll_streaming(job, wait_s, stdout_offset or 0, stderr_offset or 0)

    done = job.done
    if done is not None:
        done.wait(timeout=max(0.0, min(wait_s, MAX_POLL_WAIT_S)))
    if job.response is None:
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


def _poll_streaming(job: _Job, wait_s: float, so: int, se: int) -> ExecResponse:
    deadline = time.monotonic() + max(0.0, min(wait_s, MAX_POLL_WAIT_S))
    half = MAX_STREAM_CHUNK_BYTES // 2
    with job.cond:
        try:
            job.out.trim(so)
            job.err.trim(se)
            while True:
                finished = job.response is not None
                if job.out.end > so or job.err.end > se or finished:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                job.cond.wait(timeout=remaining)
            out_chunk = job.out.read(so, half)
            err_chunk = job.err.read(se, half)
        except _OffsetError as exc:
            return ExecResponse.from_output(
                exit_code=1,
                stdout=b"",
                stderr=f"{exc}\n".encode(),
                duration_ms=0,
                error="bad_offset",
                job_id=job.id,
                state="done",
            )
        so += len(out_chunk)
        se += len(err_chunk)
        drained = job.out.end <= so and job.err.end <= se

    final = job.response
    if final is not None and drained:
        resp = ExecResponse.from_output(
            exit_code=final.exit_code,
            stdout=out_chunk,
            stderr=err_chunk,
            duration_ms=final.duration_ms,
            timed_out=final.timed_out,
            error=final.error,
            job_id=job.id,
            state="cancelled" if job.cancelled else "done",
        )
    else:
        resp = ExecResponse.from_output(
            exit_code=0,
            stdout=out_chunk,
            stderr=err_chunk,
            duration_ms=int((time.monotonic() - job.created_at) * 1000),
            job_id=job.id,
            state="running",
        )
    resp.stream = True
    resp.stdout_offset = so
    resp.stderr_offset = se
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
