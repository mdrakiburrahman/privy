from privy.executor import execute
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
