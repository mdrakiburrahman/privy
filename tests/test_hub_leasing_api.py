from __future__ import annotations

from hub_fakes import HOST, FakeListener, Hub

from privy.hub.rest import REST_OPERATIONS, ROUTES, RestApi, write_job_stream
from privy.hub.service import OPERATIONS


def test_long_dashboard_reservation_requires_same_origin_and_preserves_agent_limit(tmp_path):
    hub = Hub(tmp_path)
    owner = hub.call("POST", "/v1/clients", {"name": "Manual", "kind": "human"}).payload["client_id"]
    body = {"client_id": owner, "target": "dev/box1_windows", "ttl": "8h"}
    assert hub.call("POST", "/v1/leases", body).status == 400
    assert hub.call("POST", "/v1/leases", body, headers={"X-Privy-UI": "1"}).status == 403
    headers = {"X-Privy-UI": "1", "Origin": f"http://{HOST}"}
    response = hub.call("POST", "/v1/leases", body, headers=headers)
    assert response.status == 201 and response.payload["manual"]
    assert response.payload["ttl_s"] == 8 * 3600
    other = hub.call("POST", "/v1/clients", {"name": "Agent"}).payload["client_id"]
    conflict = hub.call("POST", "/v1/leases", {**body, "client_id": other, "ttl": "1h"})
    assert conflict.status == 409 and conflict.payload["error"]["code"] == "group_leased"
    assert hub.call("DELETE", f"/v1/leases/{response.payload['lease_id']}?client_id={owner}").status == 200


def test_lease_routes_and_schemas_are_exposed(tmp_path):
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(hostname="BOX1")})

    capabilities = hub.call("GET", "/v1").payload
    document = hub.call("GET", "/openapi.json").payload
    mcp_names = capabilities["mcp"]["tools"]
    route_names = [route.operation for route in ROUTES]

    assert {
        "register_client",
        "list_clients",
        "get_client",
        "delete_client",
        "list_groups",
        "acquire_lease",
        "list_leases",
        "get_lease",
        "renew_lease",
        "release_lease",
    } <= set(mcp_names)
    assert "force_release_lease" not in mcp_names
    assert "force_release_lease" in route_names
    assert {
        "register_client",
        "list_clients",
        "list_groups",
        "acquire_lease",
        "list_leases",
        "get_lease",
        "renew_lease",
        "release_lease",
    } <= set(REST_OPERATIONS)

    for path in ("/v1/clients", "/v1/groups", "/v1/leases"):
        method = "get"
        operation = document["paths"][path][method]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"
    assert any(p["name"] == "lease_id" for p in document["paths"]["/v1/jobs"]["get"]["parameters"])
    assert "client_id" in {p["name"] for p in document["paths"]["/v1/jobs"]["get"]["parameters"]}
    assert (
        "lease_id"
        in document["paths"]["/v1/terminals"]["post"]["requestBody"]["content"]["application/json"]["schema"][
            "properties"
        ]
    )
    force_headers = {
        parameter["name"]
        for parameter in document["paths"]["/v1/leases/{lease_id}/force-release"]["post"]["parameters"]
    }
    assert {"Origin", "X-Privy-UI"} <= force_headers
    force_parameters = {
        parameter["name"]: parameter
        for parameter in document["paths"]["/v1/leases/{lease_id}/force-release"]["post"]["parameters"]
    }
    assert force_parameters["require_unpinned"]["schema"] == {"type": "boolean", "default": False}
    assert [operation.name for operation in OPERATIONS] == mcp_names


def test_lease_lists_are_bare_arrays(tmp_path, monkeypatch):
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(hostname="BOX1")})
    client = [{"client_id": "cl-test"}]
    group = [{"group": "dev/box1"}]
    lease = [{"lease_id": "ls-test"}]
    monkeypatch.setattr(hub.service.leases, "list_clients", lambda *, active=False: client)
    monkeypatch.setattr(hub.service.leases, "groups", lambda *, probe=False: group)
    monkeypatch.setattr(
        hub.service.leases,
        "list_leases",
        lambda *, client_id=None, group=None, label=None, state="active": lease,
    )

    assert hub.call("GET", "/v1/clients").payload == client
    assert hub.call("GET", "/v1/groups").payload == group
    assert hub.call("GET", "/v1/leases").payload == lease


