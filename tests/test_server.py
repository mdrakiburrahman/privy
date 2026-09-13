import threading
from concurrent.futures import ThreadPoolExecutor

from privy.protocol import ExecResponse
from privy.server import CONTROL_CHANNEL_BODY_LIMIT, RelayServer, _InlineResponsePump


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


def test_32_inline_responses_are_written_only_by_listener_thread():
    server = RelayServer(
        namespace="ns",
        path="path",
        keyrule="rule",
        key="key",
        max_workers=64,
    )
    control = FakeWebSocket()
    control.send_threads = []
    original_send = control.send

    def record_send(value):
        control.send_threads.append(threading.get_ident())
        original_send(value)

    control.send = record_send
    barrier = threading.Barrier(32)

    def execute(payload, request_id):
        barrier.wait(timeout=5)
        return ExecResponse.from_output(
            exit_code=0,
            stdout=str(request_id).encode(),
            stderr=b"",
            duration_ms=1,
        )

    server._execute = execute
    server._pool = ThreadPoolExecutor(max_workers=64)
    pump = _InlineResponsePump()
    listener_thread = threading.get_ident()
    try:
        for index in range(32):
            server._handle_inline(
                control,
                {
                    "method": "POST",
                    "id": f"request-{index}",
                    "body": False,
                    "address": "wss://relay/rendezvous",
                },
                pump,
            )
        server._pool.shutdown(wait=True)
        server._pool = None
        server._flush_inline_responses(control, pump)
    finally:
        if server._pool is not None:
            server._pool.shutdown(wait=True)

    assert len(control.sent) == 64
    assert set(control.send_threads) == {listener_thread}


def test_listener_connections_use_distinct_relay_connection_ids():
    server = RelayServer(
        namespace="ns",
        path="path",
        keyrule="rule",
        key="key",
        listener_connections=3,
    )

    urls = [server._listen_url(index) for index in range(3)]

    assert len(set(urls)) == 3
    assert all("sb-hc-id=privy-" in url for url in urls)
