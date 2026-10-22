"""REST routes, the LLM quickstart, and the generated OpenAPI description.

Every ``/v1`` operation takes its arguments as query parameters, a JSON body, or
both. ``POST /v1/jobs`` and ``POST /v1/broadcasts`` also accept the code as the
raw request body when ``target`` (``targets``) is in the query string: the
easiest form to type in a shell. ``format=text`` answers with a plain-text
rendering instead of JSON, and ``stream=true`` streams a job's output live.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from privy.hub import guide, render
from privy.hub.jobs import HubJob
from privy.hub.mcp import MCP_PATH, SUPPORTED_VERSIONS
from privy.hub.responses import TEXT_TYPE, HttpResponse, error_response
from privy.hub.service import INSTRUCTIONS, OPERATIONS, OPERATIONS_BY_NAME, HubError, HubService, Param

#: How often a live stream re-checks a job that produces no output.
STREAM_POLL_S = 1.0
#: The one route that is not a service operation: a job's output as a live text stream.
STREAM_JOB = "stream_job"

_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")
_HOST_RE = re.compile(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9A-Fa-f:.]+\](:\d{1,5})?")
_TRUE = ("true", "1", "yes", "on", "")
_FALSE = ("false", "0", "no", "off")


@dataclass(frozen=True)
class Route:
    """A REST endpoint backed by one hub operation."""

    method: str
    path: str
    operation: str
    #: Query argument that, when present, makes the request body the code to run.
    raw_anchor: str | None = None
    #: Whether ``stream=true`` turns the answer into a live stream of the job's output.
    streams: bool = False

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
    Route("GET", "/v1/targets", "list_targets"),
    Route("GET", "/v1/targets/{target}", "get_target"),
    Route("POST", "/v1/jobs", "run", raw_anchor="target", streams=True),
    Route("GET", "/v1/jobs", "list_jobs"),
    Route("GET", "/v1/jobs/{job_id}", "get_job", streams=True),
    Route("GET", "/v1/jobs/{job_id}/stream", STREAM_JOB),
    Route("GET", "/v1/jobs/{job_id}/output", "read_job_output"),
    Route("POST", "/v1/jobs/{job_id}/cancel", "cancel_job"),
    Route("POST", "/v1/broadcasts", "broadcast", raw_anchor="targets"),
    Route("GET", "/v1/broadcasts/{broadcast_id}", "get_broadcast"),
    Route("POST", "/v1/files/upload", "upload_file"),
    Route("POST", "/v1/files/download", "download_file"),
)

_COMPILED = tuple((route, route.regex) for route in ROUTES)
_STREAM_TITLE = "Stream a job's output live"
_STREAM_DESCRIPTION = (
    "Follow a job like a local process: replays its output from the start, streams new stdout and "
    "stderr as they are produced (interleaved, in arrival order), and ends with a "
    "'[privy] job=<id> state=<state> exit=<code> duration=<s>s' line when the job finishes. "
    "Disconnecting does not stop the job."
)


def _invalid(message: str) -> HubError:
    return HubError(message, code="invalid_argument")


def _last(query: Mapping[str, list[str]], name: str, default: str) -> str:
    values = query.get(name)
    return values[-1] if values else default


def _flag(name: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise _invalid(f"{name} must be true or false")


def _is_json(headers: Mapping[str, str]) -> bool:
    content_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    return content_type == "application/json" or content_type.endswith("+json")


def _decode_code(body: bytes) -> str:
    """The request body as text: UTF-8 (with or without BOM), or UTF-16 with a BOM."""
    try:
        if body.startswith((b"\xff\xfe", b"\xfe\xff")):
            return body.decode("utf-16")
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _invalid(f"the request body must be UTF-8 text ({exc})") from None


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        decoded = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _method_not_allowed(methods: list[str]) -> HttpResponse:
    allowed = sorted(set(methods))
    return error_response(
        405, "method_not_allowed", f"use {' or '.join(allowed)}", {"Allow": ", ".join(allowed)}
    )


# ---- live streams -------------------------------------------------------------


def _stream_header(job: HubJob) -> str:
    what = f"language={job.spec['language']}" if job.type == "exec" else f"type={job.type}"
    return f"[privy] job={job.id} target={job.target.id} {what}\n"


def _stream_trailer(job: HubJob) -> str:
    summary = job.summary()
    trailer = f"[privy] job={job.id} state={summary['state']}"
    if "exit_code" in summary:
        trailer += f" exit={summary['exit_code']}"
    if "duration_ms" in summary:
        trailer += f" duration={summary['duration_ms'] / 1000:.1f}s"
    if summary.get("error"):
        trailer += f" error={summary['error']}"
    return trailer + "\n"


def write_job_stream(job: HubJob, write: Callable[[bytes], None], *, poll_s: float = STREAM_POLL_S) -> None:
    """Write a job's output to ``write`` as it arrives, between a header and a trailer line."""
    write(_stream_header(job).encode("utf-8"))
    index = 0
    at_line_start = True
    while True:
        pieces, index, finished = job.next_pieces(index, poll_s)
        for piece in pieces:
            if piece.skipped:
                lost = (
                    "earlier output is"
                    if piece.skipped < 0
                    else f"{piece.skipped} earlier bytes of output are"
                )
                note = f"[privy] {lost} no longer kept by the hub\n"
                write((note if at_line_start else "\n" + note).encode("utf-8"))
                at_line_start = True
            if piece.data:
                write(piece.data)
                at_line_start = piece.data.endswith(b"\n")
        if finished:
            break
    trailer = _stream_trailer(job)
    write((trailer if at_line_start else "\n" + trailer).encode("utf-8", "backslashreplace"))


