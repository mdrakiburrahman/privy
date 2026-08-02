from privy.protocol import (
    MAX_POLL_WAIT_S,
    ExecRequest,
    ExecResponse,
    b64decode_str,
    b64encode_bytes,
)


def test_b64_roundtrip_non_utf8():
    data = bytes(range(256))
    assert b64decode_str(b64encode_bytes(data)) == data


def test_exec_request_roundtrip():
    req = ExecRequest(kind="python", code="print(1)", mode="subprocess", timeout_s=10)
    parsed = ExecRequest.from_json(req.to_json())
    assert parsed == req


def test_exec_request_rejects_inprocess_bash():
    bad = '{"kind":"bash","code":"echo","mode":"inprocess"}'
    try:
        ExecRequest.from_json(bad)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("should have rejected inprocess bash")


def test_exec_response_from_output_decodes():
    resp = ExecResponse.from_output(exit_code=0, stdout=b"hi\n", stderr=b"", duration_ms=3)
    roundtripped = ExecResponse.from_json(resp.to_json())
    assert roundtripped.stdout == b"hi\n"
    assert roundtripped.stderr == b""
    assert roundtripped.exit_code == 0
    assert roundtripped.duration_ms == 3


def test_exec_request_defaults_to_exec_action():
    parsed = ExecRequest.from_json('{"kind":"python","code":"print(1)"}')
    assert parsed.action == "exec"
    assert parsed.job_id is None


def test_exec_request_job_roundtrip():
    req = ExecRequest(kind="python", code="print(1)", action="poll", job_id="abc", wait_s=5)
    parsed = ExecRequest.from_json(req.to_json())
    assert parsed == req


def test_exec_request_rejects_unknown_action():
    try:
        ExecRequest.from_json('{"kind":"python","code":"x","action":"nope"}')
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("should have rejected unknown action")


def test_exec_request_poll_requires_job_id():
    try:
        ExecRequest.from_json('{"kind":"python","code":"","action":"poll"}')
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("poll without job_id should be rejected")


def test_exec_request_clamps_poll_wait():
    parsed = ExecRequest.from_json('{"kind":"python","code":"","action":"poll","job_id":"a","wait_s":600}')
    assert parsed.wait_s == MAX_POLL_WAIT_S


def test_exec_response_carries_job_state():
    resp = ExecResponse.from_output(
        exit_code=0, stdout=b"", stderr=b"", duration_ms=0, job_id="j1", state="running"
    )
    roundtripped = ExecResponse.from_json(resp.to_json())
    assert roundtripped.job_id == "j1"
    assert roundtripped.state == "running"
