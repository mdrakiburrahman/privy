"""Model Context Protocol endpoint (Streamable HTTP).

The hub is a *dual-era* MCP server:

* **Modern** clients (protocol ``2026-07-28``) send every request with
  ``params._meta["io.modelcontextprotocol/protocolVersion"]`` and matching
  ``MCP-Protocol-Version`` / ``Mcp-Method`` / ``Mcp-Name`` headers. Requests are
  served statelessly; ``server/discover`` describes the server.
* **Legacy** clients (``2025-11-25`` and earlier) open with ``initialize``.
  The hub answers without minting an ``Mcp-Session-Id``: every request is
  self-contained, so no session state is needed.

Every hub operation is exposed as one tool. A ``tools/call`` that waits for
jobs and carries ``params._meta.progressToken`` (from a client accepting
``text/event-stream``) is answered as an SSE stream: ``notifications/progress``
with the newest output while the call waits, then the JSON-RPC response.
Tool results carry a plain-text rendering as ``content`` and the JSON as
``structuredContent``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from privy.hub.render import render, render_error
from privy.hub.responses import EVENT_STREAM_TYPE, HttpResponse, json_bytes
from privy.hub.service import (
    INSTRUCTIONS,
    OPERATIONS,
    OPERATIONS_BY_NAME,
    PROGRESS_OPERATIONS,
    HubError,
    HubService,
    Progress,
)

log = logging.getLogger("privy.hub")

MCP_PATH = "/mcp"
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS
SERVER_NAME = "privy-hub"

#: Caching hints required on cacheable modern results.
DISCOVER_TTL_MS = 60 * 60 * 1000
TOOLS_TTL_MS = 5 * 60 * 1000
CACHE_SCOPE = "private"

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

_BASE64_PREFIX = "=?base64?"
_BASE64_SUFFIX = "?="


class _RpcError(Exception):
    def __init__(self, code: int, message: str, *, status: int = 200, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.data = data


def _error_payload(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _decode_header_value(value: str | None) -> str | None:
    """Undo the ``=?base64?...?=`` sentinel encoding used for non-ASCII header values."""
    if value is None:
        return None
    if value.startswith(_BASE64_PREFIX) and value.endswith(_BASE64_SUFFIX):
        encoded = value[len(_BASE64_PREFIX) : -len(_BASE64_SUFFIX)]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise _RpcError(
                HEADER_MISMATCH, "Header mismatch: malformed base64 header value", status=400
            ) from None
    return value


def tool_definitions() -> list[dict[str, Any]]:
    """MCP tool descriptions, in a deterministic order."""
    return [
        {
            "name": operation.name,
            "title": operation.title,
            "description": operation.description,
            "inputSchema": operation.input_schema(),
            "annotations": {
                "title": operation.title,
                "readOnlyHint": operation.read_only,
                "destructiveHint": operation.destructive,
                "idempotentHint": operation.idempotent,
                "openWorldHint": True,
            },
        }
        for operation in OPERATIONS
    ]


def _protocol_version(params: dict[str, Any]) -> Any:
    meta = params.get("_meta")
    return meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None


def _progress_token(message: Any, headers: Mapping[str, str]) -> str | int | None:
    """The progress token of a tool call that waits for jobs, when the client accepts SSE."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return None
    if message.get("method") != "tools/call" or "id" not in message:
        return None
    if "text/event-stream" not in (headers.get("Accept") or "").lower():
        return None
    params = message.get("params")
    if not isinstance(params, dict) or params.get("name") not in PROGRESS_OPERATIONS:
        return None
    meta = params.get("_meta")
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    if isinstance(token, bool) or not isinstance(token, (str, int)):
        return None
    return token


def _sse(payload: Any) -> bytes:
    return b"event: message\ndata: " + json_bytes(payload) + b"\n\n"


def _outcome(response: dict[str, Any] | None) -> str | None:
    """What the console line says about a JSON-RPC response beyond its HTTP status."""
    if not response:
        return None
    if "error" in response:
        return f"rpc error {response['error'].get('code')}"
    result = response.get("result")
    if isinstance(result, dict) and result.get("isError"):
        error = (result.get("structuredContent") or {}).get("error") or {}
        return f"isError {error.get('code', '')}".rstrip()
    return None


