import json

import pytest
import requests

from privy import _relay
from privy.client import RelayClient
from privy.protocol import ExecResponse
from privy.proxy import ProxyClientServer
from privy.server import RelayServer


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _token() -> str:
    return _relay.create_sas_token("ns", "path", "rule", "key", ttl_seconds=60)


def test_client_resolves_token_provider_for_every_request(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    calls = 0
    kinds = []
    urls = []

    def provider():
        nonlocal calls
        calls += 1
        return _token()

    def post(url, **kwargs):
        urls.append(url)
        kinds.append(json.loads(kwargs["data"])["kind"])
        response = ExecResponse.from_output(
            exit_code=0,
            stdout=b"ok\n",
            stderr=b"",
            duration_ms=1,
        )
        return FakeResponse(json.loads(response.to_json()))

    monkeypatch.setattr("privy.client.requests.post", post)
    client = RelayClient(namespace="ns", path="path", token=provider)

    assert client.run_bash("true", timeout_s=1).ok
    assert client.run_python("print(1)", timeout_s=1).ok
    assert client.run_powershell("Write-Output 1", timeout_s=1).ok

    assert calls == 3
    assert all("sb-hc-token=" in url for url in urls)
    assert kinds == ["bash", "python", "powershell"]


def test_client_redacts_token_from_transport_errors(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    token = _token()

    def post(url, **kwargs):
        raise requests.ConnectionError(f"failed URL {url}")

    monkeypatch.setattr("privy.client.requests.post", post)
    client = RelayClient(namespace="ns", path="path", token=token)

    with pytest.raises(RuntimeError) as exc:
        client.run_bash("true", timeout_s=1)

    assert token not in str(exc.value)
    assert "sig%3D" not in str(exc.value)
    assert "sb-hc-token=<redacted>" in str(exc.value)


def test_all_entry_points_accept_injected_tokens(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    token = _token()

    client = RelayClient(namespace="ns", path="path", token=token)
    server = RelayServer(namespace="ns", path="path", token=token)
    proxy = ProxyClientServer(namespace="ns", path="path", token=token)

    assert client._credential.resolve()[0] == token
    assert "sb-hc-token=" in server._listen_url()
    assert proxy._credential.resolve()[0] == token


def test_proxy_binds_its_credential_to_the_http_handler(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    token = _token()
    captured = {}

    class FakeHTTPServer:
        def __init__(self, address, handler):
            captured["address"] = address
            captured["credential"] = handler.relay_credential

        def serve_forever(self):
            return None

    monkeypatch.setattr("privy.proxy.HTTPServer", FakeHTTPServer)
    proxy = ProxyClientServer(namespace="ns", path="path", token=token, local_port=4321)

    proxy.serve_forever()

    assert captured["address"] == ("127.0.0.1", 4321)
    assert captured["credential"].resolve()[0] == token


def test_server_does_not_reconnect_an_expired_token(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    expired = _relay.create_sas_token("ns", "path", "rule", "key", ttl_seconds=1)
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_001)
    server = RelayServer(namespace="ns", path="path", token=expired)

    with pytest.raises(_relay.RelayTokenExpiredError):
        server.serve_forever()
