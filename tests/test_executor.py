import ctypes
import os
import shutil
import time

import pytest

from privy.executor import (
    _JOBS,
    _JOBS_LOCK,
    _child_env,
    _Job,
    cancel_job,
    execute,
    poll_job,
)
from privy.protocol import ExecRequest


def test_python_subprocess_ok():
    r = execute(ExecRequest(kind="python", code="print('hello'); import sys; sys.stderr.write('err\\n')"))
    assert r.exit_code == 0
    assert r.stdout == f"hello{os.linesep}".encode()
    assert r.stderr == f"err{os.linesep}".encode()
    assert not r.timed_out


def test_python_subprocess_nonzero_exit():
    r = execute(ExecRequest(kind="python", code="raise SystemExit(7)"))
    assert r.exit_code == 7


def test_python_subprocess_exception_goes_to_stderr():
    r = execute(ExecRequest(kind="python", code="raise RuntimeError('boom')"))
    assert r.exit_code != 0
    assert b"boom" in r.stderr


def test_bash_subprocess_ok():
    r = execute(ExecRequest(kind="bash", code="echo hi && echo err 1>&2"))
    assert r.exit_code == 0
    assert r.stdout == b"hi\n"
    assert r.stderr == b"err\n"


def test_bash_subprocess_nonzero_exit_preserves_stdout():
    r = execute(ExecRequest(kind="bash", code="echo hi && false"))
    assert r.exit_code == 1
    assert r.stdout == b"hi\n"


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="PowerShell is not installed",
)
def test_powershell_subprocess_ok():
    r = execute(
        ExecRequest(
            kind="powershell",
            code="Write-Output 'hello'; [Console]::Error.WriteLine('err')",
        )
    )
    assert r.exit_code == 0
    assert r.stdout == f"hello{os.linesep}".encode()
    assert r.stderr == f"err{os.linesep}".encode()


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="Windows PowerShell is not installed",
)
def test_windows_powershell_preserves_unicode_code_and_output(monkeypatch):
    monkeypatch.setattr(
        "privy.executor._powershell_executable",
        lambda: shutil.which("powershell.exe"),
    )

    r = execute(ExecRequest(kind="powershell", code="Write-Output 'café 世界'"))

    assert r.exit_code == 0
    assert r.stdout == f"café 世界{os.linesep}".encode()


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="PowerShell is not installed",
)
def test_powershell_subprocess_accepts_code_over_windows_command_line_limit():
    code = "Write-Output 'ok'\n# " + ("x" * 40_000)

    r = execute(ExecRequest(kind="powershell", code=code))

    assert r.exit_code == 0
    assert r.stdout == f"ok{os.linesep}".encode()


@pytest.mark.skipif(
    os.name != "nt" or (shutil.which("pwsh") is None and shutil.which("powershell") is None),
    reason="requires PowerShell on Windows",
)
def test_powershell_timeout_terminates_descendant_process():
    code = """
$executable = (Get-Process -Id $PID).Path
$child = Start-Process -FilePath $executable `
    -ArgumentList '-NoLogo','-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 30' `
    -PassThru
Write-Output $child.Id
Start-Sleep -Seconds 30
"""

    r = execute(ExecRequest(kind="powershell", code=code, timeout_s=2))

    assert r.timed_out
    child_pid = int(r.stdout.strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _windows_process_is_running(child_pid):
        time.sleep(0.1)
    assert not _windows_process_is_running(child_pid)


def test_powershell_subprocess_reports_missing_shell(monkeypatch):
    monkeypatch.setattr("privy.executor.shutil.which", lambda command: None)

    r = execute(ExecRequest(kind="powershell", code="Write-Output 'hello'"))

    assert r.exit_code == 127
    assert r.error == "not_found"
    assert b"PowerShell" in r.stderr


def _windows_process_is_running(process_id):
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, process_id)
    if not process:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
            raise ctypes.WinError()
        return exit_code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


