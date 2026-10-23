import http.client
import json
import logging
import queue
import threading
import time
from urllib.parse import quote

import pytest
import requests
from hub_fakes import FakeListener, FakeRelayClient, Hub, Scripted, blocking_run, exec_result, write_relays

from privy.client import RelayHTTPError
from privy.hub.app import HubApp, build_hub
from privy.hub.home import LEGACY_TOKEN_FILENAME, read_state
from privy.hub.rest import ROUTES, STREAM_JOB
from privy.hub.service import OPERATIONS
from privy.transfer import TransferResult

NO_LISTENER = '{"error":{"code":"EndpointNotFound","message":"There are no listeners connected"}}'
OFFLINE = RelayHTTPError("x", status_code=404, detail=NO_LISTENER)
BOX1 = "dev/box1_windows"


@pytest.fixture
def hub(tmp_path):
    # contoso-lab/box1_windows gets a default (online, Windows) listener.
    return Hub(
        tmp_path,
        {
            BOX1: FakeListener(hostname="BOX1"),
            "dev/box2_windows": FakeListener(info_error=OFFLINE),
            "contoso-lab/gpu1_linux": FakeListener(legacy=True),
        },
    )


def follow(response) -> queue.Queue:
    """Consume a streamed response in the background; each item is one write, then None."""
    writes: queue.Queue = queue.Queue()

    def consume():
        try:
            response.stream(writes.put)
        finally:
            writes.put(None)

    threading.Thread(target=consume, daemon=True).start()
    return writes


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---- discovery -----------------------------------------------------------------


def test_every_endpoint_is_open_without_credentials(hub):
    assert hub.call("GET", "/healthz").status == 200
    assert hub.call("GET", "/v1").status == 200
    assert hub.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}).status == 200
    # Clients configured for hub 0.2 still send a token, and browsers send Origin: both are ignored.
    stale = {"Authorization": "Bearer from-hub-0.2", "Origin": "http://localhost:3000"}
    assert hub.call("GET", "/v1/targets", headers=stale).status == 200
    assert hub.call("GET", "/v1", headers={"Host": "devbox.example:7748"}).status == 200


def test_quickstart_lists_online_machines_with_runnable_examples(hub):
    response = hub.call("GET", "/", headers={"Host": "localhost:9999"})

    assert response.status == 200 and response.media_type().startswith("text/plain")
    text = response.text
    assert "No authentication" in text
    assert "## Machines online now (3 of 4)" in text
    assert "| gpu1 | contoso-lab/gpu1_linux | ? | ? | bash |" in text
    assert "1 more are not online" in text
    # box1 exists in two relays, so the examples use its full id.
    assert (
        'curl.exe -sN "http://localhost:9999/v1/jobs?stream=true&target=dev/box1_windows" '
        "--data-binary 'Get-Date; hostname'"
    ) in text
    # A bare @file is PowerShell's splatting operator, so the advice must quote it.
    assert "`--data-binary '@script.ps1'`" in text and "--data-binary @" not in text
    assert hub.call("GET", "/llms.txt", headers={"Host": "localhost:9999"}).text == text


def test_quickstart_without_relays_explains_hydration(hub):
    hub.relays_file.unlink()

    text = hub.call("GET", "/").text

    assert "No relays are hydrated yet" in text and "target=devbox01" in text


def test_quickstart_names_machines_the_way_they_resolve_now(tmp_path):
    # box1 also exists in contoso-lab, but offline there: the bare name means the online one.
    hub = Hub(tmp_path, {"contoso-lab/box1_windows": FakeListener(info_error=OFFLINE)})

    text = hub.call("GET", "/").text

    assert "| box1 | dev/box1_windows |" in text
    assert '/v1/jobs?stream=true&target=box1" --data-binary' in text
    assert hub.call("POST", "/v1/jobs?target=box1", "hostname").payload["target"] == BOX1