def _stream_response(job: HubJob) -> HttpResponse:
    return HttpResponse(
        200,
        headers={"X-Privy-Job-Id": job.id},
        content_type=TEXT_TYPE,
        stream=lambda write: write_job_stream(job, write),
        note=f"stream job={job.id}",
    )


# ---- dispatch -----------------------------------------------------------------


class RestApi:
    """Dispatch ``/``, ``/v1``, and ``/openapi.json`` requests."""

    def __init__(self, service: HubService) -> None:
        self.service = service
        self.base_url = "http://127.0.0.1"

    def base_url_for(self, headers: Mapping[str, str]) -> str:
        """The URL the caller used to reach the hub, so examples work as written."""
        host = (headers.get("Host") or "").strip()
        return f"http://{host}" if host and _HOST_RE.fullmatch(host) else self.base_url

    def handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        headers: Mapping[str, str],
        body: bytes,
    ) -> HttpResponse:
        base_url = self.base_url_for(headers)
        documents: dict[str, Callable[[], HttpResponse]] = {
            "/": lambda: HttpResponse(200, text=guide.quickstart(self.service, base_url)),
            "/llms.txt": lambda: HttpResponse(200, text=guide.quickstart(self.service, base_url)),
            "/v1": lambda: HttpResponse(200, self.capabilities(base_url)),
            "/v1/": lambda: HttpResponse(200, self.capabilities(base_url)),
            "/openapi.json": lambda: HttpResponse(200, openapi_document(self.service, base_url)),
        }
        if path in documents:
            if method not in ("GET", "HEAD"):
                return _method_not_allowed(["GET", "HEAD"])
            return documents[path]()

        allowed: list[str] = []
        for route, regex in _COMPILED:
            match = regex.fullmatch(path)
            if match is None:
                continue
            if route.method != method:
                allowed.append(route.method)
                continue
            return self._dispatch(route, match.groupdict(), query, headers, body)
        if allowed:
            return _method_not_allowed(allowed)
        return error_response(404, "not_found", f"no route for {method} {path}; GET / explains the API")

    def _dispatch(
        self,
        route: Route,
        path_values: Mapping[str, str],
        query: Mapping[str, list[str]],
        headers: Mapping[str, str],
        body: bytes,
    ) -> HttpResponse:
        text = False
        try:
            output_format = _last(query, "format", "json").strip().lower()
            if output_format not in ("json", "text"):
                raise _invalid("format must be json or text")
            text = output_format == "text"
            stream = route.streams and _flag("stream", _last(query, "stream", "false"))
            args = self._arguments(route, path_values, query, headers, body)
            if route.operation == STREAM_JOB:
                if set(args) - {"job_id"}:
                    raise _invalid(
                        f"unknown argument(s) for stream_job: {', '.join(sorted(set(args) - {'job_id'}))}"
                    )
                return _stream_response(self.service.job(args["job_id"]))
            if stream and route.operation == "run":
                return _stream_response(self.service.start_run(args, origin="rest"))
            if stream:
                OPERATIONS_BY_NAME[route.operation].parse(args)
                return _stream_response(self.service.job(args["job_id"]))
            result = self.service.invoke(route.operation, args, origin="rest")
        except HubError as exc:
            error = exc.to_dict()
            if text:
                return HttpResponse(exc.status, text=render.render_error(error), note=exc.code)
            return HttpResponse(exc.status, {"error": error}, note=exc.code)
        if text:
            return HttpResponse(200, text=render.render(route.operation, result, transport="rest"))
        return HttpResponse(200, result)

    @staticmethod
    def _arguments(
        route: Route,
        path_values: Mapping[str, str],
        query: Mapping[str, list[str]],
        headers: Mapping[str, str],
        body: bytes,
    ) -> dict[str, Any]:
        operation = OPERATIONS_BY_NAME.get(route.operation)
        arrays = {param.name for param in operation.params if param.type == "array"} if operation else set()
        presentation = {"format", "stream"} if route.streams or route.operation == STREAM_JOB else {"format"}
        args: dict[str, Any] = {}
        for name, values in query.items():
            if name not in presentation:
                args[name] = values if name in arrays else values[-1]
        if route.method == "POST" and body.strip():
            decoded = None
            raw = route.raw_anchor is not None and route.raw_anchor in args and not _is_json(headers)
            if raw and body.lstrip().startswith(b"{"):
                # A JSON object carrying the code was clearly not meant to be run as code.
                decoded = _json_object(body)
                raw = decoded is None or "code" not in decoded
            if raw:
                if "code" in args:
                    raise _invalid("send the code as the request body or as the code parameter, not both")
                args["code"] = _decode_code(body)
            else:
                decoded = decoded if decoded is not None else _json_object(body)
                if decoded is None:
                    hint = (
                        f"; to send raw code instead, put {route.raw_anchor} in the query string"
                        if route.raw_anchor
                        else ""
                    )
                    raise HubError(f"the request body must be a JSON object{hint}", code="invalid_json")
                for name, value in decoded.items():
                    if name in args:
                        raise _invalid(f"{name} was sent both in the query string and in the body")
                    args[name] = value
        if route.raw_anchor and route.raw_anchor in args and "code" not in args:
            raise _invalid(
                f"send the code to run as the request body ({route.raw_anchor} is in the query string)"
            )
        for name, value in path_values.items():
            if name in args:
                raise _invalid(f"{name} is taken from the URL path; do not repeat it")
            args[name] = unquote(value)
        return args

    def capabilities(self, base_url: str) -> dict[str, Any]:
        return {
            "name": "privy-hub",
            "version": self.service.version,
            "description": "Run PowerShell, Bash, or Python on remote machines (privy listeners behind "
            "Azure Relay) from this computer, with live output.",
            "authentication": "none",
            "quickstart": f"{base_url}/",
            "openapi": f"{base_url}/openapi.json",
            "health": f"{base_url}/healthz",
            "mcp": {
                "endpoint": f"{base_url}{MCP_PATH}",
                "transport": "streamable-http",
                "protocol_versions": list(SUPPORTED_VERSIONS),
                "tools": [operation.name for operation in OPERATIONS],
            },
            "operations": [
                {
                    "name": route.operation,
                    "title": _title(route),
                    "method": route.method,
                    "path": route.path,
                    "read_only": route.operation == STREAM_JOB
                    or OPERATIONS_BY_NAME[route.operation].read_only,
                }
                for route in ROUTES
            ],
            "instructions": INSTRUCTIONS,
            "status": self.service.summary(),
        }


