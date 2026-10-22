import base64
import json
import queue
import threading
import time

import pytest
from hub_fakes import FakeListener, Hub, Scripted, blocking_run

from privy.hub.mcp import (
    HEADER_MISMATCH,
    INVALID_PARAMS,
    LEGACY_VERSIONS,
    METHOD_NOT_FOUND,
    MODERN_VERSIONS,
    PARSE_ERROR,
    UNSUPPORTED_PROTOCOL_VERSION,
    tool_definitions,
)
from privy.hub.service import OPERATIONS

MODERN = MODERN_VERSIONS[0]
SSE = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path, {"dev/box1_windows": FakeListener(hostname="BOX1")})


def legacy(hub, method, params=None, request_id=1, headers=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return hub.call("POST", "/mcp", message, headers=headers)


def modern(hub, method, params=None, *, version=MODERN, headers=None, request_id=1, meta=None):
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
        **(meta or {}),
    }
    request_headers = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if method == "tools/call":
        request_headers["Mcp-Name"] = params["name"]
    request_headers.update(headers or {})
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    return hub.call("POST", "/mcp", message, headers=request_headers)


def sse_events(body: bytes) -> list[dict]:
    return [
        json.loads(line[len("data: ") :]) for line in body.decode().splitlines() if line.startswith("data: ")
    ]


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


def test_tool_definitions_mirror_operations():
    tools = tool_definitions()

    assert [tool["name"] for tool in tools] == [operation.name for operation in OPERATIONS]
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        assert tool["description"]
    annotations = {tool["name"]: tool["annotations"] for tool in tools}
    assert annotations["list_targets"]["readOnlyHint"] is True
    assert annotations["run"]["destructiveHint"] is True