def test_capabilities_and_openapi_describe_every_operation(hub):
    capabilities = hub.call("GET", "/v1").payload
    names = [operation.name for operation in OPERATIONS]

    assert capabilities["authentication"] == "none"
    assert capabilities["quickstart"] == "http://127.0.0.1:7748/"
    assert capabilities["mcp"]["tools"] == names
    assert [item["name"] for item in capabilities["operations"]] == [route.operation for route in ROUTES]
    assert capabilities["status"]["targets"] == 4

    hub.call("GET", "/v1/targets")  # probe, so the examples name a machine that is online
    document = hub.call("GET", "/openapi.json").payload
    assert document["openapi"] == "3.1.0" and "security" not in document
    assert (
        'curl.exe -sN "http://127.0.0.1:7748/v1/jobs?stream=true&target=dev/box1_windows"'
        in (document["info"]["description"])
    )
    operation_ids = {
        spec["operationId"] for methods in document["paths"].values() for spec in methods.values()
    }
    assert set(names) | {STREAM_JOB, "quickstart", "llms_txt"} <= operation_ids
    run = document["paths"]["/v1/jobs"]["post"]
    body = run["requestBody"]["content"]
    assert body["application/json"]["schema"]["required"] == ["target", "code"]
    assert body["text/plain"]["example"] == "Get-Date; hostname"
    assert {"target", "language", "format", "stream"} <= {param["name"] for param in run["parameters"]}
    assert "stream=true&target=dev/box1_windows" in run["x-codeSamples"][0]["source"]
    assert run["responses"]["200"]["content"]["application/json"]["example"]["state"] == "succeeded"
    stream = document["paths"]["/v1/jobs/{job_id}/stream"]["get"]
    assert stream["responses"]["200"]["content"]["text/plain"]["example"].startswith("[privy] job=")


# ---- targets -------------------------------------------------------------------


def test_list_targets_reports_live_status_and_listener_details(hub):
    payload = hub.call("GET", "/v1/targets").payload

    by_id = {target["id"]: target for target in payload["targets"]}
    box1 = by_id[BOX1]
    assert (box1["status"], box1["machine"], box1["host"]) == ("online", "box1", "BOX1")
    assert (box1["user"], box1["privy_version"], box1["default_language"]) == (
        "operator",
        "0.2.0",
        "powershell",
    )
    box2 = by_id["dev/box2_windows"]
    assert box2["status"] == "offline" and "host" not in box2 and "no listener" in box2["reason"]
    legacy = by_id["contoso-lab/gpu1_linux"]
    assert "older listener" in legacy["details"] and legacy["default_language"] == "bash"
    assert payload["counts"] == {"targets": 4, "offline": 1, "online": 3}
    assert [relay["name"] for relay in payload["relays"]] == ["dev", "contoso-lab"]

    online = hub.call("GET", "/v1/targets?online_only=true&relay=dev").payload
    assert [target["id"] for target in online["targets"]] == [BOX1]
    assert hub.call("GET", "/v1/targets?relay=nope").status == 404


def test_list_targets_as_text_is_a_compact_table(hub):
    text = hub.call("GET", "/v1/targets?format=text").text

    assert text.startswith("3 of 4 machines online (relays: dev, contoso-lab)\nonline:\n")
    assert "BOX1" in text and "offline (1): dev/box2_windows" in text


def test_list_targets_without_a_relays_file_explains_how_to_hydrate(hub):
    hub.relays_file.unlink()

    payload = hub.call("GET", "/v1/targets").payload

    assert payload["targets"] == [] and payload["relays_file"]["exists"] is False
    assert "hydration script" in payload["hint"]


@pytest.mark.parametrize("path", ["/v1/targets/dev/box1_windows", "/v1/targets/dev%2Fbox1_windows"])
def test_get_target_accepts_literal_or_encoded_slash(hub, path):
    payload = hub.call("GET", path).payload

    assert payload["id"] == BOX1
    assert payload["listener_info"]["user"] == "operator"


# ---- running -------------------------------------------------------------------


def test_run_get_and_read_output_through_rest(hub):
    created = hub.call("POST", "/v1/jobs", {"target": BOX1, "language": "bash", "code": "hostname"})

    assert created.status == 200
    job = created.payload
    assert job["state"] == "succeeded" and job["stdout"] == "ran bash: hostname\n"
    fetched = hub.call("GET", f"/v1/jobs/{job['job_id']}?wait_s=0").payload
    assert fetched["job_id"] == job["job_id"] and fetched["stdout"] == ""
    again = hub.call("GET", f"/v1/jobs/{job['job_id']}?wait_s=0&stdout_offset=0").payload
    assert again["stdout"] == "ran bash: hostname\n"
    page = hub.call("GET", f"/v1/jobs/{job['job_id']}/output?stream=stdout&offset=4&limit=4").payload
    assert page["data"] == "bash"
    listed = hub.call("GET", "/v1/jobs?target=dev/box1_windows").payload
    assert listed["jobs"][0]["job_id"] == job["job_id"]