def _title(route: Route) -> str:
    return _STREAM_TITLE if route.operation == STREAM_JOB else OPERATIONS_BY_NAME[route.operation].title


# ---- OpenAPI ------------------------------------------------------------------

_FORMAT_PARAM: dict[str, Any] = {
    "name": "format",
    "in": "query",
    "required": False,
    "description": "json (default) or text: a compact plain-text answer, like a terminal would show.",
    "schema": {"type": "string", "enum": ["json", "text"], "default": "json"},
}
_STREAM_PARAM: dict[str, Any] = {
    "name": "stream",
    "in": "query",
    "required": False,
    "description": "true streams the job's output live as text/plain, like a local process: a "
    "'[privy] job=<id> ...' line, stdout and stderr as they are produced, then "
    "'[privy] job=<id> state=<state> exit=<code> duration=<s>s'. Use curl -N (no buffering).",
    "schema": {"type": "boolean", "default": False},
}


def _parameter(param: Param, location: str, *, required: bool) -> dict[str, Any]:
    schema = param.json_schema()
    description = schema.pop("description")
    entry: dict[str, Any] = {
        "name": param.name,
        "in": location,
        "required": required,
        "description": description,
        "schema": schema,
    }
    if param.type == "array":
        entry.update(style="form", explode=False)
    return entry


