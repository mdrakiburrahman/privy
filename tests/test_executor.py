import time

from privy.executor import cancel_job, execute, poll_job
from privy.protocol import ExecRequest


def test_python_subprocess_ok():
    r = execute(ExecRequest(kind="python", code="print('hello'); import sys; sys.stderr.write('err\\n')"))
    assert r.exit_code == 0
    assert r.stdout == b"hello\n"
    assert r.stderr == b"err\n"
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


def test_python_subprocess_timeout():
    r = execute(ExecRequest(kind="python", code="import time; time.sleep(5)", timeout_s=0.5))
    assert r.timed_out is True
    assert r.error == "timeout"


def test_inprocess_python_ok():
    r = execute(ExecRequest(kind="python", code="print('via-exec')", mode="inprocess"))
    assert r.exit_code == 0
    assert r.stdout == b"via-exec\n"


def test_inprocess_python_exception():
    r = execute(ExecRequest(kind="python", code="raise ValueError('nope')", mode="inprocess"))
    assert r.exit_code == 1
    assert b"ValueError" in r.stderr and b"nope" in r.stderr


def test_inprocess_python_shares_globals_across_calls():
    execute(ExecRequest(kind="python", code="PRIVY_SHARED = 42", mode="inprocess"))
    r = execute(ExecRequest(kind="python", code="print(PRIVY_SHARED)", mode="inprocess"))
    assert r.exit_code == 0
    assert r.stdout == b"42\n"


def test_inprocess_rejects_bash():
    r = execute(ExecRequest(kind="bash", code="echo hi", mode="inprocess"))  # type: ignore[arg-type]
    assert r.exit_code == 2


def test_non_utf8_stdout_is_preserved():
    # Emit raw bytes that are not valid UTF-8 via python subprocess.
    code = "import sys; sys.stdout.buffer.write(bytes([0xff, 0xfe, 0x00, 0x41]))"
    r = execute(ExecRequest(kind="python", code=code))
    assert r.exit_code == 0
    assert r.stdout == bytes([0xFF, 0xFE, 0x00, 0x41])


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
    assert final.stdout == b"late\n"


def test_poll_long_polls_until_done():
    resp = execute(
        ExecRequest(kind="python", code="import time; time.sleep(1); print('ok')", action="submit")
    )
    # A single generous poll should return the finished result, not "running".
    final = poll_job(resp.job_id or "", wait_s=10.0)
    assert final.state == "done"
    assert final.stdout == b"ok\n"


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
    assert r.stdout == b"7\n"


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
    assert fast.stdout == b"fast\n"

    final = _drain(slow.job_id or "")
    assert final.stdout == b"slow\n" * 5


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
