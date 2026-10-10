"""Incremental output: executor buffers, watermark polling, client streaming, echo."""

from __future__ import annotations

import base64
import io
import json
import sys
import time

import pytest

from privy import executor
from privy.client import RelayClient
from privy.executor import execute, poll_job, set_echo_output
from privy.protocol import MAX_STREAM_CHUNK_BYTES, ExecRequest, ExecResponse

LOOP = "for i in 1 2 3; do echo $i; sleep 0.4; done"


@pytest.fixture(autouse=True)
def _no_echo():
    set_echo_output(False)
    yield
    set_echo_output(False)


def _submit(code: str, *, stream: bool, kind: str = "bash", mode: str = "subprocess") -> str:
    resp = execute(ExecRequest(kind=kind, code=code, mode=mode, action="submit", stream=stream))
    assert resp.job_id
    assert resp.stream is (True if stream else None)
    return resp.job_id


def _drain_stream(job_id: str, so: int = 0, se: int = 0):
    out, err, polls = b"", b"", []
    while True:
        resp = poll_job(job_id, 5.0, so, se)
        out += resp.stdout
        err += resp.stderr
        so, se = resp.stdout_offset, resp.stderr_offset
        polls.append(resp)
        if resp.state != "running":
            return resp, out, err, polls


def test_running_poll_returns_output_before_exit():
    job = _submit(LOOP, stream=True)
    first = poll_job(job, 5.0, 0, 0)
    assert first.state == "running"
    assert first.stdout == b"1\n"
    assert first.stdout_offset == 2
    final, out, _, _ = _drain_stream(job, first.stdout_offset, first.stderr_offset)
    assert first.stdout + out == b"1\n2\n3\n"
    assert final.state == "done" and final.exit_code == 0


def test_stream_keeps_stdout_and_stderr_separate_and_final_is_tail_only():
    job = _submit("echo out; echo err >&2; exit 3", stream=True)
    final, out, err, _ = _drain_stream(job)
    assert (out, err) == (b"out\n", b"err\n")
    assert final.exit_code == 3 and final.state == "done"


def test_retry_with_same_offsets_is_idempotent_and_ack_frees_memory():
    job = _submit("echo a; echo b", stream=True)
    time.sleep(0.5)
    a = poll_job(job, 1.0, 0, 0)
    again = poll_job(job, 1.0, 0, 0)
    assert a.stdout == again.stdout == b"a\nb\n"
    poll_job(job, 0.1, a.stdout_offset, a.stderr_offset)
    stale = poll_job(job, 0.1, 0, 0)
    assert stale.error == "bad_offset"


def test_offset_beyond_output_is_rejected():
    job = _submit("sleep 1", stream=True)
    assert poll_job(job, 0.1, 99, 0).error == "bad_offset"


