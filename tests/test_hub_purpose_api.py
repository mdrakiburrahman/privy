import json
from urllib.parse import quote

import pytest
from hub_fakes import FakeListener, Hub

from privy.client import RelayHTTPError
from privy.hub.mcp import tool_definitions
from privy.hub.metrics import HubMetrics
from privy.transfer import TransferResult

BOX = "dev/box1_windows"
PURPOSE = "privy: Implementing HYCO purpose history."


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path)


def history(hub, suffix=""):
    response = hub.call("GET", f"/v1/purposes?target={BOX}{suffix}")
    assert response.status == 200
    return response.payload


def test_json_raw_and_streaming_share_purpose_contract(hub):
    first = hub.call("POST", "/v1/jobs", {"target": BOX, "code": "hostname", "purpose": PURPOSE})
    assert first.status == 200
    assert first.payload["purpose"] == PURPOSE
    assert hub.call("POST", f"/v1/jobs?target={BOX}&purpose={quote(PURPOSE)}", "hostname").status == 200
    response, output = hub.stream(
        "POST", f"/v1/jobs?target={BOX}&purpose={quote(PURPOSE)}&stream=true", "hostname"
    )
    assert response.status == 200 and b"state=succeeded" in output
    assert history(hub)["groups"][0]["count"] == 3
    assert all(not hasattr(request, "purpose") for request in hub.listeners[BOX].requests)
    assert PURPOSE not in hub.audit_path.read_text(encoding="utf-8")


def test_untagged_work_creates_gap_but_reads_and_rejections_do_not(hub):
    assert hub.call("POST", "/v1/jobs", {"target": BOX, "code": "x", "purpose": PURPOSE}).status == 200
    for path in ("/healthz", "/v1", "/v1/targets", "/v1/jobs", "/v1/stats"):
        assert hub.call("GET", path).status == 200
    assert hub.call("POST", "/v1/jobs", {"target": BOX, "code": "", "purpose": "invalid"}).status == 400
    assert len(history(hub)["groups"]) == 1
    assert hub.call("POST", "/v1/jobs", {"target": BOX, "code": "x"}).status == 200
    result = history(hub)
    assert [g["purpose"] for g in result["groups"]] == [None, PURPOSE]
    assert result["latest"] == PURPOSE and result["latest_unknown"]


@pytest.mark.parametrize("value", [3, False, [], "\0bad", "\ud800", "x" * 241])
def test_invalid_purpose_never_starts_work(hub, value):
    response = hub.call("POST", "/v1/jobs", {"target": BOX, "code": "x", "purpose": value})
    assert response.status == 400 and response.payload["error"]["code"] == "invalid_argument"
    assert hub.jobs.list() == []
    assert history(hub)["groups"] == []


@pytest.mark.parametrize("value", [None, "", "  \t\n "])
def test_explicit_empty_purpose_is_unknown(hub, value):
    assert hub.call("POST", "/v1/jobs", {"target": BOX, "code": "x", "purpose": value}).status == 200
    assert history(hub)["groups"][0]["purpose"] is None


def test_broadcast_copies_purpose_to_each_child(hub):
    response = hub.call("POST", "/v1/broadcasts", {"targets": ["dev/*"], "code": "x", "purpose": PURPOSE})
    assert response.status == 200 and len(response.payload["jobs"]) == 2
    assert all(job["purpose"] == PURPOSE for job in response.payload["jobs"])
    assert history(hub)["groups"][0]["count"] == 1
    assert PURPOSE not in hub.audit_path.read_text(encoding="utf-8")


def test_file_and_sync_metadata_stays_local(hub, tmp_path):
    def transfer(direction, source, destination, overwrite, progress):
        return TransferResult(direction, str(source), str(destination), 4, "a" * 64, "purpose-test")

    hub.listeners[BOX] = FakeListener(transfer=transfer)
    source = tmp_path / "input.txt"
    source.write_text("test", encoding="utf-8")
    for direction in ("upload", "download", "sync"):
        payload = {
            "target": BOX,
            "local_path": str(source),
            "remote_path": "C:\\output.txt",
            "purpose": PURPOSE,
        }
        if direction == "sync":
            payload["destination_type"] = "file"
        response = hub.call("POST", f"/v1/files/{direction}", payload)
        assert response.status in (200, 202)
        assert response.payload["purpose"] == PURPOSE
        if direction != "sync":
            assert response.payload["state"] == "succeeded"
    assert history(hub)["groups"][0]["count"] == 3
    assert PURPOSE not in hub.audit_path.read_text(encoding="utf-8")


def test_skipped_broadcast_targets_and_capacity_rejection_do_not_record(tmp_path):
    offline = "dev/box2_windows"
    hub = Hub(tmp_path, {offline: FakeListener(info_error=RelayHTTPError("offline", status_code=503))})
    response = hub.call(
        "POST", "/v1/broadcasts", {"targets": ["dev/*"], "code": "x", "purpose": PURPOSE, "only_online": True}
    )
    assert response.status == 200 and len(response.payload["jobs"]) == 1
    assert hub.service.metrics.purposes.history(offline)["groups"] == []
    assert history(hub)["groups"][0]["count"] == 1
    hub.jobs._max_queued = 0
    response = hub.call("POST", "/v1/jobs", {"target": BOX, "code": "x", "purpose": "Rejected"})
    assert response.status == 429
    assert history(hub)["groups"][0]["count"] == 1


