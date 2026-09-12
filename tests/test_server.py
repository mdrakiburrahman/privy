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
