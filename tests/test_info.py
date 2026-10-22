import json

import pytest
import requests

from privy import _relay
from privy.client import RelayClient, RelayHTTPError
from privy.info import collect_listener_info
from privy.protocol import LISTENER_INFO_KIND, ExecRequest, ExecResponse
from privy.server import RelayServer


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, text=None):
        self.payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error", response=self)

    def json(self):
        return self.payload


def _client():
    return RelayClient(namespace="ns", path="path", keyrule="rule", key="key")


def test_listener_info_describes_the_host_without_running_code():
    info = collect_listener_info(started_at=0.0, proxy_enabled=True)

    assert info["kind"] == LISTENER_INFO_KIND
    assert info["hostname"]
    assert info["pid"] > 0
    assert set(info["runtimes"]) == {"python", "bash", "powershell"}
    assert info["capabilities"]["http_proxy"] is True
    assert info["capabilities"]["languages"][0] == "python"
    assert set(info["jobs"]) == {"running", "retained"}
    assert info["uptime_s"] >= 0
    json.dumps(info)


def test_server_answers_listener_info_before_execution_parsing():
    server = RelayServer(namespace="ns", path="path", keyrule="rule", key="key")

    raw = server._execute(json.dumps({"kind": LISTENER_INFO_KIND}), request_id="request-id")

    payload = json.loads(raw)
    assert payload["kind"] == LISTENER_INFO_KIND
    assert payload["capabilities"]["http_proxy"] is False


def test_listener_info_kind_is_rejected_by_execution_parsing():
    # Listeners that predate listener_info answer it as a bad execution request,
    # which RelayClient.listener_info() reports as a reachable legacy listener.
    with pytest.raises(ValueError, match="invalid kind"):
        ExecRequest.from_json(json.dumps({"kind": LISTENER_INFO_KIND}))


def test_client_listener_info_returns_the_listener_description(monkeypatch):
    sent = []

    def post(url, **kwargs):
        sent.append(json.loads(kwargs["data"]))
        return FakeResponse({"kind": LISTENER_INFO_KIND, "hostname": "box"})

    monkeypatch.setattr("privy.client.requests.post", post)

    assert _client().listener_info() == {"kind": LISTENER_INFO_KIND, "hostname": "box"}
    assert sent[0]["kind"] == LISTENER_INFO_KIND


def test_client_listener_info_reports_a_legacy_listener_as_none(monkeypatch):
    legacy = ExecResponse.from_output(
        exit_code=2,
        stdout=b"",
        stderr=b"invalid request\n",
        duration_ms=0,
        error="bad_request",
    )
    monkeypatch.setattr(
        "privy.client.requests.post",
        lambda url, **kwargs: FakeResponse(json.loads(legacy.to_json())),
    )

    assert _client().listener_info() is None


def test_http_failures_carry_status_and_redacted_detail(monkeypatch):
    monkeypatch.setattr(_relay.time, "time", lambda: 1_800_000_000)
    token = _relay.create_sas_token("ns", "path", "rule", "key", ttl_seconds=60)

    def post(url, **kwargs):
        return FakeResponse(status_code=503, text=f"No listener for {token}")

    monkeypatch.setattr("privy.client.requests.post", post)
    client = RelayClient(namespace="ns", path="path", token=token)

    with pytest.raises(RelayHTTPError) as exc:
        client.listener_info()

    assert exc.value.status_code == 503
    assert exc.value.timed_out is False
    assert "No listener" in exc.value.detail
    assert token not in exc.value.detail
    assert isinstance(exc.value, RuntimeError)


def test_http_timeouts_are_marked(monkeypatch):
    def post(url, **kwargs):
        raise requests.ReadTimeout("read timed out")

    monkeypatch.setattr("privy.client.requests.post", post)

    with pytest.raises(RelayHTTPError) as exc:
        _client().listener_info()

    assert exc.value.status_code is None
    assert exc.value.timed_out is True


def test_send_reports_the_listener_job_id_through_on_submit(monkeypatch):
    client = _client()
    actions = []

    def send_raw(request, *, http_timeout_s=None):
        actions.append(request.action)
        if request.action == "submit":
            return ExecResponse.from_output(
                exit_code=0, stdout=b"", stderr=b"", duration_ms=0, job_id="job-1", state="running"
            )
        return ExecResponse.from_output(
            exit_code=0, stdout=b"done\n", stderr=b"", duration_ms=5, job_id="job-1", state="done"
        )

    monkeypatch.setattr(client, "_send_raw", send_raw)
    submitted = []

    result = client.send(ExecRequest(kind="bash", code="true", timeout_s=5), on_submit=submitted.append)

    assert submitted == ["job-1"]
    assert actions == ["submit", "poll"]
    assert result.stdout == "done\n"


def test_on_submit_requires_the_job_transport():
    with pytest.raises(ValueError, match="on_submit requires the async job transport"):
        _client().send(ExecRequest(kind="bash", code="true"), async_job=False, on_submit=lambda job: None)
