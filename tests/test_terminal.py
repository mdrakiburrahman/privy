from __future__ import annotations

import base64
import json
import queue
import threading
import time

import pytest

from privy.terminal import MAX_INPUT, MAX_OUTPUT, TerminalManager

OWNER = "workspace-0123456789"
REQUEST = "open-request-0123456789"


class Backend:
    def __init__(self):
        self.chunks = queue.Queue()
        self.closed = threading.Event()
        self.exit_code = None
        self.writes = []
        self.sizes = []

    def read(self):
        try:
            return self.chunks.get(timeout=0.02)
        except queue.Empty:
            return b"" if self.closed.is_set() else None

    def write(self, data):
        self.writes.append(data)

    def resize(self, cols, rows):
        self.sizes.append((cols, rows))

    def close(self):
        self.exit_code = 0
        self.closed.set()


@pytest.fixture
def manager():
    backends, now = [], [0.0]

    def factory(*args):
        backend = Backend()
        backends.append(backend)
        return backend

    manager = TerminalManager(clock=lambda: now[0], backend_factory=factory)
    yield manager, backends, now
    manager.close()


def call(manager, action, **args):
    return manager.handle(
        {
            "kind": "terminal",
            "protocol_version": 1,
            "owner": OWNER,
            "action": action,
            **args,
        }
    )


def opened(manager, **args):
    return call(manager, "open", **{"request_id": REQUEST, "shell": "bash", **args})


def write(manager, session_id, seq=0, data=b"echo test\r"):
    return call(manager, "write", session_id=session_id, seq=seq, data_b64=base64.b64encode(data).decode())


def test_open_is_idempotent_and_owner_scoped(manager):
    manager, backends, _ = manager
    first = opened(manager)
    second = opened(manager)
    assert first["ok"] and first["session_id"] == second["session_id"]
    assert len(backends) == 1
    other = call(manager, "read", session_id=first["session_id"], owner="different-owner-0123456")
    assert other["error"]["code"] == "forbidden"
    assert (
        call(manager, "close", session_id=first["session_id"], owner="different-owner-0123456")["ok"] is False
    )


def test_close_request_before_open_and_repeated_close_never_spawn(manager):
    manager, backends, _ = manager
    assert call(manager, "close", request_id=REQUEST)["state"] == "closed"
    assert opened(manager)["state"] == "closed"
    assert call(manager, "close", request_id=REQUEST)["ok"]
    assert not backends


def test_input_deduplication_rejects_gaps_and_changed_payload(manager):
    manager, backends, _ = manager
    session_id = opened(manager)["session_id"]
    assert write(manager, session_id)["next_seq"] == 1
    assert write(manager, session_id)["next_seq"] == 1
    assert backends[0].writes == [b"echo test\r"]
    assert write(manager, session_id, seq=2)["error"]["code"] == "sequence_gap"
    assert write(manager, session_id, seq=0, data=b"different")["error"]["code"] == "sequence_gap"


def test_only_new_input_resets_idle_ttl_poll_output_resize_and_duplicate_do_not(manager):
    manager, backends, now = manager
    session_id = opened(manager)["session_id"]
    now[0] = 1000
    assert write(manager, session_id)["expires_in_s"] == 3600
    now[0] = 2000
    assert write(manager, session_id)["expires_in_s"] == 2600
    automatic = call(
        manager,
        "write",
        session_id=session_id,
        seq=1,
        data_b64=base64.b64encode(b"\x1b[1;1R").decode(),
        user_input=False,
    )
    assert automatic["ok"] and automatic["expires_in_s"] == 2600
    assert call(manager, "resize", session_id=session_id, cols=120, rows=42)["expires_in_s"] == 2600
    backends[0].chunks.put(b"background output")
    assert call(manager, "read", session_id=session_id, wait_s=0.1)["expires_in_s"] == 2600
    now[0] = 4600
    assert call(manager, "read", session_id=session_id, wait_s=0)["state"] == "expired"
    assert backends[0].closed.is_set()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cols", True),
        ("cols", 1),
        ("cols", 501),
        ("rows", 0),
        ("rows", 301),
        ("shell", "python"),
        ("owner", "short"),
        ("request_id", "../x"),
        ("protocol_version", True),
        ("protocol_version", 2),
    ],
)
def test_open_rejects_invalid_protocol_and_bounds_without_spawning(manager, field, value):
    manager, backends, _ = manager
    args = {
        "kind": "terminal",
        "protocol_version": 1,
        "action": "open",
        "owner": OWNER,
        "request_id": REQUEST,
        "shell": "bash",
        field: value,
    }
    assert not manager.handle(args)["ok"]
    assert not backends


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seq", True),
        ("seq", -1),
        ("seq", 2**53),
        ("data_b64", "!not base64!"),
        ("data_b64", ""),
        ("data_b64", base64.b64encode(b"\xff").decode()),
        ("data_b64", base64.b64encode(b"x" * (MAX_INPUT + 1)).decode()),
    ],
    ids=["boolean", "negative", "overflow", "invalid-base64", "empty", "invalid-utf8", "oversized"],
)
def test_input_bounds_reject_invalid_data_before_write(manager, field, value):
    manager, backends, _ = manager
    session_id = opened(manager)["session_id"]
    payload = {"seq": 0, "data_b64": "eA==", field: value}
    assert not call(manager, "write", session_id=session_id, **payload)["ok"]
    assert not backends[0].writes


