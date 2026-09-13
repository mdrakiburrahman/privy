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
import io
import os
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
) -> ExecResponse:
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

    timed_out = False
    try:
        try:
            stdout, stderr = proc.communicate(input=stdin_payload, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                stdout, stderr = b"", b""
    finally:
        _release_process_tree(proc)

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
        self._state_lock = threading.Lock()
        self._cancel_requested = False
        self._finished = False
        self._interruptible = False
        self._completed = threading.Event()
        self.thread = threading.Thread(target=self._target, name="privy-inprocess", daemon=True)

    def start(self) -> bool:
        with self._state_lock:
            if self._cancel_requested:
                self._finished = True
                self._completed.set()
                return False
            self.thread.start()
            return True

    def join(self, timeout: float | None) -> bool:
        return self._completed.wait(timeout=timeout)

    @property
    def completed(self) -> bool:
        return self._completed.is_set()

    def interrupt(self) -> bool:
        with self._state_lock:
            self._cancel_requested = True
            if self._finished:
                return False
            if not self._interruptible:
                return True
            _try_async_raise(self.thread, KeyboardInterrupt, known_active=True)
            return True

    def output(self) -> tuple[bytes, bytes]:
        return self._stdout_buf.getvalue(), self._stderr_buf.getvalue()

    def _target(self) -> None:
        out_router, err_router = _ensure_routers()
        lock = _INPROCESS_LOCK if _SERIALIZE_INPROCESS else _NULL_LOCK
        with lock:
            out_router.register(self._stdout_text)
            err_router.register(self._stderr_text)
            try:
                with self._state_lock:
                    if self._cancel_requested:
                        self.exit_code = 130
                        self.error = "cancelled"
                        return
                    self._interruptible = True
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
                with self._state_lock:
                    self._interruptible = False
                    self._finished = True
                try:
                    self._stdout_text.flush()
                    self._stderr_text.flush()
                finally:
                    out_router.unregister()
                    err_router.unregister()
                    self._completed.set()


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
    if on_run is not None and not on_run(run):
        run.interrupt()
        run.start()
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

    stdout, stderr = run.output()
    return ExecResponse.from_output(
        exit_code=run.exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((time.monotonic() - start) * 1000),
        timed_out=timed_out,
        error=run.error,
    )


def _try_async_raise(
    thread: threading.Thread,
    exc_type: type[BaseException],
    *,
    known_active: bool = False,
) -> None:
    if not known_active and not thread.is_alive():
        return
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
        self._run: _InprocessRun | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._supervisor_completed = threading.Event()
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
        resp: ExecResponse | None = None
        try:
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
            elif self.request.mode == "inprocess":
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
        except BaseException as exc:  # noqa: BLE001 - a job must always become terminal
            resp = ExecResponse.from_output(
                exit_code=130 if isinstance(exc, KeyboardInterrupt) else 1,
                stdout=b"",
                stderr=("job error: " + traceback.format_exc()).encode("utf-8", "replace"),
                duration_ms=int((time.monotonic() - start) * 1000),
                error=type(exc).__name__,
            )
        finally:
            if resp is None:  # pragma: no cover - defensive against interrupted exception handling
                resp = ExecResponse.from_output(
                    exit_code=1,
                    stdout=b"",
                    stderr=b"job supervisor terminated without a response\n",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    error="supervisor_terminated",
                )
            try:
                self._finish(resp)
            finally:
                self._supervisor_completed.set()

    def _finish(self, resp: ExecResponse) -> None:
        retained_run: _InprocessRun | None = None
        retained_proc: subprocess.Popen | None = None
        with self._lock:
            if self.response is not None:
                return
            self.response = resp
            if self._run is not None and not self._run.completed:
                retained_run = self._run
            else:
                self._run = None
            if self._proc is not None and self._proc.poll() is None:
                retained_proc = self._proc
            else:
                self._proc = None
            if retained_run is None and retained_proc is None:
                self.finished_at = time.monotonic()
            self._thread = None
            done = self.done
            self.done = None
        if done is not None:
            done.set()
        if retained_run is not None or retained_proc is not None:
            threading.Thread(
                target=self._await_retained_resources,
                args=(retained_run, retained_proc),
                name=f"privy-job-reaper-{self.id[:8]}",
                daemon=True,
            ).start()
        _reap_jobs()

    def _await_retained_resources(
        self,
        run: _InprocessRun | None,
        proc: subprocess.Popen | None,
    ) -> None:
        run_completed = run is None or run.join(timeout=None)
        proc_completed = proc is None
        if proc is not None:
            try:
                proc.wait()
                proc_completed = True
            except Exception:  # pragma: no cover - retain an unverified process handle
                pass
        with self._lock:
            if run_completed and self._run is run:
                self._run = None
            if proc_completed and self._proc is proc:
                self._proc = None
            if self._run is None and self._proc is None:
                self.finished_at = time.monotonic()
        _reap_jobs()

    def _terminalize_completed_supervisor(self) -> None:
        if not self._supervisor_completed.is_set():
            return
        with self._lock:
            if self.response is not None:
                return
        self._finish(
            ExecResponse.from_output(
                exit_code=1,
                stdout=b"",
                stderr=b"job supervisor exited without publishing a response\n",
                duration_ms=int((time.monotonic() - self.created_at) * 1000),
                error="supervisor_terminated",
            )
        )

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

    def cancel(self) -> bool:
        terminate_proc: subprocess.Popen | None = None
        with self._lock:
            if self.response is not None or self.finished_at is not None:
                return False
            if self._run is not None:
                if not self._run.interrupt():
                    return False
                self.cancelled = True
                return True
            if self._proc is not None:
                if self._proc.poll() is not None:
                    return False
                self.cancelled = True
                terminate_proc = self._proc
            elif not self._supervisor_completed.is_set():
                self.cancelled = True
                return True
            else:
                return False
        if terminate_proc is not None:
            try:
                _terminate_process_tree(terminate_proc)
            except Exception:  # pragma: no cover
                pass
        return True


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

    job._terminalize_completed_supervisor()
    done = job.done
    if done is not None:
        done.wait(timeout=max(0.0, min(wait_s, MAX_POLL_WAIT_S)))
    job._terminalize_completed_supervisor()
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


def cancel_job(job_id: str) -> ExecResponse:
    """Best-effort interrupt of a running job without discarding its result."""
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
    if not job.cancel():
        return poll_job(job_id, wait_s=0)
    return ExecResponse.from_output(
        exit_code=0,
        stdout=b"",
        stderr=b"",
        duration_ms=0,
        job_id=job_id,
        state="cancelled",
    )
