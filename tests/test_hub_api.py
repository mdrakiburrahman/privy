import http.client
import json
import threading
import time

import pytest
import requests
from hub_fakes import PORT, TOKEN, FakeListener, Hub, blocking_run

from privy.client import RelayHTTPError
from privy.hub.app import HubApp, build_hub
from privy.hub.home import TOKEN_FILENAME, read_state
from privy.hub.rest import ROUTES
from privy.hub.service import OPERATIONS
from privy.transfer import TransferResult

NO_LISTENER = '{"error":{"code":"EndpointNotFound","message":"There are no listeners connected"}}'


@pytest.fixture
def hub(tmp_path):
    return Hub(
        tmp_path,
        {
            "dev/box1_windows": FakeListener(hostname="BOX1"),
            "dev/box2_windows": FakeListener(
                info_error=RelayHTTPError("x", status_code=404, detail=NO_LISTENER)
            ),
            "contoso-lab/gpu1_linux": FakeListener(legacy=True),
        },
    )


def test_health_is_public_but_everything_else_needs_the_token(hub):
    assert hub.call("GET", "/healthz", token=None).status == 200
    missing = hub.call("GET", "/v1", token=None)
    assert missing.status == 401
    assert missing.headers["WWW-Authenticate"].startswith("Bearer")
    assert hub.call("GET", "/v1", token="wrong").status == 401
    assert hub.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None).status == 401
    assert hub.call("GET", "/v1").status == 200


def test_foreign_hosts_and_browser_origins_are_refused(hub):
    rebinding = hub.call("GET", "/healthz", headers={"Host": "attacker.example:7748"})
    assert rebinding.status == 403 and rebinding.payload["error"]["code"] == "forbidden_host"
    browser = hub.call("GET", "/v1", headers={"Origin": "https://evil.example"})
    assert browser.status == 403 and browser.payload["error"]["code"] == "forbidden_origin"
    assert hub.call("GET", "/v1", headers={"Host": "localhost:7748"}).status == 200


def test_capabilities_and_openapi_cover_every_operation(hub):
    capabilities = hub.call("GET", "/v1").payload
    names = [operation.name for operation in OPERATIONS]

    assert capabilities["mcp"]["tools"] == [route.operation for route in ROUTES]
    assert sorted(capabilities["mcp"]["tools"]) == sorted(names)
    assert capabilities["status"]["targets"] == 4

    document = hub.call("GET", "/openapi.json").payload
    assert document["openapi"] == "3.1.0"
    operation_ids = {
        spec["operationId"] for methods in document["paths"].values() for spec in methods.values()
    }
    assert set(names) <= operation_ids
    run_body = document["paths"]["/v1/jobs"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert run_body["required"] == ["target", "language", "code"]


def test_list_targets_reports_live_status_and_listener_details(hub):
    payload = hub.call("GET", "/v1/targets").payload

    by_id = {target["id"]: target for target in payload["targets"]}
    assert by_id["dev/box1_windows"]["status"] == "online"
    assert by_id["dev/box1_windows"]["listener"]["hostname"] == "BOX1"
    assert by_id["dev/box2_windows"]["status"] == "offline"
    assert by_id["dev/box2_windows"]["listener"] is None
    assert by_id["contoso-lab/gpu1_linux"]["listener"]["details_available"] is False
    assert payload["counts"]["targets"] == 4 and payload["counts"]["online"] == 3
    assert [relay["name"] for relay in payload["relays"]] == ["dev", "contoso-lab"]

    online = hub.call("GET", "/v1/targets?online_only=true&relay=dev").payload
    assert [target["id"] for target in online["targets"]] == ["dev/box1_windows"]
    assert hub.call("GET", "/v1/targets?relay=nope").status == 404


def test_list_targets_without_a_relays_file_explains_how_to_hydrate(hub):
    hub.relays_file.unlink()

    payload = hub.call("GET", "/v1/targets").payload

    assert payload["targets"] == [] and payload["relays_file"]["exists"] is False
    assert "hydration script" in payload["hint"]


@pytest.mark.parametrize("path", ["/v1/targets/dev/box1_windows", "/v1/targets/dev%2Fbox1_windows"])
def test_get_target_accepts_literal_or_encoded_slash(hub, path):
    payload = hub.call("GET", path).payload

    assert payload["id"] == "dev/box1_windows"
    assert payload["listener_info"]["user"] == "operator"


def test_run_get_and_read_output_through_rest(hub):
    created = hub.call(
        "POST", "/v1/jobs", {"target": "dev/box1_windows", "language": "bash", "code": "hostname"}
    )

    assert created.status == 200
    job = created.payload
    assert job["state"] == "succeeded" and job["stdout"] == "ran bash: hostname\n"
    fetched = hub.call("GET", f"/v1/jobs/{job['job_id']}?wait_s=0").payload
    assert fetched["job_id"] == job["job_id"]
    page = hub.call("GET", f"/v1/jobs/{job['job_id']}/output?stream=stdout&offset=4&limit=4").payload
    assert page["data"] == "bash"
    listed = hub.call("GET", "/v1/jobs?target=dev/box1_windows").payload
    assert listed["jobs"][0]["job_id"] == job["job_id"]


def test_running_jobs_return_a_next_step_and_can_be_cancelled(tmp_path):
    listener = FakeListener(run=blocking_run)
    hub = Hub(tmp_path, {"dev/box1_windows": listener})

    started = hub.call(
        "POST",
        "/v1/jobs",
        {"target": "dev/box1_windows", "language": "python", "code": "import time", "wait_s": 0},
    ).payload
    assert started["done"] is False and "get_job" in started["next_step"]
    assert listener.started.wait(5)

    cancelled = hub.call("POST", f"/v1/jobs/{started['job_id']}/cancel").payload

    assert cancelled["state"] == "cancelled"
    assert listener.cancelled == ["remote-blocking"]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"target": "dev/box1_windows", "language": "ruby", "code": "x"}, 400, "invalid_argument"),
        ({"target": "dev/box1_windows", "language": "bash"}, 400, "invalid_argument"),
        (
            {"target": "dev/box1_windows", "language": "bash", "code": "x", "extra": 1},
            400,
            "invalid_argument",
        ),
        (
            {"target": "dev/box1_windows", "language": "bash", "code": "x", "mode": "inprocess"},
            400,
            "invalid_argument",
        ),
        ({"target": "dev/box1_windows", "language": "bash", "code": "   "}, 400, "invalid_argument"),
        (
            {"target": "dev/box1_windows", "language": "bash", "code": "x", "wait_s": 500},
            400,
            "invalid_argument",
        ),
        ({"target": "box1_windows", "language": "bash", "code": "x"}, 404, "target_not_found"),
        ({"target": "dev/nope", "language": "bash", "code": "x"}, 404, "target_not_found"),
    ],
)
def test_invalid_run_requests_are_explained(hub, body, status, code):
    response = hub.call("POST", "/v1/jobs", body)

    assert response.status == status
    assert response.payload["error"]["code"] == code