def _response(samples: Mapping[str, Any], *, json_body: bool = True) -> dict[str, Any]:
    content: dict[str, Any] = {}
    if json_body:
        content["application/json"] = {"schema": {"type": "object"}}
        if "response" in samples:
            content["application/json"]["example"] = samples["response"]
    content["text/plain"] = {"schema": {"type": "string"}}
    if "text" in samples:
        content["text/plain"]["example"] = samples["text"]
    return {"description": "Success", "content": content}


def _operation_spec(route: Route, base_url: str, example: guide.Example) -> dict[str, Any]:
    samples = guide.examples(route.operation, base_url, example)
    if route.operation == STREAM_JOB:
        spec: dict[str, Any] = {
            "operationId": STREAM_JOB,
            "summary": _STREAM_TITLE,
            "description": _STREAM_DESCRIPTION,
            "parameters": [
                {
                    "name": "job_id",
                    "in": "path",
                    "required": True,
                    "description": "Job id returned by run.",
                    "schema": {"type": "string"},
                }
            ],
            "responses": {
                "200": _response(samples, json_body=False),
                "default": {"$ref": "#/components/responses/Error"},
            },
        }
    else:
        operation = OPERATIONS_BY_NAME[route.operation]
        path_names = set(route.path_params)
        parameters: list[dict[str, Any]] = []
        for param in operation.params:
            if param.name in path_names:
                entry = _parameter(param, "path", required=True)
                if param.name == "target":
                    entry["description"] += " The '/' of a full id may be sent literally or as %2F."
                parameters.append(entry)
            elif route.method == "GET":
                parameters.append(_parameter(param, "query", required=param.required))
            elif param.name != "code":
                parameters.append(_parameter(param, "query", required=False))
        parameters.append(_FORMAT_PARAM)
        if route.streams:
            parameters.append(_STREAM_PARAM)
        description = operation.description
        if route.raw_anchor:
            description += (
                f"\n\nSimplest form: put {route.raw_anchor} (and any other argument) in the query string and "
                "send the code as the raw request body. Or send every argument as a JSON object "
                "(Content-Type: application/json)."
            )
        elif route.method == "POST":
            description += "\n\nArguments may be sent as query parameters, as a JSON body, or both."
        spec = {
            "operationId": operation.name,
            "summary": operation.title,
            "description": description,
            "parameters": parameters,
            "responses": {"200": _response(samples), "default": {"$ref": "#/components/responses/Error"}},
        }
        body_params = [param for param in operation.params if param.name not in path_names]
        if route.method == "POST" and body_params:
            content: dict[str, Any] = {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {param.name: param.json_schema() for param in body_params},
                        "required": [param.name for param in body_params if param.required],
                        "additionalProperties": False,
                    }
                }
            }
            if "request" in samples:
                content["application/json"]["example"] = samples["request"]
            if route.raw_anchor:
                content["text/plain"] = {
                    "schema": {
                        "type": "string",
                        "description": f"The code to run, when {route.raw_anchor} is in the query string.",
                    }
                }
                if "raw" in samples:
                    content["text/plain"]["example"] = samples["raw"]
            spec["requestBody"] = {"required": route.raw_anchor is not None, "content": content}
    code_samples = guide.code_samples(route.operation, base_url, example)
    if code_samples:
        spec["x-codeSamples"] = code_samples
    return spec


