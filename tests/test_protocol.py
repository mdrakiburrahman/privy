from privy.protocol import (
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