def test_raw_bodies_short_names_and_inferred_languages(tmp_path):
    hub = Hub(tmp_path, {"dev/box2_windows": FakeListener(hostname="BUILD-AGENT")})

    by_machine = hub.call("POST", "/v1/jobs?target=box2", "Get-Date").payload
    assert (by_machine["target"], by_machine["language"]) == ("dev/box2_windows", "powershell")
    assert by_machine["stdout"] == "ran powershell: Get-Date\n"
    by_host = hub.call("POST", "/v1/jobs?target=build-agent&language=python", "print(1)").payload
    assert (by_host["target"], by_host["language"]) == ("dev/box2_windows", "python")

    ambiguous = hub.call("POST", "/v1/jobs?target=box1", "hostname")
    assert ambiguous.status == 404 and "ambiguous" in ambiguous.payload["error"]["message"]
    assert ambiguous.payload["error"]["details"]["candidates"] == ["contoso-lab/box1_windows", BOX1]


def test_raw_body_rules(hub):
    url = f"/v1/jobs?target={BOX1}&language=bash"

    assert hub.call("POST", url, b"\xef\xbb\xbfecho bom").payload["stdout"] == "ran bash: echo bom\n"
    assert hub.call("POST", url, "echo é".encode("utf-16")).payload["stdout"] == "ran bash: echo é\n"
    assert (
        hub.call("POST", url, b"\xc3")
        .payload["error"]["message"]
        .startswith("the request body must be UTF-8")
    )
    both = hub.call("POST", url + "&code=x", "y")
    assert both.status == 400 and "not both" in both.payload["error"]["message"]
    empty = hub.call("POST", url)
    assert empty.status == 400 and "as the request body" in empty.payload["error"]["message"]
    blank_target = hub.call("POST", "/v1/jobs?target=", "hostname")
    assert blank_target.status == 404 and "must not be empty" in blank_target.payload["error"]["message"]
    # JSON bodies still work next to query arguments.
    assert hub.call("POST", url, {"code": "uname"}).payload["stdout"] == "ran bash: uname\n"
    sniffed = hub.call("POST", f"/v1/jobs?target={BOX1}", b'{"code": "whoami", "language": "bash"}')
    assert sniffed.payload["stdout"] == "ran bash: whoami\n"
    duplicate = hub.call("POST", url, {"target": BOX1, "code": "x"})
    assert (
        duplicate.status == 400
        and "both in the query string and in the body" in duplicate.payload["error"]["message"]
    )


def test_format_text_renders_results_and_errors(hub):
    response = hub.call("POST", f"/v1/jobs?target={BOX1}&format=text", "hostname")

    assert response.status == 200 and response.media_type().startswith("text/plain")
    assert response.text.startswith("job ") and "(powershell): succeeded, exit 0" in response.text
    assert response.text.endswith("\nran powershell: hostname\n")
    error = hub.call("GET", "/v1/jobs/nope?format=text")
    assert error.status == 404 and error.text.startswith("error (not_found): unknown job 'nope'")
    assert hub.call("GET", "/v1/targets?format=yaml").status == 400


def test_stream_true_writes_output_as_it_is_produced(tmp_path):
    script = Scripted([("stdout", b"one\n"), ("stderr", b"oops\n"), ("stdout", b"two")])
    hub = Hub(tmp_path, {BOX1: FakeListener(run=script)})

    response = hub.call("POST", f"/v1/jobs?stream=true&target={BOX1}&language=bash", "work")
    assert response.status == 200 and response.media_type().startswith("text/plain")
    writes = follow(response)

    header = writes.get(timeout=5)
    assert (
        header == f"[privy] job={response.headers['X-Privy-Job-Id']} target={BOX1} language=bash\n".encode()
    )
    for expected in (b"one\n", b"oops\n", b"two"):
        script.step()
        assert writes.get(timeout=5) == expected
    script.gate.release()
    tail = b""
    while (chunk := writes.get(timeout=5)) is not None:
        tail += chunk
    assert tail.startswith(b"\n[privy] job=") and b" state=succeeded exit=0 duration=" in tail


