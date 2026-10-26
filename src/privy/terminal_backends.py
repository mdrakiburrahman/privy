"""Native PTY adapters; no subprocess-pipe approximation of an interactive shell."""

from __future__ import annotations

import errno
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from privy.executor import _child_env, available_runtimes


def supported_shells() -> dict[str, str]:
    if os.name == "nt":
        try:
            import winpty  # noqa: F401
        except (ImportError, OSError):
            return {}
        if sys.getwindowsversion().build < 17763:
            return {}
    elif sys.platform != "linux" or not shutil.which("setsid"):
        return {}
    # Git Bash/ConPTY can swallow input after resize; never advertise a broken terminal.
    allowed = ("powershell",) if os.name == "nt" else ("powershell", "bash")
    return {
        name: path
        for name, path in available_runtimes().items()
        if name in allowed and path and (os.name == "nt" or not path.lower().endswith(".exe"))
    }


def spawn(shell: str, cols: int, rows: int) -> Any:
    executable = supported_shells().get(shell)
    if executable is None:
        raise ValueError(f"native interactive {shell} is unavailable")
    env = _child_env()
    argv = [executable, "-NoLogo", "-NoExit"] if shell == "powershell" else [executable, "-i"]
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    return WindowsTerminal(argv, env, cols, rows) if os.name == "nt" else PosixTerminal(argv, env, cols, rows)


class PosixTerminal:
    def __init__(self, argv: list[str], env: dict[str, str], cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios

        setsid = shutil.which("setsid")
        if not setsid:
            raise OSError("Linux setsid is required for a controlling terminal")
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            # setsid establishes the controlling PTY without unsafe preexec_fn or frozen Python forks.
            process = subprocess.Popen(
                [setsid, "-c", *argv],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
            )
        except BaseException:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        self.process, self.pid, self.fd = process, process.pid, master
        self.exit_code: int | None = None
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._cleanup_complete = False
        self._master_closed = False
        os.set_blocking(master, False)

    def read(self) -> bytes | None:
        import select

        if self._closed.is_set():
            return b""
        try:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if not ready:
                return None if self.alive() else b""
            with self._close_lock:
                return b"" if self._closed.is_set() else os.read(self.fd, 32768)
        except BlockingIOError:
            return None
        except OSError as exc:
            if exc.errno == errno.EIO or self._closed.is_set():
                return b""
            raise

    def write(self, data: bytes) -> None:
        import select

        deadline, position = time.monotonic() + 5, 0
        while position < len(data):
            if self._closed.is_set() or time.monotonic() > deadline:
                raise OSError("terminal input timed out or terminal closed")
            _, ready, _ = select.select([], [self.fd], [], 0.1)
            if ready:
                try:
                    with self._close_lock:
                        if self._closed.is_set():
                            raise OSError("terminal closed")
                        position += os.write(self.fd, data[position:])
                except BlockingIOError:
                    continue

    def resize(self, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios

        with self._close_lock:
            if self._closed.is_set():
                raise OSError("terminal closed")
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def alive(self) -> bool:
        self.exit_code = self.process.poll()
        return self.exit_code is None

    def close(self) -> None:
        with self._close_lock:
            if self._cleanup_complete:
                return
            self._closed.set()
            failures = []
            # Interactive foreground jobs have their own process groups but share this session.
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    fields = (entry / "stat").read_text().rpartition(")")[2].split()
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
                if int(fields[3]) == self.pid:
                    try:
                        os.kill(int(entry.name), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError as exc:
                        failures.append(exc)
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not self._master_closed:
                os.close(self.fd)
                self._master_closed = True
            if self.process.poll() is None:
                self.process.kill()
            if self.exit_code is None:
                self.exit_code = self.process.wait(timeout=5)
            if failures:
                raise OSError("Permission denied terminating terminal descendants") from failures[0]
            self._cleanup_complete = True


class _WindowsJob:
    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", w.DWORD),
                ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t),
                ("processes", w.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class Counters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class Limits(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", Counters),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE),
            ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
            ("TerminateProcess", [w.HANDLE, w.UINT], w.BOOL),
            ("CloseHandle", [w.HANDLE], w.BOOL),
        ):
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            limits = Limits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process = self.kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process or not self.kernel.AssignProcessToJobObject(self.handle, process):
                if process:
                    self.kernel.TerminateProcess(process, 1)
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise
        finally:
            if process:
                self.kernel.CloseHandle(process)

    def close(self) -> None:
        import ctypes

        if self.handle:
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


class WindowsTerminal:
    def __init__(self, argv: list[str], env: dict[str, str], cols: int, rows: int) -> None:
        from winpty import PTY
        from winpty.enums import Backend

        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self.exit_code: int | None = None
        self.pty = PTY(cols, rows, backend=Backend.ConPTY)
        self.job: _WindowsJob | None = None
        try:
            environment = "\0".join(f"{key}={value}" for key, value in sorted(env.items())) + "\0"
            if not self.pty.spawn(
                argv[0],
                cmdline=" " + subprocess.list2cmdline(argv[1:]),
                cwd=os.getcwd(),
                env=environment,
            ):
                raise OSError("ConPTY failed to start the shell")
            self.pid = self.pty.pid
            if self.pid is None:
                raise OSError("ConPTY did not return a shell process")
            self.job = _WindowsJob(self.pid)
        except BaseException:
            self.pty = None
            raise

    def read(self) -> bytes | None:
        with self._close_lock:
            if self._closed.is_set():
                return b""
            data = self.pty.read(blocking=False)
            if data:
                return data.encode("utf-8")
            if self.pty.iseof() or not self.pty.isalive():
                self.exit_code = self.pty.get_exitstatus()
                return b""
        self._closed.wait(0.01)
        return None

    def write(self, data: bytes) -> None:
        text = data.decode("utf-8")
        with self._close_lock:
            if self._closed.is_set():
                raise OSError("terminal closed")
            self.pty.write(text)

    def resize(self, cols: int, rows: int) -> None:
        with self._close_lock:
            if self._closed.is_set():
                raise OSError("terminal closed")
            self.pty.set_size(cols, rows)

    def alive(self) -> bool:
        return not self._closed.is_set() and self.pty.isalive()

    def close(self) -> None:
        with self._close_lock:
            if self._closed.is_set() and self.pty is None and (self.job is None or self.job.handle is None):
                return
            self._closed.set()
            try:
                if self.pty is not None:
                    self.exit_code = self.pty.get_exitstatus()
                if self.job:
                    self.job.close()
            finally:
                if self.pty is not None:
                    self.pty = None