def test_exact_input_limit_and_unicode_round_trip(manager):
    manager, backends, _ = manager
    session_id = opened(manager)["session_id"]
    text = "λ".encode() * (MAX_INPUT // 2)
    assert write(manager, session_id, data=text)["ok"]
    assert backends[0].writes == [text]


def test_capacity_and_shutdown_cleanup(manager):
    manager, backends, _ = manager
    for index in range(4):
        assert opened(manager, request_id=f"request-number-{index:04d}")["ok"]
    assert opened(manager)["error"]["code"] == "capacity"
    manager.close()
    assert all(backend.closed.is_set() for backend in backends)
    assert not opened(manager)["ok"]


def test_output_replay_chunk_bound_and_explicit_gap(manager):
    manager, backends, _ = manager
    session_id = opened(manager)["session_id"]
    backends[0].chunks.put(b"x" * (MAX_OUTPUT + 10))
    deadline = time.monotonic() + 2
    while manager._sessions[session_id].start == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    gap = call(manager, "read", session_id=session_id, offset=0, wait_s=0)
    assert gap["error"]["code"] == "buffer_gap" and gap["start_offset"] == 10
    first = call(manager, "read", session_id=session_id, offset=10, max_bytes=32768, wait_s=0)
    second = call(manager, "read", session_id=session_id, offset=10, max_bytes=32768, wait_s=0)
    assert first == second
    assert len(base64.b64decode(first["data_b64"])) == 32768
    assert first["offset"] == 32778
    assert len(json.dumps(first).encode()) < 65536
    assert len(manager._sessions[session_id].output) == MAX_OUTPUT


def test_failed_partial_input_closes_shell_instead_of_replaying(manager):
    manager, backends, _ = manager
    session_id = opened(manager)["session_id"]

    def broken(data):
        raise OSError("partial write")

    backends[0].write = broken
    assert write(manager, session_id)["error"]["code"] == "terminal_error"
    assert backends[0].closed.is_set()
    assert not write(manager, session_id)["ok"]


def test_natural_exit_drains_all_buffered_output_before_final_state(manager):
    manager, backends, _ = manager
    session_id = opened(manager)["session_id"]
    backends[0].chunks.put(b"a" * 40000)
    backends[0].chunks.put(b"")
    deadline = time.monotonic() + 2
    while manager._sessions[session_id].state == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    first = call(manager, "read", session_id=session_id, offset=0, wait_s=0)
    assert first["state"] == "running" and first["offset"] == 32768
    last = call(manager, "read", session_id=session_id, offset=32768, wait_s=0)
    assert last["state"] == "exited" and last["offset"] == 40000


def test_failed_cleanup_is_retained_retried_and_does_not_block_other_expiry(manager):
    manager, backends, now = manager
    first_id = opened(manager)["session_id"]
    second_id = opened(manager, request_id="second-request-0123456789")["session_id"]
    first, second = manager._sessions[first_id], manager._sessions[second_id]
    original = backends[0].close

    def failed():
        raise OSError("cleanup temporarily unavailable")

    backends[0].close = failed
    now[0] = 4000
    manager._prune()
    assert first.cleanup_pending and first.id in manager._sessions
    assert second.state == "expired" and backends[1].closed.is_set()
    backends[0].close = original
    manager._prune()
    assert not first.cleanup_pending and first.error is None
    assert backends[0].closed.is_set()


def test_shutdown_attempts_every_session_even_when_one_cleanup_fails(manager):
    manager, backends, _ = manager
    opened(manager)
    opened(manager, request_id="second-request-0123456789")
    original = backends[0].close

    def failed():
        raise OSError("cleanup temporarily unavailable")

    backends[0].close = failed
    try:
        with pytest.raises(OSError, match="Failed to clean up 1"):
            manager.close()
        assert backends[1].closed.is_set()
    finally:
        backends[0].close = original
        manager.close()