def test_job_streams_replay_finished_jobs(hub):
    job = hub.call("POST", "/v1/jobs", {"target": BOX1, "code": "hostname"}).payload

    _, body = hub.stream("GET", f"/v1/jobs/{job['job_id']}/stream")

    lines = body.decode().splitlines()
    assert lines[0] == f"[privy] job={job['job_id']} target={BOX1} language=powershell"
    assert lines[1] == "ran powershell: hostname"
    assert lines[2].startswith(f"[privy] job={job['job_id']} state=succeeded exit=0 duration=")
    assert hub.stream("GET", f"/v1/jobs/{job['job_id']}?stream=true")[1] == body
    missing = hub.call("GET", "/v1/jobs/nope/stream")
    assert missing.status == 404 and missing.stream is None
    assert hub.call("GET", f"/v1/jobs/{job['job_id']}/stream?wait_s=3").status == 400


def test_streams_mark_one_gap_for_output_the_hub_dropped(tmp_path):
    def chatty(request, on_output, on_submit, listener):
        on_submit("remote-1")
        for index in range(6):
            on_output("stdout", f"line{index}\n".encode())
        return exec_result()

    hub = Hub(tmp_path, {BOX1: FakeListener(run=chatty)}, output_limit_bytes=10)
    job = hub.call("POST", "/v1/jobs", {"target": BOX1, "code": "chatty"}).payload

    _, body = hub.stream("GET", f"/v1/jobs/{job['job_id']}/stream")

    lines = body.decode().splitlines()
    assert lines[1:4] == ["[privy] 26 earlier bytes of output are no longer kept by the hub", "ne4", "line5"]
    assert body.count(b"no longer kept") == 1


def test_a_stream_that_loses_its_client_leaves_the_job_running(tmp_path):
    listener = FakeListener(run=blocking_run)
    hub = Hub(tmp_path, {BOX1: listener})
    response = hub.call("POST", f"/v1/jobs?stream=true&target={BOX1}", "Start-Sleep 60")

    def disconnected(data):
        raise ConnectionResetError("the client went away")

    with pytest.raises(ConnectionResetError):
        response.stream(disconnected)
    assert listener.started.wait(5)
    job_id = response.headers["X-Privy-Job-Id"]
    assert hub.jobs.get(job_id).state == "running"
    assert hub.call("POST", f"/v1/jobs/{job_id}/cancel").payload["state"] == "cancelled"


def test_running_jobs_return_a_next_step_and_can_be_cancelled(tmp_path):
    listener = FakeListener(run=blocking_run)
    hub = Hub(tmp_path, {BOX1: listener})

    started = hub.call(
        "POST", "/v1/jobs", {"target": BOX1, "language": "python", "code": "import time", "wait_s": 0}
    ).payload
    assert started["done"] is False and "get_job" in started["next_step"]
    assert listener.started.wait(5)

    cancelled = hub.call("POST", f"/v1/jobs/{started['job_id']}/cancel").payload

    assert cancelled["state"] == "cancelled"
    assert listener.cancelled == ["remote-blocking"]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"target": BOX1, "language": "ruby", "code": "x"}, 400, "invalid_argument"),
        ({"target": BOX1, "language": "bash"}, 400, "invalid_argument"),
        ({"target": BOX1, "language": "bash", "code": "x", "extra": 1}, 400, "invalid_argument"),
        ({"target": BOX1, "language": "bash", "code": "x", "mode": "inprocess"}, 400, "invalid_argument"),
        ({"target": BOX1, "language": "bash", "code": "   "}, 400, "invalid_argument"),
        ({"target": BOX1, "language": "bash", "code": "x", "wait_s": 500}, 400, "invalid_argument"),
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
    wrong_method = hub.call("DELETE", "/v1/jobs")
    assert wrong_method.status == 405 and wrong_method.headers["Allow"] == "GET, POST"
    assert hub.call("POST", "/").status == 405
    assert hub.call("GET", "/v2/nothing").status == 404
    assert hub.call("GET", "/v1/jobs/unknown").status == 404
    assert hub.call("POST", f"/v1/jobs?stream=maybe&target={BOX1}", "x").status == 400