def test_python_subprocess_timeout():
    r = execute(ExecRequest(kind="python", code="import time; time.sleep(5)", timeout_s=0.5))
    assert r.timed_out is True
    assert r.error == "timeout"


def test_inprocess_python_ok():
    r = execute(ExecRequest(kind="python", code="print('via-exec')", mode="inprocess"))
    assert r.exit_code == 0
    assert r.stdout == f"via-exec{os.linesep}".encode()


def test_inprocess_python_exception():
    r = execute(ExecRequest(kind="python", code="raise ValueError('nope')", mode="inprocess"))
    assert r.exit_code == 1
    assert b"ValueError" in r.stderr and b"nope" in r.stderr


def test_inprocess_python_shares_globals_across_calls():
    execute(ExecRequest(kind="python", code="PRIVY_SHARED = 42", mode="inprocess"))
    r = execute(ExecRequest(kind="python", code="print(PRIVY_SHARED)", mode="inprocess"))
    assert r.exit_code == 0
    assert r.stdout == f"42{os.linesep}".encode()


def test_inprocess_rejects_bash():
    r = execute(ExecRequest(kind="bash", code="echo hi", mode="inprocess"))  # type: ignore[arg-type]
    assert r.exit_code == 2


def test_non_utf8_stdout_is_preserved():
    # Emit raw bytes that are not valid UTF-8 via python subprocess.
    code = "import sys; sys.stdout.buffer.write(bytes([0xff, 0xfe, 0x00, 0x41]))"
    r = execute(ExecRequest(kind="python", code=code))
    assert r.exit_code == 0
    assert r.stdout == bytes([0xFF, 0xFE, 0x00, 0x41])


