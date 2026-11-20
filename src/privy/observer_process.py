"""Windows-only containment for private observer helpers, before their first instruction."""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes


class ContainmentError(RuntimeError):
    """Fail closed before an uncontained helper is allowed to run."""


def _windows():
    if os.name != "nt":
        raise ContainmentError("observer_requires_windows_job_containment")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    return kernel


def require_external_launcher():
    """Verify actual membership, not merely the launcher's environment marker."""
    kernel = _windows()
    name = os.environ.get("PRIVY_OBSERVER_JOB", "")
    if not name.startswith("Local\\PrivyObserver-") or len(name) > 100:
        raise ContainmentError("external_observer_launcher_required")
    handle = kernel.OpenJobObjectW(4, False, name)
    if not handle:
        raise ContainmentError("external_observer_job_unavailable")
    try:
        belongs = wintypes.BOOL()
        if (
            not kernel.IsProcessInJob(kernel.GetCurrentProcess(), handle, ctypes.byref(belongs))
            or not belongs.value
        ):
            raise ContainmentError("external_observer_job_membership_required")
    finally:
        kernel.CloseHandle(handle)


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class OwnedProcess(subprocess.Popen):
    """Assign a suspended child to a kill-on-close Job Object before resuming it."""

    def __init__(self, *args, **kwargs):
        self._kernel = _windows()
        self._job = self._kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise ContainmentError("private_observer_job_creation_failed")
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway flags.
        try:
            if not self._kernel.SetInformationJobObject(
                self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ContainmentError("private_observer_job_limits_failed")
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 4  # CREATE_SUSPENDED.
            super().__init__(*args, **kwargs)
            if not self._kernel.AssignProcessToJobObject(self._job, int(self._handle)):
                self.kill()
                self.wait(timeout=2)
                raise ContainmentError("private_observer_job_assignment_failed")
            ntdll = ctypes.WinDLL("ntdll")
            ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            ntdll.NtResumeProcess.restype = ctypes.c_long
            if ntdll.NtResumeProcess(int(self._handle)) != 0:
                raise ContainmentError("private_observer_job_resume_failed")
        except BaseException:
            self.close_tree()
            if hasattr(self, "_handle"):
                for stream in (self.stdout, self.stderr, self.stdin):
                    if stream is not None:
                        stream.close()
                self.wait(timeout=2)
            raise

    def close_tree(self):
        if self._job:
            self._kernel.TerminateJobObject(self._job, 124)
            self._kernel.CloseHandle(self._job)
            self._job = None

    def __exit__(self, *args):
        # Closing the job first releases pipes held by credential grandchildren.
        self.close_tree()
        for stream in (self.stdout, self.stderr, self.stdin):
            if stream is not None:
                stream.close()
        try:
            self.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise ContainmentError("private_observer_cleanup_deadline") from exc
        return False