def test_registration_and_acquisition_report_created_status(tmp_path, monkeypatch):
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(hostname="BOX1")})
    client = {"client_id": "cl-test", "created": True}
    existing_client = {**client, "created": False}
    lease = {"lease_id": "ls-test", "created": True}
    existing_lease = {**lease, "created": False}
    monkeypatch.setattr(
        hub.service.leases,
        "register_client",
        lambda **kwargs: existing_client if kwargs.get("session_id") == "existing" else client,
    )
    monkeypatch.setattr(
        hub.service.leases,
        "acquire",
        lambda *args, **kwargs: existing_lease if kwargs.get("target") == "existing" else lease,
    )

    created_client = hub.call("POST", "/v1/clients", {"name": "task"})
    reused_client = hub.call("POST", "/v1/clients", {"name": "task", "session_id": "existing"})
    created_lease = hub.call("POST", "/v1/leases", {"client_id": "cl-test"})
    reused_lease = hub.call("POST", "/v1/leases", {"client_id": "cl-test", "target": "existing"})

    assert created_client.status == created_lease.status == 201
    assert reused_client.status == reused_lease.status == 200
    assert created_client.payload == client and reused_client.payload == existing_client
    assert created_lease.payload == lease and reused_lease.payload == existing_lease


def test_job_list_header_touches_but_query_filter_does_not(tmp_path, monkeypatch):
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(hostname="BOX1")})
    touched = []
    monkeypatch.setattr(hub.service.leases, "touch", lambda lease_id: touched.append(lease_id))
    monkeypatch.setitem(
        hub.service._handlers,
        "list_jobs",
        lambda **kwargs: {"jobs": [], "counts": {}, "filters": kwargs},
    )

    query = hub.call("GET", "/v1/jobs?lease_id=ls-query")
    header = hub.call("GET", "/v1/jobs", headers={"X-Privy-Lease": "ls-header"})

    assert query.status == header.status == 200
    assert touched == ["ls-header"]
    assert query.payload["filters"]["lease_id"] == "ls-query"
    assert header.payload["filters"]["lease_id"] is None


def test_force_release_requires_ui_header_and_same_origin(tmp_path):
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(hostname="BOX1")})
    path = "/v1/leases/ls-not-real/force-release"

    no_origin = hub.call("POST", path, {}, headers={"X-Privy-UI": "1"})
    no_ui_marker = hub.call("POST", path, {}, headers={"Origin": "http://127.0.0.1:7748"})
    foreign_origin = hub.call(
        "POST",
        path,
        {},
        headers={"X-Privy-UI": "1", "Origin": "http://localhost:7748"},
    )

    assert no_origin.status == no_ui_marker.status == foreign_origin.status == 403


def test_bulk_force_release_rejects_pin_added_after_confirmation(tmp_path):
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(hostname="BOX1")})
    store = hub.service.leases
    owner = store.register_client("Bulk admin", session_id="bulk-admin")["client_id"]
    lease = store.acquire(owner, target="dev/box1")
    lease_id = lease["lease_id"]
    assert store.get_lease(lease_id)["running_jobs"] == []

    with store._lock:
        store._lease(lease_id)["running_jobs"].append("job-started-after-confirmation")
    ui_headers = {"X-Privy-UI": "1", "Origin": f"http://{HOST}"}
    path = f"/v1/leases/{lease_id}/force-release"

    guarded = hub.call("POST", path, {"require_unpinned": True}, headers=ui_headers)
    assert guarded.status == 409
    assert guarded.payload["error"]["code"] == "lease_pinned"
    assert store.get_lease(lease_id)["state"] == "active"
    assert store.get_lease(lease_id)["running_jobs"] == ["job-started-after-confirmation"]

    # Omitting the bulk-only guard retains the established individual force-release behavior.
    individual = hub.call("POST", path, {}, headers=ui_headers)
    assert individual.status == 200
    assert individual.payload["state"] == "released"


def test_execution_header_is_an_alternative_to_lease_id_query(tmp_path):
    route = next(route for route in ROUTES if route.operation == "run")
    args = RestApi._arguments(
        route,
        {},
        {"target": ["dev/box1_windows"]},
        {"X-Privy-Lease": "ls-header"},
        b"hostname",
    )

    assert args["lease_id"] == "ls-header"
    assert args["code"] == "hostname"


def test_job_stream_starts_with_advisory_lease_warning():
    class Job:
        id = "job-1"
        target = type("Target", (), {"id": "dev/box1_windows"})()
        spec = {"language": "bash"}
        type = "exec"
        lease_warning = {
            "lease_id": "ls-held",
            "group": "dev/box1",
            "holder": {"name": "other client"},
            "expires_at": "2026-11-01T21:00:00Z",
        }

        @staticmethod
        def next_pieces(index, poll_s):
            return [], index, True

        @staticmethod
        def summary():
            return {"state": "succeeded", "exit_code": 0}

    chunks = []
    write_job_stream(Job(), chunks.append)

    stream = b"".join(chunks).decode("utf-8")
    assert "[privy] lease_warning=" in stream
    assert '"lease_id":"ls-held"' in stream
