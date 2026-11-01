"""Exercise lease-aware HTTP/MCP surfaces with actual stores and fake Relay I/O."""

from __future__ import annotations

import base64
import json

import pytest
from hub_fakes import FakeListener, Hub, relays_document, write_relays
from test_hub_remote_exec import OWNER, REQUEST, TerminalStub, info

from privy.hub.leases import LeaseStore
from privy.transfer import TransferResult

BOX = "dev/box1_windows"
WSL = "dev/box1_wsl"


@pytest.fixture
def leased(tmp_path):
    terminal = TerminalStub()

    def transfer(direction, local, remote, overwrite, progress):
        progress(4, 4)
        return TransferResult(direction, local, remote, 4, "digest", "transfer")

    hub = Hub(
        tmp_path,
        {
            BOX: FakeListener(info=info(powershell="pwsh"), terminal=terminal, transfer=transfer),
            WSL: FakeListener(transfer=transfer),
        },
    )
    document = relays_document()
    document["relays"] = [document["relays"][0]]
    document["relays"][0]["hybrid_connections"].append(
        {"path": "box1_wsl", "keyrule": "all", "key": "a2V5MQ=="}
    )
    write_relays(hub.relays_file, document)
    now = [1_800_000_000.0]
    store = LeaseStore(hub.registry, clock=lambda: now[0])
    hub.service.leases = store
    hub.jobs.on_finish = store.job_finished
    client = hub.call(
        "POST", "/v1/clients", {"name": "integration", "session_id": "session", "labels": {"any": "tag"}}
    )
    assert client.status == 201
    lease = hub.call(
        "POST",
        "/v1/leases",
        {
            "client_id": client.payload["client_id"],
            "target": "box1",
            "purpose": "privy: Testing API integration.",
            "labels": {"any": "a=b"},
        },
    )
    assert lease.status == 201
    return hub, now, client.payload, lease.payload, terminal


def carrier(lease_id, mode):
    return (
        {"lease_id": lease_id} if mode == "body" else {},
        f"?lease_id={lease_id}" if mode == "query" else "",
        {"X-Privy-Lease": lease_id} if mode == "header" else {},
    )


def test_real_registration_acquisition_status_and_duration_seconds_compatibility(leased):
    hub, _, client, lease, _ = leased
    registration = hub.call("POST", "/v1/clients", {"name": "renamed", "session_id": "session"})
    assert registration.status == 200 and registration.payload["client_id"] == client["client_id"]
    again = hub.call("POST", "/v1/leases", {"client_id": client["client_id"], "target": lease["group"]})
    assert again.status == 200 and again.payload["lease_id"] == lease["lease_id"]
    another = hub.call("POST", "/v1/leases", {"client_id": client["client_id"], "ttl": 7200})
    assert another.status == 201 and another.payload["group"] != lease["group"]
    assert another.payload["ttl_s"] == 7200
    for ttl in (7200, "2h", "7200"):
        renewed = hub.call(
            "POST",
            f"/v1/leases/{lease['lease_id']}/renew",
            {
                "client_id": client["client_id"],
                "ttl": ttl,
            },
        )
        assert renewed.status == 200 and renewed.payload["ttl_s"] == 7200


@pytest.mark.parametrize("mode", ["body", "query", "header"])
def test_run_poll_cancel_and_stream_carry_lease_and_inherit_purpose(leased, mode):
    hub, now, client, lease, _ = leased
    body, query, headers = carrier(lease["lease_id"], mode)
    now[0] += 100
    response = hub.call(
        "POST", "/v1/jobs" + query, {"target": WSL, "code": "echo leased", **body}, headers=headers
    )
    assert response.status == 200 and response.payload["state"] == "succeeded"
    job = response.payload
    assert job["lease_id"] == lease["lease_id"] and job["client_id"] == client["client_id"]
    assert job["purpose"] == lease["purpose"]
    now[0] += 100
    for suffix in ("", "/output?stream=stdout", "/stream"):
        path = f"/v1/jobs/{job['job_id']}{suffix}"
        path += ("&" if "?" in path else "?") + f"lease_id={lease['lease_id']}"
        result = hub.call("GET", path)
        assert result.status == 200
        if result.stream:
            result.stream(lambda _: None)
    cancelled = hub.call(
        "POST", f"/v1/jobs/{job['job_id']}/cancel", headers={"X-Privy-Lease": lease["lease_id"]}
    )
    assert cancelled.status == 200
    assert hub.service.leases.get_lease(lease["lease_id"])["expires_at"] > lease["expires_at"]


@pytest.mark.parametrize("mode", ["body", "query", "header"])
@pytest.mark.parametrize("direction", ["upload", "download"])
def test_transfer_carries_lease_on_all_forms(leased, tmp_path, mode, direction):
    hub, _, _, lease, _ = leased
    path = tmp_path / "data"
    path.write_text("data")
    body, query, headers = carrier(lease["lease_id"], mode)
    response = hub.call(
        "POST",
        f"/v1/files/{direction}{query}",
        {
            "target": WSL,
            "local_path": str(path),
            "remote_path": "/tmp/data",
            **body,
        },
        headers=headers,
    )
    assert response.status == 200 and response.payload["state"] == "succeeded"
    assert response.payload["lease_id"] == lease["lease_id"]
    assert response.payload["purpose"] == lease["purpose"]


