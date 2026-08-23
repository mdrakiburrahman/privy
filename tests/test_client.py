import json

import requests

from privy.client import RelayClient
from privy.protocol import ExecRequest


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_async_send_populates_and_reuses_request_id(monkeypatch):
    seen = []

    def fake_post(url, headers, data, timeout):
        payload = json.loads(data)
        seen.append(payload)
        if payload["action"] == "submit":
            return _FakeResponse({"exit_code": 0, "job_id": "job-1", "state": "running"})
        return _FakeResponse({"exit_code": 0, "job_id": "job-1", "state": "done"})

    monkeypatch.setattr(requests, "post", fake_post)

    client = RelayClient(namespace="ns", path="path", keyrule="rule", key="key")
    request = ExecRequest(kind="python", code="print(1)", timeout_s=600)

    result = client.send(request, async_job=True)

    assert result.job_id == "job-1"
    assert request.request_id
    assert seen[0]["request_id"] == request.request_id
    assert seen[1]["request_id"] == request.request_id

    seen.clear()
    assert client.submit(request) == "job-1"
    assert seen[0]["request_id"] == request.request_id