def test_rest_rejects_bad_bodies_methods_and_routes(hub):
    assert hub.call("POST", "/v1/jobs", b"{not json").payload["error"]["code"] == "invalid_json"
    assert hub.call("POST", "/v1/jobs", b"[]").payload["error"]["code"] == "invalid_json"
    query = hub.call("POST", "/v1/jobs?target=x", {"language": "bash", "code": "x"})
    assert query.status == 400
    wrong_method = hub.call("DELETE", "/v1/jobs")
    assert wrong_method.status == 405 and wrong_method.headers["Allow"] == "GET, POST"
    assert hub.call("GET", "/v2/nothing").status == 404
    assert hub.call("GET", "/v1/jobs/unknown").status == 404


def test_broadcast_skips_targets_that_are_not_online(hub):
    payload = hub.call(
        "POST",
        "/v1/broadcasts",
        {"targets": ["dev/*"], "language": "powershell", "code": "hostname", "wait_s": 5},
    ).payload

    assert payload["done"] is True
    assert [job["target"] for job in payload["jobs"]] == ["dev/box1_windows"]
    assert payload["skipped"] == [
        {
            "target": "dev/box2_windows",
            "status": "offline",
            "reason": "no listener is connected to this hybrid connection",
        }
    ]
    assert payload["summary"] == {"succeeded": 1, "skipped": 1}
    again = hub.call("GET", f"/v1/broadcasts/{payload['broadcast_id']}").payload
    assert again["broadcast_id"] == payload["broadcast_id"]
    assert (
        hub.call("POST", "/v1/broadcasts", {"targets": ["nomatch*"], "language": "bash", "code": "x"}).status
        == 404
    )


def test_broadcast_with_no_online_targets_reports_the_skips(tmp_path):
    offline = RelayHTTPError("x", status_code=404, detail=NO_LISTENER)
    hub = Hub(
        tmp_path,
        {
            "dev/box1_windows": FakeListener(info_error=offline),
            "dev/box2_windows": FakeListener(info_error=offline),
        },
    )

    response = hub.call("POST", "/v1/broadcasts", {"targets": ["dev/*"], "language": "bash", "code": "true"})

    assert response.status == 200
    payload = response.payload
    assert payload["jobs"] == [] and payload["done"] is True
    assert [item["target"] for item in payload["skipped"]] == ["dev/box1_windows", "dev/box2_windows"]
    assert hub.call("GET", f"/v1/broadcasts/{payload['broadcast_id']}").status == 200


