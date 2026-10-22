import base64
import json

import pytest
from hub_fakes import FakeListener, Hub

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


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path, {"dev/box1_windows": FakeListener(hostname="BOX1")})


def legacy(hub, method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return hub.call("POST", "/mcp", message)


def modern(hub, method, params=None, *, version=MODERN, headers=None, request_id=1):
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    request_headers = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if method == "tools/call":
        request_headers["Mcp-Name"] = params["name"]
    request_headers.update(headers or {})
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    return hub.call("POST", "/mcp", message, headers=request_headers)


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
    assert structured["targets"][0]["listener"]["hostname"] == "BOX1"
    assert json.loads(called["content"][0]["text"]) == structured

    ran = legacy(
        hub,
        "tools/call",
        {"name": "run", "arguments": {"target": "dev/box1_windows", "language": "bash", "code": "whoami"}},
    ).payload["result"]
    assert ran["structuredContent"]["stdout"] == "ran bash: whoami\n"


def test_tool_errors_are_returned_to_the_model(hub):
    failed = legacy(
        hub,
        "tools/call",
        {"name": "run", "arguments": {"target": "dev/nope", "language": "bash", "code": "x"}},
    ).payload["result"]

    assert failed["isError"] is True
    assert failed["structuredContent"]["error"]["code"] == "target_not_found"
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