class McpEndpoint:
    """Serve JSON-RPC messages POSTed to :data:`MCP_PATH`."""

    def __init__(self, service: HubService) -> None:
        self.service = service
        self._tools = tool_definitions()

    def handle(self, method: str, headers: Mapping[str, str], body: bytes) -> HttpResponse:
        if method != "POST":
            payload = _error_payload(None, INVALID_REQUEST, "The MCP endpoint only accepts POST")
            return HttpResponse(405, payload, {"Allow": "POST"})
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return HttpResponse(400, _error_payload(None, PARSE_ERROR, "Parse error"))

        if isinstance(message, list):
            # JSON-RPC batches were allowed by protocol 2025-03-26; they get no progress.
            responses = [
                response for _, response in (self._message(item, headers) for item in message) if response
            ]
            if not responses:
                return HttpResponse(202)
            return HttpResponse(200, responses)
        token = _progress_token(message, headers)
        if token is not None:
            return self._streamed(message, headers, token)
        status, response = self._message(message, headers)
        if response is None:
            return HttpResponse(202)
        return HttpResponse(status, response, note=_outcome(response))

    def _streamed(
        self, message: dict[str, Any], headers: Mapping[str, str], token: str | int
    ) -> HttpResponse:
        """Answer a tool call as SSE: progress notifications while it waits, then the response."""
        params = message["params"]
        version = _protocol_version(params)
        if version is not None:
            try:
                self._check_modern("tools/call", params, version, headers)
            except _RpcError as exc:
                return HttpResponse(exc.status, _error_payload(message["id"], exc.code, str(exc), exc.data))
        response = HttpResponse(200, content_type=EVENT_STREAM_TYPE, note="stream")

        def write_events(write: Callable[[bytes], None]) -> None:
            last = 0

            def progress(text: str, done: int | None = None, total: int | None = None) -> None:
                nonlocal last
                # progress must grow with every notification, including heartbeats between byte counts.
                last = done if done is not None and done > last else last + 1
                update: dict[str, Any] = {"progressToken": token, "progress": last}
                if total is not None:
                    update["total"] = total
                if text:
                    update["message"] = text
                write(_sse({"jsonrpc": "2.0", "method": "notifications/progress", "params": update}))

            _, final = self._message(message, headers, progress)
            outcome = _outcome(final)
            response.note = f"stream {outcome}" if outcome else "stream"
            write(_sse(final))

        response.stream = write_events
        return response

    def _message(
        self,
        message: Any,
        headers: Mapping[str, str],
        progress: Progress | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return 400, _error_payload(None, INVALID_REQUEST, "Invalid Request")
        if "method" not in message or "id" not in message:
            # Notifications (and stray responses) need no answer.
            return 202, None
        request_id = message["id"]
        method = message["method"]
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(method, str) or not isinstance(params, dict):
            return 400, _error_payload(request_id, INVALID_REQUEST, "Invalid Request")
        try:
            version = _protocol_version(params)
            log.debug("MCP %s (%s)", method, f"modern {version}" if version is not None else "legacy")
            if version is not None:
                result = self._modern(method, params, version, headers, progress)
            else:
                result = self._legacy(method, params, progress)
        except _RpcError as exc:
            return exc.status, _error_payload(request_id, exc.code, str(exc), exc.data)
        except ConnectionError:
            # The client of a progress stream went away; the hub's job keeps running.
            raise
        except Exception:  # noqa: BLE001 - never leak a traceback to the client
            log.exception("MCP %s failed", method)
            return 500, _error_payload(request_id, INTERNAL_ERROR, "Internal error")
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}

    # ---- eras ---------------------------------------------------------------

    @staticmethod
    def _check_modern(method: str, params: dict[str, Any], version: Any, headers: Mapping[str, str]) -> None:
        header_version = headers.get("MCP-Protocol-Version")
        if header_version != version:
            raise _RpcError(
                HEADER_MISMATCH,
                f"Header mismatch: MCP-Protocol-Version {header_version!r} does not match body {version!r}",
                status=400,
            )
        header_method = headers.get("Mcp-Method")
        if header_method != method:
            raise _RpcError(
                HEADER_MISMATCH,
                f"Header mismatch: Mcp-Method {header_method!r} does not match body {method!r}",
                status=400,
            )
        if method == "tools/call":
            header_name = _decode_header_value(headers.get("Mcp-Name"))
            if header_name != params.get("name"):
                raise _RpcError(
                    HEADER_MISMATCH,
                    f"Header mismatch: Mcp-Name {header_name!r} does not match body {params.get('name')!r}",
                    status=400,
                )
        if version not in MODERN_VERSIONS:
            raise _RpcError(
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                status=400,
                data={"supported": list(SUPPORTED_VERSIONS), "requested": version},
            )

    def _modern(
        self,
        method: str,
        params: dict[str, Any],
        version: Any,
        headers: Mapping[str, str],
        progress: Progress | None,
    ) -> dict[str, Any]:
        self._check_modern(method, params, version, headers)
        if method == "server/discover":
            result = {**self._discover(), "ttlMs": DISCOVER_TTL_MS, "cacheScope": CACHE_SCOPE}
        elif method == "tools/list":
            result = {"tools": self._tools, "ttlMs": TOOLS_TTL_MS, "cacheScope": CACHE_SCOPE}
        elif method == "tools/call":
            result = self._call_tool(params, progress)
        elif method == "ping":
            result = {}
        else:
            raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method}", status=404)
        return {"resultType": "complete", **result}

    def _legacy(self, method: str, params: dict[str, Any], progress: Progress | None) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            negotiated = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
            return {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": self._server_info(),
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self._tools}
        if method == "tools/call":
            return self._call_tool(params, progress)
        if method == "server/discover":
            return self._discover()
        raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    # ---- methods ------------------------------------------------------------

    def _server_info(self) -> dict[str, str]:
        return {"name": SERVER_NAME, "title": "privy hub", "version": self.service.version}

    def _discover(self) -> dict[str, Any]:
        return {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}},
            "_meta": {META_SERVER_INFO: self._server_info()},
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, params: dict[str, Any], progress: Progress | None = None) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in OPERATIONS_BY_NAME:
            raise _RpcError(INVALID_PARAMS, f"Unknown tool: {name!r}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        try:
            if not isinstance(arguments, dict):
                raise HubError("arguments must be an object", code="invalid_argument")
            result = self.service.invoke(name, arguments, origin="mcp", progress=progress)
        except HubError as exc:
            error = exc.to_dict()
            return {
                "content": [{"type": "text", "text": render_error(error)}],
                "structuredContent": {"error": error},
                "isError": True,
            }
        return {
            "content": [{"type": "text", "text": render(name, result, transport="mcp")}],
            "structuredContent": result,
            "isError": False,
        }
