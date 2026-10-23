"""HTTP server for the hub: routing, live streams, and one console line per call.

The hub has no authentication: anyone who can reach its address can run code on
every target with forged local headers. It binds to loopback by default and
checks Host and Origin to block DNS rebinding and cross-origin browser calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from privy.hub import console
from privy.hub.config import RelaysStore
from privy.hub.home import AUDIT_FILENAME, RELAYS_FILENAME, ensure_home, write_state
from privy.hub.jobs import DEFAULT_MAX_RUNNING, AuditLog, JobManager
from privy.hub.mcp import MCP_PATH, McpEndpoint
from privy.hub.registry import DEFAULT_PROBE_TTL_S, TargetRegistry
from privy.hub.responses import HttpResponse, error_response
from privy.hub.rest import RestApi
from privy.hub.service import HubService

__all__ = ["Headers", "HttpResponse", "HubApp", "HubHTTPServer", "HubRuntime", "build_hub"]

log = logging.getLogger("privy.hub")

MAX_BODY_BYTES = 8 * 1024 * 1024
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
_UNSPECIFIED_HOSTS = ("0.0.0.0", "::", "")
_LABEL_CHARS = 300
_LOCAL_AUTHORITY_RE = re.compile(r"(localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?", re.IGNORECASE)


class Headers(Mapping[str, str]):
    """Case-insensitive, read-only view of request headers."""

    def __init__(self, items: Iterable[tuple[str, str]]) -> None:
        self._all: dict[str, list[str]] = {}
        for name, value in items:
            self._all.setdefault(name.lower(), []).append(value)
        self._values = {name: values[-1] for name, values in self._all.items()}

    def get_all(self, name: str) -> list[str]:
        return list(self._all.get(name.lower(), ()))

    def __getitem__(self, name: str) -> str:
        return self._values[name.lower()]

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name.lower(), default)

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _printable(text: str, limit: int = _LABEL_CHARS) -> str:
    text = "".join(char if char.isprintable() else ascii(char)[1:-1] for char in text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe_body(body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    try:
        return console.compact_json(json.loads(text))
    except ValueError:
        return console.compact_json(text)


def _describe_mcp(body: bytes) -> str:
    try:
        message = json.loads(body.decode("utf-8"))
    except ValueError:
        return "(unparseable body)"
    if isinstance(message, list):
        return f"batch of {len(message)} message(s)"
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return "(not a request)"
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if message["method"] == "tools/call":
        arguments = params.get("arguments")
        return (
            f"tools/call {_printable(str(params.get('name')))} "
            f"{console.compact_json(arguments if arguments is not None else {})}"
        )
    return _printable(message["method"])


def _local_authority(value: str, port: int) -> tuple[str, int] | None:
    match = _LOCAL_AUTHORITY_RE.fullmatch(value)
    if match is None:
        return None
    actual_port = int(match[2]) if match[2] is not None else 80
    if actual_port != port:
        return None
    return match[1].lower(), actual_port


class HubApp:
    """Everything the hub does with one HTTP request, independent of sockets."""

    def __init__(self, service: HubService, *, port: int) -> None:
        self.service = service
        self.rest = RestApi(service)
        self.mcp = McpEndpoint(service)
        self.set_port(port)

    def set_port(self, port: int) -> None:
        self.port = port
        self.rest.base_url = f"http://127.0.0.1:{port}"

    def validate_request(self, target: str, headers: Mapping[str, str]) -> HttpResponse | None:
        """Block rebinding and cross-origin browsers, not ordinary local HTTP clients."""
        request_headers = headers if isinstance(headers, Headers) else Headers(headers.items())
        hosts = request_headers.get_all("Host")
        authority = _local_authority(hosts[0], self.port) if len(hosts) == 1 else None
        if authority is None:
            return error_response(403, "forbidden_host", "Host must be a loopback host on the hub's port")
        origins = request_headers.get_all("Origin")
        if origins:
            match = re.fullmatch(r"http://(.+)", origins[0], re.IGNORECASE)
            if len(origins) != 1 or match is None or _local_authority(match[1], self.port) != authority:
                return error_response(403, "forbidden_origin", "Origin must match the hub request's origin")
        sites = request_headers.get_all("Sec-Fetch-Site")
        if len(sites) > 1 or (sites and sites[0].strip().lower() == "cross-site"):
            return error_response(403, "forbidden_origin", "Cross-site browser requests are not allowed")
        try:
            parsed = urlsplit(target)
        except ValueError:
            return error_response(403, "forbidden_host", "Invalid request target")
        if (
            any(ord(char) <= 32 or ord(char) == 127 for char in target)
            or parsed.fragment
            or target.startswith("//")
            or (
                not target.startswith("/")
                and (parsed.scheme != "http" or _local_authority(parsed.netloc, self.port) != authority)
            )
        ):
            return error_response(403, "forbidden_host", "Request target must match the hub request's origin")
        return None

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> HttpResponse:
        headers = headers if isinstance(headers, Headers) else Headers(headers.items())
        rejected = self.validate_request(target, headers)
        if rejected is not None:
            return rejected
        parsed = urlsplit(target)
        path = parsed.path or "/"
        if path == "/healthz":
            if method not in ("GET", "HEAD"):
                return error_response(405, "method_not_allowed", "use GET", {"Allow": "GET, HEAD"})
            return HttpResponse(
                200, {"status": "ok", "service": "privy-hub", "version": self.service.version}
            )
        if path == MCP_PATH:
            return self.mcp.handle(method, headers, body)
        query = parse_qs(parsed.query, keep_blank_values=True)
        return self.rest.handle(method, path, query, headers, body)

    @staticmethod
    def describe(method: str, target: str, body: bytes) -> str:
        """The console label of a call: what was asked, with its arguments."""
        try:
            path = urlsplit(target).path or "/"
        except ValueError:
            path = None
        if method == "POST" and path == MCP_PATH:
            return "MCP " + _describe_mcp(body)
        label = f"REST {method} {_printable(unquote(target))}"
        return f"{label} {_describe_body(body)}" if body else label

    def log_call(self, method: str, target: str, body: bytes, response: HttpResponse, seconds: float) -> None:
        outcome = f"{response.status} {response.note}" if response.note else str(response.status)
        console.call(self.describe(method, target, body), outcome, seconds)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "privy-hub"
    sys_version = ""
    # Live output must not wait for Nagle's algorithm to coalesce small writes.
    disable_nagle_algorithm = True
    server: HubHTTPServer

    def _dispatch(self) -> None:
        started = time.monotonic()
        body, response = self._read_and_handle()
        try:
            if response.stream is not None:
                self._send_stream(response)
            else:
                self._send(response)
        except ConnectionError:
            # The caller went away (Ctrl-C on curl, a client timeout); jobs keep running.
            response.note = (
                f"{response.note}, client disconnected" if response.note else "client disconnected"
            )
            self.close_connection = True
        except Exception:  # noqa: BLE001 - keep the server alive
            log.exception("failed to answer %s %s", self.command, self.path)
            response.note = f"{response.note}, failed" if response.note else "failed"
            self.close_connection = True
        self.server.app.log_call(self.command, self.path, body, response, time.monotonic() - started)

    def _read_and_handle(self) -> tuple[bytes, HttpResponse]:
        headers = Headers(self.headers.items())
        rejected = self.server.app.validate_request(self.path, headers)
        if rejected is not None:
            self.close_connection = True
            rejected.headers["Connection"] = "close"
            return b"", rejected
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return b"", error_response(
                411, "length_required", "send Content-Length; chunked request bodies are not supported"
            )
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            return b"", error_response(
                413, "payload_too_large", f"request bodies are limited to {MAX_BODY_BYTES} bytes"
            )
        body = self.rfile.read(length) if length else b""
        try:
            return body, self.server.app.handle(self.command, self.path, headers, body)
        except Exception:  # noqa: BLE001 - keep the server alive
            log.exception("unhandled error for %s %s", self.command, self.path)
            return body, error_response(500, "internal_error", "internal error; see the hub console")

    def _send(self, response: HttpResponse) -> None:
        body = response.body()
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if body:
            self.send_header("Content-Type", response.media_type())
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _send_stream(self, response: HttpResponse) -> None:
        """Write a streamed body as it is produced (chunked for HTTP/1.1 clients)."""
        assert response.stream is not None
        chunked = self.request_version == "HTTP/1.1"
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", response.media_type())
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

        def write(data: bytes) -> None:
            if data:
                self.wfile.write(b"%X\r\n%s\r\n" % (len(data), data) if chunked else data)

        response.stream(write)
        if chunked:
            self.wfile.write(b"0\r\n\r\n")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _dispatch

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler API
        log.debug("http %s - %s", self.address_string(), format % args)


class HubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let another process bind the same port.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], app: HubApp) -> None:
        self.app = app
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


@dataclass
class HubRuntime:
    """A constructed hub: its service, HTTP server, and where it keeps state."""

    service: HubService
    server: HubHTTPServer
    app: HubApp
    home: Path
    relays_file: Path

    @property
    def url(self) -> str:
        """Where a client on this machine reaches the hub."""
        host, port = self.server.server_address[:2]
        if host in _UNSPECIFIED_HOSTS:
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    def serve_forever(self) -> None:
        self.server.serve_forever(poll_interval=0.5)

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def build_hub(
    *,
    home: Path,
    host: str = "127.0.0.1",
    port: int,
    relays_file: Path | None = None,
    max_jobs: int = DEFAULT_MAX_RUNNING,
    probe_ttl_s: float = DEFAULT_PROBE_TTL_S,
    version: str,
    client_factory: Any = None,
) -> HubRuntime:
    """Create the hub's components and bind its HTTP server (without serving yet)."""
    ensure_home(home)
    relays_path = relays_file or home / RELAYS_FILENAME
    store = RelaysStore(relays_path)
    store.snapshot()
    registry = TargetRegistry(store, client_factory=client_factory, probe_ttl_s=probe_ttl_s)
    jobs = JobManager(registry, max_running=max_jobs, audit=AuditLog(home / AUDIT_FILENAME))
    service = HubService(registry=registry, jobs=jobs, version=version)
    app = HubApp(service, port=port)
    server = HubHTTPServer((host, port), app)
    app.set_port(server.server_address[1])
    runtime = HubRuntime(service=service, server=server, app=app, home=home, relays_file=relays_path)
    write_state(
        home,
        {
            "url": runtime.url,
            "pid": os.getpid(),
            "version": version,
            "started_at": service.started_at,
            "relays_file": str(relays_path),
        },
    )
    return runtime