@pytest.mark.parametrize("mode", ["body", "query", "header"])
def test_sync_and_broadcast_admission_carry_lease(leased, tmp_path, monkeypatch, mode):
    hub, _, _, lease, _ = leased
    path = tmp_path / "data"
    path.write_text("data")

    def sync(client, **kwargs):
        kwargs["report"](
            {
                "phase": "done",
                "bytes_done": 4,
                "bytes_total": 4,
                "files_copied": 1,
                "bytes_copied": 4,
            }
        )

    monkeypatch.setattr("privy.hub.jobs.sync", sync)
    body, query, headers = carrier(lease["lease_id"], mode)
    response = hub.call(
        "POST",
        "/v1/files/sync" + query,
        {
            "target": WSL,
            "local_path": str(path),
            "remote_path": "/tmp/data",
            "destination_type": "file",
            **body,
        },
        headers=headers,
    )
    assert response.status == 202
    job = hub.jobs.get(response.payload["job_id"])
    hub.jobs.wait(job, 5)
    assert job.state == "succeeded" and job.lease_id == lease["lease_id"]
    broadcast = hub.call(
        "POST",
        "/v1/broadcasts" + query,
        {
            "targets": ["dev/box1_*"],
            "code": "echo leased",
            **body,
        },
        headers=headers,
    )
    assert broadcast.status == 200
    assert len(broadcast.payload["jobs"]) == 2
    assert all(j["lease_id"] == lease["lease_id"] for j in broadcast.payload["jobs"])


@pytest.mark.parametrize("mode", ["body", "query", "header"])
def test_terminal_open_and_io_touch_lease_without_pinning(leased, mode):
    hub, now, _, lease, terminal = leased
    body, query, headers = carrier(lease["lease_id"], mode)
    now[0] += 100
    opened = hub.call(
        "POST",
        "/v1/terminals" + query,
        {
            "target": BOX,
            "shell": "powershell",
            "owner": OWNER,
            "request_id": REQUEST,
            **body,
        },
        headers=headers,
    )
    assert opened.status == 200 and opened.payload["ok"]
    now[0] += 100
    for action, extra in [
        ("write", {"seq": 0, "data_b64": base64.b64encode(b"echo\n").decode()}),
        ("read", {"offset": 0}),
        ("resize", {"cols": 100, "rows": 30}),
        ("close", {}),
    ]:
        response = hub.call(
            "POST",
            f"/v1/terminals/{REQUEST}/{action}" + query,
            {
                "owner": OWNER,
                "target": BOX,
                **extra,
                **body,
            },
            headers=headers,
        )
        assert response.status == 200
    state = hub.service.leases.get_lease(lease["lease_id"])
    assert state["expires_at"] > lease["expires_at"] and state["running_jobs"] == []
    assert terminal.closed


def test_public_peek_lists_filters_and_holder_authorization(leased):
    hub, now, client, lease, _ = leased
    result = hub.call(
        "POST", "/v1/jobs", {"target": WSL, "code": "echo leased", "lease_id": lease["lease_id"]}
    )
    assert result.status == 200
    before = hub.service.leases.get_lease(lease["lease_id"])["expires_at"]
    now[0] += 100
    for path in (
        "/v1/clients",
        "/v1/groups",
        "/v1/leases?label=any=a%3Db",
        f"/v1/leases/{lease['lease_id']}",
        f"/v1/jobs?lease_id={lease['lease_id']}",
        f"/v1/jobs?client_id={client['client_id']}",
    ):
        assert hub.call("GET", path).status == 200
    assert hub.service.leases.get_lease(lease["lease_id"])["expires_at"] == before
    invalid = hub.call("POST", f"/v1/leases/{lease['lease_id']}/renew", {"client_id": "not-holder"})
    assert invalid.status == 403 and invalid.payload["error"]["code"] == "not_lease_holder"
    plain = hub.call("POST", "/v1/jobs", {"target": WSL, "code": "echo human"})
    assert plain.status == 200 and plain.payload["lease_warning"]["lease_id"] == lease["lease_id"]
    assert hub.service.leases.get_lease(lease["lease_id"])["expires_at"] == before


def test_mcp_lists_have_object_structured_content_and_use_real_store(leased):
    hub, _, client, lease, _ = leased
    for name, key in (("list_clients", "clients"), ("list_groups", "groups"), ("list_leases", "leases")):
        response = hub.call(
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": {}},
            },
        )
        assert response.status == 200
        result = response.payload["result"]
        assert not result["isError"] and isinstance(result["structuredContent"][key], list)
        assert isinstance(json.loads(result["content"][0]["text"]), list)
    assert client["client_id"] and lease["lease_id"]


def test_expired_http_token_revives_then_reports_loss_after_takeover(leased):
    hub, now, client, lease, _ = leased
    now[0] += 1801
    revived = hub.call(
        "POST",
        "/v1/jobs",
        {"target": WSL, "code": "echo revived"},
        headers={"X-Privy-Lease": lease["lease_id"]},
    )
    assert revived.status == 200 and revived.payload["lease_id"] == lease["lease_id"]
    now[0] += 2701
    other = hub.call("POST", "/v1/clients", {"name": "other"}).payload
    assert hub.call("POST", "/v1/leases", {"client_id": other["client_id"], "target": WSL}).status == 201
    lost = hub.call(
        "POST",
        "/v1/jobs",
        {"target": WSL, "code": "must not run"},
        headers={"X-Privy-Lease": lease["lease_id"]},
    )
    assert lost.status == 409 and lost.payload["error"]["code"] == "lease_lost"
    assert hub.call("DELETE", f"/v1/leases/{lease['lease_id']}?client_id={client['client_id']}").status == 200
