import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

from privy.server import CONTROL_CHANNEL_BODY_LIMIT, RelayServer


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(value)

    def close(self):
        self.closed = True


def _server():
    return RelayServer(namespace="ns", path="path", keyrule="rule", key="key")


def test_large_inline_response_uses_rendezvous(monkeypatch):
    server = _server()
    control = FakeWebSocket()
    rendezvous = FakeWebSocket()
    body = "x" * (CONTROL_CHANNEL_BODY_LIMIT + 1)
    server._execute = lambda payload, request_id: body
    monkeypatch.setattr("privy.server.websocket.create_connection", lambda address: rendezvous)

    server._handle_inline(
        control,
        {
            "method": "POST",
            "id": "request-id",
            "body": False,
            "address": "wss://relay/rendezvous",
        },
    )

    assert control.sent == []
    assert len(rendezvous.sent) == 2
    assert '"requestId": "request-id"' in rendezvous.sent[0]
    assert rendezvous.sent[1] == body
    assert rendezvous.closed is True


def test_control_channel_handles_response_at_limit(monkeypatch):
    server = _server()
    control = FakeWebSocket()
    body = "x" * CONTROL_CHANNEL_BODY_LIMIT
    server._execute = lambda payload, request_id: body

    def unexpected_rendezvous(address):
        raise AssertionError(f"unexpected rendezvous: {address}")

    monkeypatch.setattr("privy.server.websocket.create_connection", unexpected_rendezvous)

    server._handle_inline(
        control,
        {
            "method": "POST",
            "id": "request-id",
            "body": False,
            "address": "wss://relay/rendezvous",
        },
    )

    assert len(control.sent) == 2
    assert control.sent[1] == body


def test_inline_workers_queue_responses_for_control_thread():
    server = _server()
    control = FakeWebSocket()
    outbox = queue.Queue(maxsize=2)
    server._execute = lambda payload, request_id: "controlled response"
    main_thread = threading.get_ident()
    writers = []
    original = control.send

    def send(value):
        writers.append(threading.get_ident())
        original(value)

    control.send = send
    with ThreadPoolExecutor(max_workers=1) as pool:
        server._pool = pool
        server._handle_inline(control, {"id": "request-id", "body": False}, response_queue=outbox)
    assert control.sent == []
    assert outbox.qsize() == 1
    server._flush_control_responses(control, outbox)
    assert len(control.sent) == 2 and writers == [main_thread, main_thread]
    assert control.sent[1] == "controlled response"


def test_control_flush_is_bounded_and_preserves_response_pairs():
    server = _server()
    server._max_workers = 1
    outbox = queue.Queue()
    for index in range(3):
        outbox.put((str(index), f"body-{index}"))
    control = FakeWebSocket()
    server._flush_control_responses(control, outbox)
    assert len(control.sent) == 4 and outbox.qsize() == 1
    assert '"requestId": "0"' in control.sent[0] and control.sent[1] == "body-0"
    assert '"requestId": "1"' in control.sent[2] and control.sent[3] == "body-1"


def test_control_readiness_checks_decrypted_buffer_before_socket(monkeypatch):
    server = _server()

    class BufferedSocket:
        def pending(self):
            return 1

    control = FakeWebSocket()
    control.sock = BufferedSocket()
    monkeypatch.setattr(
        "privy.server.select.select",
        lambda *args: (_ for _ in ()).throw(AssertionError("buffered TLS should not select")),
    )
    assert server._control_readable(control)


def test_control_readiness_uses_short_poll_without_changing_frame_timeout(monkeypatch):
    server = _server()
    control = FakeWebSocket()
    control.sock = object()
    calls = []
    monkeypatch.setattr("privy.server.select.select", lambda *args: calls.append(args) or ([], [], []))
    assert not server._control_readable(control)
    assert calls[0][0] == [control.sock] and 0 < calls[0][3] <= 0.025


def test_large_worker_response_still_uses_dedicated_rendezvous(monkeypatch):
    server = _server()
    control, dedicated = FakeWebSocket(), FakeWebSocket()
    outbox = queue.Queue(maxsize=2)
    server._execute = lambda payload, request_id: "x" * (CONTROL_CHANNEL_BODY_LIMIT + 1)
    monkeypatch.setattr("privy.server.websocket.create_connection", lambda address: dedicated)
    with ThreadPoolExecutor(max_workers=1) as pool:
        server._pool = pool
        server._handle_inline(
            control,
            {"id": "large", "body": False, "address": "wss://relay/rendezvous"},
            response_queue=outbox,
        )
    assert outbox.empty() and not control.sent
    assert len(dedicated.sent) == 2 and dedicated.closed


def test_reconnect_never_routes_old_worker_response_to_new_control_socket(monkeypatch):
    server = _server()
    callbacks = []

    class DeferredPool:
        def submit(self, callback):
            callbacks.append(callback)

    class Connection(FakeWebSocket):
        def __init__(self, frames):
            super().__init__()
            self.frames = iter(frames)

        def settimeout(self, timeout):
            self.timeout = timeout

        def recv(self):
            return next(self.frames)

    old = Connection([json.dumps({"request": {"method": "POST", "id": "old", "body": False}}), ""])
    new = Connection([""])
    connections = iter([old, new])
    server._pool = DeferredPool()
    server._execute = lambda payload, request_id: "old body"
    server._listen_url = lambda: "wss://fake/control"
    server._control_readable = lambda ws: True
    monkeypatch.setattr("privy.server.websocket.create_connection", lambda url: next(connections))
    server._serve_once()
    assert old.closed and not old.sent and len(callbacks) == 1
    callbacks[0]()
    server._serve_once()
    assert new.closed and not new.sent