def test_text_with_unpaired_surrogates_is_rejected(hub):
    body = b'{"target": "dev/box1_windows", "language": "python", "code": "print(\\"\\ud800\\")"}'

    response = hub.call("POST", "/v1/jobs", body)

    assert response.status == 400
    assert response.payload["error"]["code"] == "invalid_argument"
    assert "unpaired surrogate" in response.payload["error"]["message"]
    assert hub.jobs.counts()["retained"] == 0


# ---- broadcasts and files -----------------------------------------------------------


def test_broadcast_skips_targets_that_are_not_online(hub):
    payload = hub.call(
        "POST", "/v1/broadcasts", {"targets": ["dev/*"], "code": "hostname", "wait_s": 5}
    ).payload

    assert payload["done"] is True
    assert [(job["target"], job["language"]) for job in payload["jobs"]] == [(BOX1, "powershell")]
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
    assert hub.call("POST", "/v1/broadcasts", {"targets": ["nomatch*"], "code": "x"}).status == 404


def test_broadcast_takes_raw_code_and_comma_separated_patterns(hub):
    response = hub.call("POST", "/v1/broadcasts?targets=dev/box1*,gpu1_linux&format=text", "uname")

    assert response.status == 200
    assert response.text.startswith("broadcast b")
    assert "== dev/box1_windows (powershell): succeeded" in response.text
    assert "== contoso-lab/gpu1_linux (bash): succeeded" in response.text
    assert "ran bash: uname" in response.text


def test_broadcast_with_no_online_targets_reports_the_skips(tmp_path):
    hub = Hub(
        tmp_path,
        {BOX1: FakeListener(info_error=OFFLINE), "dev/box2_windows": FakeListener(info_error=OFFLINE)},
    )

    response = hub.call("POST", "/v1/broadcasts", {"targets": ["dev/*"], "code": "true"})

    assert response.status == 200
    payload = response.payload
    assert payload["jobs"] == [] and payload["done"] is True
    assert [item["target"] for item in payload["skipped"]] == [BOX1, "dev/box2_windows"]
    assert hub.call("GET", f"/v1/broadcasts/{payload['broadcast_id']}").status == 200


def test_file_transfers_take_json_or_query_arguments(tmp_path):
    def transfer(direction, source, destination, overwrite, progress):
        return TransferResult(direction, str(source), str(destination), 3, "b" * 64, "t")

    hub = Hub(tmp_path, {BOX1: FakeListener(transfer=transfer)})
    source = tmp_path / "source.txt"
    source.write_text("abc", encoding="utf-8")

    upload = hub.call(
        "POST",
        "/v1/files/upload",
        {"target": BOX1, "local_path": str(source), "remote_path": "/tmp/source.txt"},
    ).payload
    assert (upload["state"], upload["type"], upload["result"]["size"]) == ("succeeded", "upload", 3)

    local = quote(str(tmp_path / "x"))
    download = hub.call(
        "POST", f"/v1/files/download?target={BOX1}&remote_path=/tmp/x&local_path={local}"
    ).payload
    assert (download["state"], download["type"]) == ("succeeded", "download")

    relative = hub.call(
        "POST", "/v1/files/upload", {"target": BOX1, "local_path": "source.txt", "remote_path": "/x"}
    )
    assert relative.status == 400
    missing = hub.call(
        "POST",
        "/v1/files/upload",
        {"target": BOX1, "local_path": str(tmp_path / "missing"), "remote_path": "/x"},
    )
    assert missing.status == 404 and missing.payload["error"]["code"] == "local_file_not_found"
    no_dir = hub.call(
        "POST",
        "/v1/files/download",
        {"target": BOX1, "remote_path": "/x", "local_path": str(tmp_path / "no" / "x")},
    )
    assert no_dir.status == 404


# ---- console labels ------------------------------------------------------------------


