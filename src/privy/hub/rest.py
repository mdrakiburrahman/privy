"""REST routes, the capabilities document, and the generated OpenAPI description."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from privy.hub.mcp import MCP_PATH, SUPPORTED_VERSIONS
from privy.hub.service import INSTRUCTIONS, OPERATIONS_BY_NAME, HubError, HubService

_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class Route:
    """A REST endpoint backed by one hub operation."""

    method: str
    path: str
    operation: str
    source: str

    @property
    def path_params(self) -> list[str]:
        return _PATH_PARAM_RE.findall(self.path)

    @property
    def regex(self) -> re.Pattern[str]:
        def group(match: re.Match[str]) -> str:
            name = match.group(1)
            # Target ids contain '/', so {target} swallows the rest of the path.
            return f"(?P<{name}>.+)" if name == "target" else f"(?P<{name}>[^/]+)"

        return re.compile(
            _PATH_PARAM_RE.sub(group, re.escape(self.path).replace(r"\{", "{").replace(r"\}", "}"))
        )


ROUTES: tuple[Route, ...] = (
    Route("GET", "/v1/targets", "list_targets", "query"),
    Route("GET", "/v1/targets/{target}", "get_target", "query"),
    Route("POST", "/v1/jobs", "run", "body"),
    Route("GET", "/v1/jobs", "list_jobs", "query"),
    Route("GET", "/v1/jobs/{job_id}", "get_job", "query"),
    Route("GET", "/v1/jobs/{job_id}/output", "read_job_output", "query"),
    Route("POST", "/v1/jobs/{job_id}/cancel", "cancel_job", "body"),
    Route("POST", "/v1/broadcasts", "broadcast", "body"),
    Route("GET", "/v1/broadcasts/{broadcast_id}", "get_broadcast", "query"),
    Route("POST", "/v1/files/upload", "upload_file", "body"),
    Route("POST", "/v1/files/download", "download_file", "body"),
)

_COMPILED = tuple((route, route.regex) for route in ROUTES)


def _error(status: int, code: str, message: str, details: Any = None) -> tuple[int, dict[str, Any]]:
    return status, {"error": HubError(message, status=status, code=code, details=details).to_dict()}


class RestApi:
    """Dispatch ``/v1`` requests to :class:`HubService` operations."""

    def __init__(self, service: HubService) -> None:
        self.service = service
        self.base_url = "http://127.0.0.1"

    def handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        body: bytes,
    ) -> tuple[int, Any, dict[str, str]]:
        if path in ("/v1", "/v1/"):
            if method != "GET":
                return 405, {"error": {"code": "method_not_allowed", "message": "use GET"}}, {"Allow": "GET"}
            return 200, self.capabilities(), {}
        if path == "/openapi.json":
            if method != "GET":
                return 405, {"error": {"code": "method_not_allowed", "message": "use GET"}}, {"Allow": "GET"}
            return 200, openapi_document(self.service.version, self.base_url), {}

        allowed: list[str] = []
        for route, regex in _COMPILED:
            match = regex.fullmatch(path)
            if match is None:
                continue
            if route.method != method:
                allowed.append(route.method)
                continue
            try:
                args = self._arguments(route, match.groupdict(), query, body)
                return 200, self.service.invoke(route.operation, args, origin="rest"), {}
            except HubError as exc:
                return exc.status, {"error": exc.to_dict()}, {}
        if allowed:
            status, payload = _error(405, "method_not_allowed", f"use {' or '.join(sorted(set(allowed)))}")
            return status, payload, {"Allow": ", ".join(sorted(set(allowed)))}
        status, payload = _error(404, "not_found", f"no route for {method} {path}; GET /v1 lists them")
        return status, payload, {}

    @staticmethod
    def _arguments(
        route: Route,
        path_values: Mapping[str, str],
        query: Mapping[str, list[str]],
        body: bytes,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {}
        operation = OPERATIONS_BY_NAME[route.operation]
        if route.source == "query":
            arrays = {param.name for param in operation.params if param.type == "array"}
            for name, values in query.items():
                args[name] = values if name in arrays else values[-1]
        else:
            if query:
                raise HubError("send arguments as a JSON body, not query parameters", code="invalid_argument")
            if body.strip():
                try:
                    decoded = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HubError(f"request body is not valid JSON: {exc}", code="invalid_json") from None
                if not isinstance(decoded, dict):
                    raise HubError("request body must be a JSON object", code="invalid_json")
                args.update(decoded)
        for name, value in path_values.items():
            if name in args:
                raise HubError(
                    f"{name} is taken from the URL path; do not repeat it", code="invalid_argument"
                )
            args[name] = unquote(value)
        return args

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "privy-hub",
            "version": self.service.version,
            "description": "Local control plane for privy listeners reachable through Azure Relay.",
            "authentication": "Send 'Authorization: Bearer <token>'. The token is the hub-token file in "
            "the hub home directory.",
            "mcp": {
                "endpoint": MCP_PATH,
                "transport": "streamable-http",
                "protocol_versions": list(SUPPORTED_VERSIONS),
                "tools": [route.operation for route in ROUTES],
            },
            "openapi": "/openapi.json",
            "health": "/healthz",
            "operations": [
                {
                    "name": route.operation,
                    "title": OPERATIONS_BY_NAME[route.operation].title,
                    "method": route.method,
                    "path": route.path,
                    "read_only": OPERATIONS_BY_NAME[route.operation].read_only,
                }
                for route in ROUTES
            ],
            "instructions": INSTRUCTIONS,
            "status": self.service.summary(),
        }


def openapi_document(version: str, base_url: str) -> dict[str, Any]:
    """OpenAPI 3.1 description generated from the operation declarations."""
    paths: dict[str, dict[str, Any]] = {}
    for route in ROUTES:
        operation = OPERATIONS_BY_NAME[route.operation]
        path_names = set(route.path_params)
        parameters: list[dict[str, Any]] = []
        for param in operation.params:
            if param.name in path_names:
                description = param.description
                if param.name == "target":
                    description += " The '/' may be sent literally or as %2F."
                parameters.append(
                    {
                        "name": param.name,
                        "in": "path",
                        "required": True,
                        "description": description,
                        "schema": {"type": "string"},
                    }
                )
            elif route.source == "query":
                schema = param.json_schema()
                description = schema.pop("description")
                entry: dict[str, Any] = {
                    "name": param.name,
                    "in": "query",
                    "required": param.required,
                    "description": description,
                    "schema": schema,
                }
                if param.type == "array":
                    entry.update(style="form", explode=True)
                parameters.append(entry)
        spec: dict[str, Any] = {
            "operationId": operation.name,
            "summary": operation.title,
            "description": operation.description,
            "responses": {
                "200": {
                    "description": "Success",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
                "default": {"$ref": "#/components/responses/Error"},
            },
        }
        if parameters:
            spec["parameters"] = parameters
        if route.source == "body":
            body_params = [param for param in operation.params if param.name not in path_names]
            if body_params:
                spec["requestBody"] = {
                    "required": any(param.required for param in body_params),
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {param.name: param.json_schema() for param in body_params},
                                "required": [param.name for param in body_params if param.required],
                                "additionalProperties": False,
                            }
                        }
                    },
                }
        paths.setdefault(route.path, {})[route.method.lower()] = spec

    paths["/v1"] = {
        "get": {
            "operationId": "capabilities",
            "summary": "Describe the hub",
            "description": "Endpoints, MCP details, usage instructions, and current status.",
            "responses": {"200": {"description": "Capabilities document"}},
        }
    }
    paths["/healthz"] = {
        "get": {
            "operationId": "health",
            "summary": "Liveness check",
            "security": [],
            "responses": {"200": {"description": "The hub is running"}},
        }
    }
    paths[MCP_PATH] = {
        "post": {
            "operationId": "mcp",
            "summary": "Model Context Protocol endpoint (Streamable HTTP)",
            "description": "JSON-RPC 2.0. Exposes every operation above as an MCP tool.",
            "responses": {"200": {"description": "JSON-RPC response"}, "202": {"description": "Accepted"}},
        }
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "privy hub", "version": version, "description": INSTRUCTIONS},
        "servers": [{"url": base_url}],
        "security": [{"bearer": []}],
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "Error": {
                    "type": "object",
                    "properties": {
                        "error": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "message": {"type": "string"},
                                "details": {},
                            },
                            "required": ["code", "message"],
                        }
                    },
                    "required": ["error"],
                }
            },
            "responses": {
                "Error": {
                    "description": "Error",
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
                }
            },
        },
        "paths": paths,
    }