def test_large_output_is_chunked_and_complete():
    total = MAX_STREAM_CHUNK_BYTES * 3
    job = _submit(f"head -c {total} /dev/zero | tr '\\0' x", stream=True)
    final, out, _, polls = _drain_stream(job)
    assert len(out) == total
    assert all(len(p.stdout) <= MAX_STREAM_CHUNK_BYTES // 2 for p in polls)
    assert final.state == "done"


def test_legacy_poll_still_returns_full_output_at_completion():
    job = _submit("echo hi", stream=False)
    resp = poll_job(job, 5.0)
    assert resp.state == "done"
    assert resp.stdout == b"hi\n"
    assert resp.stdout_offset is None


def test_inprocess_output_streams():
    code = "import time\nprint('x', flush=True)\ntime.sleep(0.6)\nprint('y')"
    job = _submit(code, stream=True, kind="python", mode="inprocess")
    first = poll_job(job, 3.0, 0, 0)
    assert first.state == "running" and first.stdout == b"x\n"
    final, out, _, _ = _drain_stream(job, first.stdout_offset, first.stderr_offset)
    assert first.stdout + out == b"x\ny\n" and final.state == "done"


def test_sync_exec_output_unchanged():
    resp = execute(ExecRequest(kind="bash", code="echo a; echo b >&2"))
    assert (resp.stdout, resp.stderr, resp.exit_code) == (b"a\n", b"b\n", 0)


def test_timeout_keeps_partial_output():
    resp = execute(ExecRequest(kind="bash", code="echo partial; sleep 30", timeout_s=1))
    assert resp.timed_out and resp.stdout == b"partial\n"


def test_request_rejects_bad_offsets():
    for bad in (-1, "3", True):
        raw = json.dumps({"kind": "bash", "code": "x", "action": "poll", "job_id": "j", "stdout_offset": bad})
        with pytest.raises(ValueError):
            ExecRequest.from_json(raw)


def _loopback(monkeypatch, *, modern: bool = True, poll_failures: int = 0):
    """Route RelayClient straight into the executor, optionally like an old listener."""
    failures = {"left": poll_failures}

    def post(self, payload, http_timeout_s=None):
        raw = json.loads(payload)
        if raw["action"] == "poll" and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("relay request failed: blip")
        if not modern:
            for key in ("stream", "stdout_offset", "stderr_offset"):
                raw.pop(key, None)
        resp = execute(ExecRequest.from_json(json.dumps(raw)))
        obj = json.loads(resp.to_json())
        if not modern:
            for key in ("stream", "stdout_offset", "stderr_offset"):
                obj.pop(key, None)
        return obj

    monkeypatch.setattr(RelayClient, "_post_json", post)


def _client() -> RelayClient:
    return RelayClient(namespace="n", path="p", token=lambda: ("t", 9999999999))


def test_client_streams_chunks_while_running(monkeypatch):
    _loopback(monkeypatch)
    seen: list[tuple[float, str, bytes]] = []
    t0 = time.monotonic()
    result = _client().run_bash(LOOP, on_output=lambda s, d: seen.append((time.monotonic() - t0, s, d)))
    assert result.ok and result.stdout == "1\n2\n3\n"
    assert [d for _, _, d in seen] == [b"1\n", b"2\n", b"3\n"]
    assert seen[0][0] < 0.35 < seen[-1][0]


def test_client_stream_result_matches_buffered(monkeypatch):
    _loopback(monkeypatch)
    code = "echo o; echo e >&2; exit 5"
    streamed = _client().run_bash(code, on_output=lambda s, d: None)
    buffered = _client().run_bash(code)
    assert (streamed.stdout, streamed.stderr, streamed.exit_code) == (
        buffered.stdout,
        buffered.stderr,
        buffered.exit_code,
    )


def test_client_falls_back_against_listener_without_streaming(monkeypatch):
    _loopback(monkeypatch, modern=False)
    seen = []
    result = _client().run_bash("echo one; echo two >&2", on_output=lambda s, d: seen.append((s, d)))
    assert result.stdout == "one\n" and result.stderr == "two\n"
    assert seen == [("stdout", b"one\n"), ("stderr", b"two\n")]


def test_client_retries_failed_poll_without_loss_or_duplicates(monkeypatch):
    _loopback(monkeypatch, poll_failures=2)
    monkeypatch.setattr("privy.client._POLL_BACKOFF_MIN_S", 0.01)
    chunks = []
    result = _client().run_bash("echo a; sleep 0.3; echo b", on_output=lambda s, d: chunks.append(d))
    assert result.stdout == "a\nb\n"
    assert b"".join(chunks) == b"a\nb\n"


def test_on_output_rejects_forced_sync():
    with pytest.raises(ValueError):
        _client().run_bash("true", async_job=False, on_output=lambda s, d: None)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_echo_prefixes_job_output(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    set_echo_output(True)
    job = _submit("echo hello; echo oops >&2; printf tail", stream=True)
    _drain_stream(job)
    prefix = f"[{job[:8]}] "
    assert _wait_for(lambda: out.getvalue() == f"{prefix}hello\n{prefix}tail\n")
    assert _wait_for(lambda: err.getvalue() == f"{prefix}oops\n")


def test_echo_line_splitting_and_partial_flush(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    set_echo_output(True)
    executor._echo_chunk("abcd1234", "stdout", b"par")
    executor._echo_chunk("abcd1234", "stdout", b"tial\nnext\nrest")
    executor._echo_chunk("abcd1234", "stdout", None)
    assert _wait_for(lambda: out.getvalue().endswith("[abcd1234] rest\n"))
    assert out.getvalue() == "[abcd1234] partial\n[abcd1234] next\n[abcd1234] rest\n"


def test_echo_never_blocks_when_queue_is_full(monkeypatch):
    set_echo_output(True)
    monkeypatch.setattr(executor, "_ECHO_QUEUE", executor.queue.Queue(maxsize=1))
    start = time.monotonic()
    for _ in range(1000):
        executor._echo_chunk("abcd1234", "stdout", b"x\n")
    assert time.monotonic() - start < 1.0
    assert executor._ECHO_DROPPED > 0


def test_echo_disabled_is_silent(capsys):
    job = _submit("echo secret", stream=True)
    _drain_stream(job)
    time.sleep(0.2)
    assert "secret" not in capsys.readouterr().out


def test_response_json_roundtrip_has_watermark():
    resp = ExecResponse.from_output(exit_code=0, stdout=b"a", stderr=b"", duration_ms=1)
    resp.stream, resp.stdout_offset, resp.stderr_offset = True, 5, 0
    again = ExecResponse.from_json(resp.to_json())
    assert (again.stream, again.stdout_offset, again.stderr_offset) == (True, 5, 0)
    assert base64.b64decode(again.stdout_b64) == b"a"