def test_text_with_unpaired_surrogates_is_rejected(hub):
    body = b'{"target": "dev/box1_windows", "language": "python", "code": "print(\\"\\ud800\\")"}'

    response = hub.call("POST", "/v1/jobs", body)

    assert response.status == 400
    assert response.payload["error"]["code"] == "invalid_argument"
    assert "unpaired surrogate" in response.payload["error"]["message"]
    assert hub.jobs.counts()["retained"] == 0


def test_allowed_browser_origins_get_cors_headers(tmp_path):
    hub = Hub(tmp_path)
    hub.app = HubApp(hub.service, token=TOKEN, port=PORT, allowed_origins=["http://localhost:3000/"])
    origin = {"Origin": "http://localhost:3000"}

    preflight = hub.call(
        "OPTIONS", "/v1/targets", token=None, headers={**origin, "Access-Control-Request-Method": "GET"}
    )
    assert preflight.status == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert "Authorization" in preflight.headers["Access-Control-Allow-Headers"]
    assert "Mcp-Method" in preflight.headers["Access-Control-Allow-Headers"]

    allowed = hub.call("GET", "/v1", headers=origin)
    assert allowed.status == 200
    assert allowed.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
    unauthorized = hub.call("GET", "/v1", token=None, headers=origin)
    assert unauthorized.status == 401 and unauthorized.headers["Vary"] == "Origin"

    assert (
        hub.call("OPTIONS", "/v1/targets", token=None, headers={"Origin": "https://evil.example"}).status
        == 403
    )
    assert hub.call("OPTIONS", "/v1/targets", token=None).status == 401
    foreign_host = hub.call(
        "OPTIONS", "/v1/targets", token=None, headers={**origin, "Host": "evil.example:7748"}
    )
    assert foreign_host.status == 403


def test_file_transfer_validates_local_paths(tmp_path):
    def transfer(direction, source, destination, overwrite, progress):
        return TransferResult(direction, str(source), str(destination), 3, "b" * 64, "t")

    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(transfer=transfer)})
    source = tmp_path / "source.txt"
    source.write_text("abc", encoding="utf-8")

    upload = hub.call(
        "POST",
        "/v1/files/upload",
        {"target": "dev/box1_windows", "local_path": str(source), "remote_path": "/tmp/source.txt"},
    ).payload
    assert upload["state"] == "succeeded" and upload["result"]["size"] == 3

    download = hub.call(
        "POST",
        "/v1/files/download",
        {"target": "dev/box1_windows", "remote_path": "/tmp/x", "local_path": str(tmp_path / "x")},
    ).payload
    assert download["state"] == "succeeded" and download["direction"] == "download"

    relative = hub.call(
        "POST",
        "/v1/files/upload",
        {"target": "dev/box1_windows", "local_path": "source.txt", "remote_path": "/tmp/x"},
    )
    assert relative.status == 400
    missing = hub.call(
        "POST",
        "/v1/files/upload",
        {"target": "dev/box1_windows", "local_path": str(tmp_path / "missing"), "remote_path": "/tmp/x"},
    )
    assert missing.status == 404 and missing.payload["error"]["code"] == "local_file_not_found"
    no_dir = hub.call(
        "POST",
        "/v1/files/download",
        {"target": "dev/box1_windows", "remote_path": "/x", "local_path": str(tmp_path / "no" / "x")},
    )
    assert no_dir.status == 404


def test_build_hub_serves_http_on_loopback(tmp_path):
    runtime = build_hub(home=tmp_path / "home", port=0, version="test")
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    try:
        token = (tmp_path / "home" / TOKEN_FILENAME).read_text(encoding="utf-8").strip()
        assert len(token) >= 32
        assert read_state(tmp_path / "home")["url"] == runtime.url

        assert requests.get(f"{runtime.url}/healthz", timeout=5).json()["status"] == "ok"
        assert requests.get(f"{runtime.url}/v1/targets", timeout=5).status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        targets = requests.get(f"{runtime.url}/v1/targets", headers=headers, timeout=5)
        assert targets.status_code == 200 and targets.json()["targets"] == []

        started = time.monotonic()
        mcp = requests.post(
            f"{runtime.url}/mcp",
            headers={**headers, "Accept": "application/json, text/event-stream"},
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            timeout=5,
        )
        assert mcp.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
        assert time.monotonic() - started < 5

        host, port = runtime.server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.putrequest("POST", "/v1/jobs")
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("Content-Length", str(9 * 1024 * 1024))
        connection.endheaders()
        assert connection.getresponse().status == 413
        connection.close()
    finally:
        runtime.shutdown()
        thread.join(5)


def test_hub_token_is_reused_across_restarts(tmp_path):
    first = build_hub(home=tmp_path, port=0, version="test")
    first.server.server_close()
    second = build_hub(home=tmp_path, port=0, version="test")
    second.server.server_close()

    assert first.app._token == second.app._token == (tmp_path / TOKEN_FILENAME).read_text().strip().encode()
    assert TOKEN not in (tmp_path / TOKEN_FILENAME).read_text()