def test_call_labels_show_what_was_asked():
    describe = HubApp.describe

    assert describe("GET", "/v1/targets?format=text", b"") == "REST GET /v1/targets?format=text"
    assert describe("POST", "/v1/jobs?target=dev%2Fbox1", b"Get-Date\nhostname") == (
        'REST POST /v1/jobs?target=dev/box1 "Get-Date\\nhostname"'
    )
    assert describe("POST", "/v1/jobs", b'{"target": "box1", "code": "x"}') == (
        'REST POST /v1/jobs {"target":"box1","code":"x"}'
    )
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "run", "arguments": {"code": "x"}},
    }
    assert describe("POST", "/mcp", json.dumps(call).encode()) == 'MCP tools/call run {"code":"x"}'
    assert describe("POST", "/mcp", b"[{}, {}]") == "MCP batch of 2 message(s)"
    assert describe("GET", "/v1/jobs/a%0Ab", b"") == "REST GET /v1/jobs/a\\nb"


# ---- the real HTTP server ----------------------------------------------------------------


def _serve(runtime) -> threading.Thread:
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    return thread


def _messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "privy.hub"]


def test_build_hub_serves_http_without_credentials(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="privy.hub")
    home = tmp_path / "home"
    home.mkdir()
    (home / LEGACY_TOKEN_FILENAME).write_text("token from hub 0.2", encoding="utf-8")
    runtime = build_hub(home=home, port=0, version="test")
    thread = _serve(runtime)
    try:
        assert read_state(home)["url"] == runtime.url
        assert requests.get(f"{runtime.url}/healthz", timeout=5).json()["status"] == "ok"
        targets = requests.get(f"{runtime.url}/v1/targets", timeout=5)
        assert targets.status_code == 200 and targets.json()["targets"] == []
        quickstart = requests.get(f"{runtime.url}/", timeout=5)
        assert (
            quickstart.headers["Content-Type"].startswith("text/plain") and "# privy hub" in quickstart.text
        )

        mcp = requests.post(
            f"{runtime.url}/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            timeout=5,
        )
        assert mcp.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}

        host, port = runtime.server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.putrequest("POST", "/v1/jobs")
        connection.putheader("Content-Length", str(9 * 1024 * 1024))
        connection.endheaders()
        assert connection.getresponse().status == 413
        connection.close()
    finally:
        runtime.shutdown()
        thread.join(5)
    assert wait_for(lambda: any(m.startswith("REST POST /v1/jobs -> 413") for m in _messages(caplog)))
    messages = _messages(caplog)
    assert any(message.startswith("REST GET /v1/targets -> 200 (") for message in messages)
    assert any(message.startswith("MCP ping -> 200 (") for message in messages)


def test_streams_reach_http_clients_as_output_is_produced(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="privy.hub")
    script = Scripted([("stdout", b"tick 1\n"), ("stdout", b"tick 2\n")])
    listener = FakeListener(run=script)
    home = tmp_path / "home"
    home.mkdir()
    write_relays(home / "relays.json")
    runtime = build_hub(
        home=home, port=0, version="test", client_factory=lambda target: FakeRelayClient(listener)
    )
    thread = _serve(runtime)
    try:
        url = f"{runtime.url}/v1/jobs?stream=true&target={BOX1}"
        with requests.post(url, data=b"loop", stream=True, timeout=10) as response:
            assert response.headers["Transfer-Encoding"] == "chunked"
            chunks = response.iter_content(chunk_size=None)
            assert next(chunks).startswith(b"[privy] job=")
            script.step()
            assert next(chunks) == b"tick 1\n"
            script.step()
            assert next(chunks) == b"tick 2\n"
            script.gate.release()
            assert b" state=succeeded exit=0 " in b"".join(chunks)
    finally:
        runtime.shutdown()
        thread.join(5)
    assert wait_for(lambda: any(" -> 200 stream job=" in message for message in _messages(caplog)))
    messages = _messages(caplog)
    run_box = next(message for message in messages if "▶ RUN" in message)
    assert "relay=dev (contoso-relay)  hyco=box1_windows" in run_box and "loop" in run_box
    result_box = next(message for message in messages if "◀ RESULT" in message)
    assert "✓ succeeded  exit=0" in result_box and "tick 2" in result_box
    call = next(message for message in messages if " -> 200 stream job=" in message)
    assert call.startswith(f'REST POST /v1/jobs?stream=true&target={BOX1} "loop" -> 200 stream job=')