def test_child_environment_removes_listener_and_storage_secrets(monkeypatch):
    secrets = {
        "PRIVY_RELAY_TOKEN": "listen-token",
        "PRIVY_RELAY_KEY": "relay-key",
        "PRIVY_RELAY_SEND_KEY": "send-key",
        "PRIVY_RELAY_LISTEN_KEY": "listen-key",
        "BASE64_ENV": "encoded-env",
        "STORAGE_KEY": "storage-key",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PRIVY_RELAY_NAMESPACE", "safe-namespace")

    child = _child_env()

    assert not secrets.keys() & child.keys()
    assert child["PRIVY_RELAY_NAMESPACE"] == "safe-namespace"


# ---- async jobs ------------------------------------------------------------


def _drain(job_id: str, timeout_s: float = 10.0):
    """Poll a job until it leaves the 'running' state."""
    deadline = time.monotonic() + timeout_s
    while True:
        resp = poll_job(job_id, wait_s=1.0)
        if resp.state != "running":
            return resp
        assert time.monotonic() < deadline, f"job {job_id} never finished"


def test_submit_returns_job_id_immediately():
    start = time.monotonic()
    resp = execute(
        ExecRequest(kind="python", code="import time; time.sleep(2); print('late')", action="submit")
    )
    assert resp.state == "running"
    assert resp.job_id
    # The whole point: submitting must not wait for the work.
    assert time.monotonic() - start < 1.0

    final = _drain(resp.job_id)
    assert final.state == "done"
    assert final.exit_code == 0
    assert final.stdout == f"late{os.linesep}".encode()


def test_poll_long_polls_until_done():
    resp = execute(
        ExecRequest(kind="python", code="import time; time.sleep(1); print('ok')", action="submit")
    )
    # A single generous poll should return the finished result, not "running".
    final = poll_job(resp.job_id or "", wait_s=10.0)
    assert final.state == "done"
    assert final.stdout == f"ok{os.linesep}".encode()


def test_poll_returns_running_before_completion():
    resp = execute(ExecRequest(kind="python", code="import time; time.sleep(3)", action="submit"))
    mid = poll_job(resp.job_id or "", wait_s=0.2)
    assert mid.state == "running"
    _drain(resp.job_id or "")


def test_poll_unknown_job_is_missing():
    resp = poll_job("does-not-exist", wait_s=0.1)
    assert resp.state == "missing"
    assert resp.error == "unknown_job"


def test_inprocess_job_shares_globals():
    submitted = execute(
        ExecRequest(kind="python", code="PRIVY_JOB_SHARED = 7", mode="inprocess", action="submit")
    )
    assert _drain(submitted.job_id or "").state == "done"
    r = execute(ExecRequest(kind="python", code="print(PRIVY_JOB_SHARED)", mode="inprocess"))
    assert r.stdout == f"7{os.linesep}".encode()


def test_job_error_is_reported():
    submitted = execute(ExecRequest(kind="python", code="raise RuntimeError('job-boom')", action="submit"))
    final = _drain(submitted.job_id or "")
    assert final.exit_code != 0
    assert b"job-boom" in final.stderr


def test_job_honours_its_own_timeout():
    submitted = execute(
        ExecRequest(kind="python", code="import time; time.sleep(30)", timeout_s=0.5, action="submit")
    )
    final = _drain(submitted.job_id or "")
    assert final.timed_out is True


def test_cancel_job():
    submitted = execute(ExecRequest(kind="bash", code="sleep 30", action="submit"))
    cancelled = cancel_job(submitted.job_id or "")
    assert cancelled.state == "cancelled"
    # Handle is forgotten, so a later poll no longer knows about it.
    assert poll_job(submitted.job_id or "", wait_s=0.1).state == "missing"


def test_job_cancelled_before_start_does_not_execute(monkeypatch):
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("cancelled job must not execute")

    monkeypatch.setattr("privy.executor._run_subprocess", fake_run)
    job = _Job(ExecRequest(kind="python", code="print('should not run')"))
    worker = job._thread

    job.cancel()
    job.start()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not called


def test_job_adoption_after_cancellation_terminates_process(monkeypatch):
    terminated = []
    monkeypatch.setattr("privy.executor._terminate_process_tree", terminated.append)
    job = _Job(ExecRequest(kind="python", code="print('should not run')"))
    process = object()

    job.cancel()
    job._adopt_proc(process)

    assert terminated == [process]


def test_completed_job_releases_execution_resources():
    submitted = execute(ExecRequest(kind="python", code="print('done')", action="submit", timeout_s=1))
    assert submitted.job_id
    deadline = time.monotonic() + 5
    while poll_job(submitted.job_id, wait_s=0.1).state == "running":
        assert time.monotonic() < deadline

    with _JOBS_LOCK:
        job = _JOBS[submitted.job_id]
    assert job._proc is None
    assert job._run is None
    assert job._thread is None
    assert job.done is None

    cancel_job(submitted.job_id)


def test_concurrent_inprocess_output_is_not_interleaved():
    """Two overlapping inprocess runs must each get only their own stdout."""
    slow = execute(
        ExecRequest(
            kind="python",
            code="import time\nfor _ in range(5):\n    print('slow')\n    time.sleep(0.1)\n",
            mode="inprocess",
            action="submit",
        )
    )
    fast = execute(ExecRequest(kind="python", code="print('fast')", mode="inprocess"))
    assert fast.stdout == f"fast{os.linesep}".encode()

    final = _drain(slow.job_id or "")
    assert final.stdout == f"slow{os.linesep}".encode() * 5


def test_concurrent_inprocess_runs_actually_overlap():
    """Independent runs must not serialize behind one another."""
    start = time.monotonic()
    jobs = [
        execute(
            ExecRequest(
                kind="python",
                code="import time; time.sleep(1)",
                mode="inprocess",
                action="submit",
            )
        )
        for _ in range(4)
    ]
    for job in jobs:
        assert _drain(job.job_id or "", timeout_s=15).state == "done"
    # Serialized would be ~4s; overlapped is ~1s.
    assert time.monotonic() - start < 3.0