def test_null_sync_purpose_is_accepted_but_null_paths_are_not(hub, tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("test", encoding="utf-8")
    payload = {
        "target": BOX,
        "local_path": str(source),
        "remote_path": "C:\\output.txt",
        "destination_type": "file",
        "purpose": None,
    }
    assert hub.call("POST", "/v1/files/sync", payload).status in (200, 202)
    assert history(hub)["groups"][0]["purpose"] is None
    assert hub.call("POST", "/v1/files/sync", {**payload, "local_path": None}).status == 400
    assert history(hub)["groups"][0]["count"] == 1


def test_audit_restore_does_not_restore_purpose(hub):
    hub.call("POST", "/v1/jobs", {"target": BOX, "code": "x", "purpose": PURPOSE})
    metrics = HubMetrics()
    assert metrics.restore(hub.audit_path) == 1
    assert metrics.purposes.history(BOX)["groups"] == []


def test_history_pagination_and_text(hub):
    for purpose in ("A", "B", "C"):
        hub.call("POST", "/v1/jobs", {"target": BOX, "code": "x", "purpose": purpose})
    first = history(hub, "&limit=1")
    second = history(hub, "&limit=1&cursor=" + quote(first["next_cursor"]))
    assert second["groups"][0]["purpose"] == "B"
    text = hub.call("GET", f"/v1/purposes?target={BOX}&format=text").text
    assert "C" in text and "reset" in text.lower()
    assert hub.call("GET", f"/v1/purposes?target={BOX}&cursor=invalid").status == 400


def test_mcp_accepts_and_discovers_purpose(hub):
    response = hub.call(
        "POST",
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run", "arguments": {"target": BOX, "code": "x", "purpose": PURPOSE}},
        },
    )
    assert response.status == 200
    assert not response.payload.get("error")
    assert history(hub)["groups"][0]["purpose"] == PURPOSE
    tools = {tool["name"]: tool for tool in tool_definitions()}
    for name in ("run", "broadcast", "upload_file", "download_file"):
        prop = tools[name]["inputSchema"]["properties"]["purpose"]
        assert prop["maxLength"] == 240 and "privy:" in prop["description"]
    assert "get_purpose_history" in tools


def test_terminal_open_is_hub_only_and_followups_do_not_create_gaps(tmp_path):
    requests = []

    def terminal(payload):
        requests.append(payload)
        return {
            "kind": "terminal",
            "protocol_version": 1,
            "ok": True,
            "session_id": "remote",
            "state": "running",
        }

    listener = FakeListener(
        info={
            "platform": {"system": "Windows"},
            "capabilities": {"terminal": {"version": 1, "shells": ["powershell"]}},
            "runtimes": {"powershell": "pwsh"},
        },
        terminal=terminal,
    )
    hub = Hub(tmp_path, {BOX: listener})
    payload = {
        "target": BOX,
        "shell": "powershell",
        "owner": "owner-0123456789abcdef",
        "request_id": "request-0123456789abcdef",
        "purpose": PURPOSE,
    }
    assert hub.call("POST", "/v1/terminals", payload).status == 200
    assert hub.call("POST", "/v1/terminals", payload).status == 409
    assert (
        hub.call("POST", f"/v1/terminals/{payload['request_id']}/read", {"owner": payload["owner"]}).status
        == 200
    )
    assert len(history(hub)["groups"]) == 1
    assert all("purpose" not in request for request in requests)
    assert PURPOSE not in hub.audit_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("purpose,expected_status", [(3, 400), (PURPOSE, 409)])
def test_invalid_or_failed_terminal_open_does_not_record(tmp_path, purpose, expected_status):
    listener = FakeListener(
        info={
            "platform": {"system": "Windows"},
            "capabilities": {"terminal": {"version": 1, "shells": ["powershell"]}},
            "runtimes": {"powershell": "pwsh"},
        },
        terminal=lambda _: {
            "kind": "terminal",
            "protocol_version": 1,
            "ok": False,
            "error": {"code": "capacity", "message": "No terminal capacity"},
        },
    )
    hub = Hub(tmp_path, {BOX: listener})
    payload = {
        "target": BOX,
        "shell": "powershell",
        "owner": "owner-0123456789abcdef",
        "request_id": "request-0123456789abcdef",
        "purpose": purpose,
    }
    assert hub.call("POST", "/v1/terminals", payload).status == expected_status
    assert history(hub)["groups"] == []
    if expected_status == 400:
        assert listener.info_calls == 0 and not listener.terminal_requests


def test_discovery_contains_examples_and_ephemeral_semantics(hub):
    for route in ("/", "/llms.txt", "/v1", "/openapi.json"):
        response = hub.call("GET", route)
        text = response.text if route in ("/", "/llms.txt") else json.dumps(response.payload)
        assert "purpose" in text and "privy:" in text
        assert "restart" in text.lower()
    spec = hub.call("GET", "/openapi.json").payload
    for route in (
        "/v1/jobs",
        "/v1/broadcasts",
        "/v1/files/upload",
        "/v1/files/download",
        "/v1/files/sync",
        "/v1/terminals",
    ):
        content = spec["paths"][route]["post"]["requestBody"]["content"]["application/json"]
        assert "purpose" in content["schema"]["properties"]
        assert content["schema"]["properties"]["purpose"]["type"] == ["string", "null"]
        assert "purpose" in content["example"]