def test_legacy_handshake_negotiates_a_supported_version(hub):
    answer = legacy(hub, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

    result = answer.payload["result"]
    assert answer.status == 200
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "privy-hub"
    assert "list_targets" in result["instructions"]
    assert "Mcp-Session-Id" not in answer.headers

    future = legacy(hub, "initialize", {"protocolVersion": "2099-01-01"}).payload["result"]
    assert future["protocolVersion"] == LEGACY_VERSIONS[0]


def test_legacy_notifications_are_accepted_without_a_body(hub):
    response = hub.call("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert response.status == 202 and response.payload is None


def test_legacy_tools_list_and_call(hub):
    tools = legacy(hub, "tools/list").payload["result"]["tools"]
    assert len(tools) == len(OPERATIONS)

    called = legacy(hub, "tools/call", {"name": "list_targets", "arguments": {"relay": "dev"}}).payload[
        "result"
    ]
    assert called["isError"] is False
    assert "resultType" not in called
    structured = called["structuredContent"]
    assert structured["targets"][0]["host"] == "BOX1"
    # The model reads a compact rendering; the JSON stays in structuredContent.
    text = called["content"][0]["text"]
    assert text.startswith("2 of 2 machines online (relays: dev)") and "BOX1" in text

    ran = legacy(
        hub,
        "tools/call",
        {"name": "run", "arguments": {"target": "dev/box1_windows", "language": "bash", "code": "whoami"}},
    ).payload["result"]
    assert ran["structuredContent"]["stdout"] == "ran bash: whoami\n"
    ran_text = ran["content"][0]["text"]
    assert "(bash): succeeded, exit 0" in ran_text and ran_text.endswith("\nran bash: whoami\n")


def test_tool_errors_are_returned_to_the_model(hub):
    failed = legacy(
        hub,
        "tools/call",
        {"name": "run", "arguments": {"target": "dev/nope", "language": "bash", "code": "x"}},
    ).payload["result"]

    assert failed["isError"] is True
    assert failed["structuredContent"]["error"]["code"] == "target_not_found"
    assert failed["content"][0]["text"].startswith("error (target_not_found): unknown target 'dev/nope'")
    bad_args = legacy(hub, "tools/call", {"name": "get_job", "arguments": []}).payload["result"]
    assert bad_args["isError"] is True


def test_unknown_tools_and_methods_are_protocol_errors(hub):
    unknown_tool = legacy(hub, "tools/call", {"name": "format_disk"}).payload
    assert unknown_tool["error"]["code"] == INVALID_PARAMS
    assert legacy(hub, "resources/list").payload["error"]["code"] == METHOD_NOT_FOUND
    assert legacy(hub, "ping").payload["result"] == {}


def test_modern_discover_and_stateless_tool_calls(hub):
    discover = modern(hub, "server/discover")
    result = discover.payload["result"]
    assert discover.status == 200
    assert result["resultType"] == "complete"
    assert result["supportedVersions"][0] == MODERN
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "privy-hub"

    listed = modern(hub, "tools/list").payload["result"]
    assert listed["resultType"] == "complete" and len(listed["tools"]) == len(OPERATIONS)

    called = modern(hub, "tools/call", {"name": "get_target", "arguments": {"target": "dev/box1_windows"}})
    assert called.payload["result"]["resultType"] == "complete"
    assert called.payload["result"]["structuredContent"]["status"] == "online"


def test_modern_mcp_name_may_be_base64_encoded(hub):
    encoded = "=?base64?" + base64.b64encode(b"list_targets").decode() + "?="

    response = modern(hub, "tools/call", {"name": "list_targets"}, headers={"Mcp-Name": encoded})

    assert response.status == 200 and response.payload["result"]["isError"] is False


@pytest.mark.parametrize(
    "headers",
    [
        {"MCP-Protocol-Version": "2025-11-25"},
        {"Mcp-Method": "tools/list"},
        {"Mcp-Name": "run"},
        {"Mcp-Name": "=?base64?not-base64!?="},
    ],
)
def test_modern_header_mismatches_are_rejected(hub, headers):
    response = modern(hub, "tools/call", {"name": "list_targets"}, headers=headers)

    assert response.status == 400
    assert response.payload["error"]["code"] == HEADER_MISMATCH


def test_modern_unsupported_versions_list_what_is_supported(hub):
    response = modern(hub, "tools/list", version="2099-01-01")

    assert response.status == 400
    error = response.payload["error"]
    assert error["code"] == UNSUPPORTED_PROTOCOL_VERSION
    assert error["data"]["requested"] == "2099-01-01"
    assert MODERN in error["data"]["supported"] and "2025-11-25" in error["data"]["supported"]


def test_modern_unknown_methods_are_404(hub):
    response = modern(hub, "resources/list")

    assert response.status == 404 and response.payload["error"]["code"] == METHOD_NOT_FOUND


def test_modern_cacheable_results_carry_cache_hints(hub):
    listed = modern(hub, "tools/list").payload["result"]
    discovered = modern(hub, "server/discover").payload["result"]

    assert listed["ttlMs"] > 0 and listed["cacheScope"] == "private"
    assert discovered["ttlMs"] > 0 and discovered["cacheScope"] == "private"
    assert "ttlMs" not in legacy(hub, "tools/list").payload["result"]


def test_transport_level_errors(hub):
    get = hub.call("GET", "/mcp")
    assert get.status == 405 and get.headers["Allow"] == "POST"
    assert hub.call("DELETE", "/mcp").status == 405
    parse = hub.call("POST", "/mcp", b"{oops")
    assert parse.status == 400 and parse.payload["error"]["code"] == PARSE_ERROR
    assert hub.call("POST", "/mcp", {"id": 1, "method": "ping"}).status == 400


def test_legacy_batches_answer_requests_only(hub):
    batch = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 7, "method": "ping"},
    ]

    response = hub.call("POST", "/mcp", batch)

    assert response.status == 200
    assert response.payload == [{"jsonrpc": "2.0", "id": 7, "result": {}}]


def _run_call(request_id, arguments, token="tok"):
    params = {"name": "run", "arguments": arguments, "_meta": {"progressToken": token}}
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