def _info_description(base_url: str, example: guide.Example) -> str:
    return (
        "Run PowerShell, Bash, or Python on remote machines from this computer and watch the output "
        "live, as if the machine were local. No authentication.\n\n"
        "Fastest start (the body is the code; the output streams back as it is produced):\n\n"
        f'    curl.exe -sN "{base_url}/v1/jobs?stream=true&target={example.machine}" '
        f"--data-binary '{example.code}'\n\n"
        f"GET {base_url}/ is a quickstart listing the machines online now. Every operation accepts "
        "format=text for a compact plain-text answer.\n\n" + INSTRUCTIONS
    )


def _document(operation_id: str, summary: str, description: str) -> dict[str, Any]:
    return {
        "get": {
            "operationId": operation_id,
            "summary": summary,
            "description": description,
            "responses": {
                "200": {"description": summary, "content": {"text/plain": {"schema": {"type": "string"}}}}
            },
        }
    }


def openapi_document(service: HubService, base_url: str) -> dict[str, Any]:
    """OpenAPI 3.1 description generated from the operation declarations, with live examples."""
    example = guide.example_for(service)
    paths: dict[str, dict[str, Any]] = {
        "/": _document(
            "quickstart",
            "Quickstart",
            "Markdown guide: the machines online now and copy-paste examples. Start here.",
        ),
        "/llms.txt": _document("llms_txt", "Quickstart (llms.txt)", "The same guide as GET /."),
    }
    for route in ROUTES:
        paths.setdefault(route.path, {})[route.method.lower()] = _operation_spec(route, base_url, example)
    paths["/v1"] = {
        "get": {
            "operationId": "capabilities",
            "summary": "Describe the hub",
            "description": "Endpoints, MCP details, usage instructions, and current status as JSON.",
            "responses": {"200": {"description": "Capabilities document"}},
        }
    }
    paths["/healthz"] = {
        "get": {
            "operationId": "health",
            "summary": "Liveness check",
            "responses": {"200": {"description": "The hub is running"}},
        }
    }
    paths[MCP_PATH] = {
        "post": {
            "operationId": "mcp",
            "summary": "Model Context Protocol endpoint (Streamable HTTP)",
            "description": "JSON-RPC 2.0. Exposes every operation as an MCP tool. Tool calls that send a "
            "progressToken and accept text/event-stream get live progress notifications.",
            "responses": {"200": {"description": "JSON-RPC response"}, "202": {"description": "Accepted"}},
        }
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "privy hub",
            "version": service.version,
            "description": _info_description(base_url, example),
        },
        "servers": [{"url": base_url}],
        "components": {
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
                    "description": "Error: JSON, or text with format=text",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Error"}},
                        "text/plain": {"schema": {"type": "string"}},
                    },
                }
            },
        },
        "paths": paths,
    }