def test_progress_tokens_stream_live_output_before_the_result(tmp_path):
    script = Scripted([("stdout", b"one\n"), ("stdout", b"two\n")])
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(run=script)})

    response = hub.call(
        "POST",
        "/mcp",
        _run_call(9, {"target": "dev/box1_windows", "code": "loop", "wait_s": 10}),
        headers=SSE,
    )
    assert response.status == 200 and response.media_type().startswith("text/event-stream")
    writes = follow(response)

    script.step()
    [first] = sse_events(writes.get(timeout=5))
    assert first["method"] == "notifications/progress"
    assert first["params"]["progressToken"] == "tok" and first["params"]["message"] == "one"
    script.step()
    [second] = sse_events(writes.get(timeout=5))
    assert second["params"]["message"] == "two"
    assert second["params"]["progress"] > first["params"]["progress"]
    script.gate.release()
    rest = []
    while (chunk := writes.get(timeout=5)) is not None:
        rest.extend(sse_events(chunk))
    final = rest[-1]
    assert final["id"] == 9 and final["result"]["isError"] is False
    assert final["result"]["structuredContent"]["stdout"] == "one\ntwo\n"
    assert response.note == "stream"


def test_progress_needs_an_sse_client_and_a_tool_that_waits(hub):
    arguments = {"target": "dev/box1_windows", "code": "x"}
    plain = hub.call("POST", "/mcp", _run_call(1, arguments))
    assert plain.stream is None and plain.payload["result"]["isError"] is False

    listing = {"name": "list_targets", "_meta": {"progressToken": 1}}
    listed = hub.call(
        "POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": listing}, headers=SSE
    )
    assert listed.stream is None and listed.payload["result"]["isError"] is False

    streamed, body = hub.stream("POST", "/mcp", _run_call(3, arguments, token=7), headers=SSE)
    events = sse_events(body)
    assert streamed.stream is not None
    assert events[-1]["id"] == 3 and events[-1]["result"]["structuredContent"]["state"] == "succeeded"


def test_streamed_tool_errors_and_modern_validation(hub):
    failed, body = hub.stream("POST", "/mcp", _run_call(4, {"target": "dev/nope", "code": "x"}), headers=SSE)
    [final] = sse_events(body)
    assert final["result"]["isError"] is True and failed.note == "stream isError target_not_found"

    arguments = {"target": "dev/box1_windows", "code": "x"}
    good = modern(
        hub, "tools/call", {"name": "run", "arguments": arguments}, meta={"progressToken": "t"}, headers=SSE
    )
    chunks = []
    good.stream(chunks.append)
    result = sse_events(b"".join(chunks))[-1]["result"]
    assert result["resultType"] == "complete" and result["structuredContent"]["state"] == "succeeded"

    mismatch = modern(
        hub,
        "tools/call",
        {"name": "run", "arguments": arguments},
        meta={"progressToken": "t"},
        headers={**SSE, "Mcp-Name": "get_job"},
    )
    assert mismatch.status == 400 and mismatch.stream is None
    assert mismatch.payload["error"]["code"] == HEADER_MISMATCH


def test_quiet_jobs_send_heartbeats_but_not_right_away(tmp_path, monkeypatch):
    monkeypatch.setattr("privy.hub.service.HEARTBEAT_S", 0.4)
    listener = FakeListener(run=blocking_run)
    hub = Hub(tmp_path, {"dev/box1_windows": listener})
    arguments = {"target": "dev/box1_windows", "code": "sleep", "wait_s": 1.0}

    started = time.monotonic()
    writes = follow(hub.call("POST", "/mcp", _run_call(5, arguments), headers=SSE))
    timeline = []
    while (chunk := writes.get(timeout=5)) is not None:
        timeline.extend((time.monotonic() - started, event) for event in sse_events(chunk))
    listener.cancel_event.set()

    messages = [(at, event["params"]["message"]) for at, event in timeline if "method" in event]
    assert messages[0][1] == "started"
    heartbeats = [at for at, message in messages if message.startswith("still running")]
    assert heartbeats and heartbeats[0] >= 0.35
    assert timeline[-1][1]["result"]["structuredContent"]["state"] == "running"
