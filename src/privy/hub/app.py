"""Loopback HTTP server for the hub: authentication, host/origin checks, and routing."""

from __future__ import annotations

import hmac
import json
import logging
import os
import socket
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from privy.hub.config import RelaysStore
from privy.hub.home import (
    AUDIT_FILENAME,
    RELAYS_FILENAME,
    ensure_home,
    load_or_create_token,
    write_state,
)
from privy.hub.jobs import DEFAULT_MAX_RUNNING, AuditLog, JobManager
from privy.hub.mcp import MCP_PATH, McpEndpoint
from privy.hub.registry import DEFAULT_PROBE_TTL_S, TargetRegistry
from privy.hub.rest import RestApi
from privy.hub.service import HubService

log = logging.getLogger("privy.hub")

MAX_BODY_BYTES = 8 * 1024 * 1024
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class Headers(Mapping[str, str]):
    """Case-insensitive, read-only view of request headers."""

    def __init__(self, items: Iterable[tuple[str, str]]) -> None:
        self._values = {name.lower(): value for name, value in items}

    def __getitem__(self, name: str) -> str:
        return self._values[name.lower()]

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name.lower(), default)

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@dataclass
class HttpResponse:
    status: int
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def body(self) -> bytes:
        if self.payload is None:
            return b""
        text = json.dumps(self.payload, ensure_ascii=False)
        try:
            return text.encode("utf-8")
        except UnicodeEncodeError:
            # An unpaired surrogate cannot be UTF-8 encoded; ASCII escapes always can.
            return json.dumps(self.payload).encode("ascii")


class HubApp:
    """Everything the hub does with one HTTP request, independent of sockets."""

    def __init__(
        self,
        service: HubService,
        *,
        token: str,
        port: int,
        allowed_origins: Iterable[str] = (),
        extra_hosts: Iterable[str] = (),
    ) -> None:
        self.service = service
        self.rest = RestApi(service)
        self.mcp = McpEndpoint(service)
        self._token = token.encode("utf-8")
        self._allowed_origins = {origin.rstrip("/").lower() for origin in allowed_origins}
        self._extra_hosts = [host.lower() for host in extra_hosts]
        self._allowed_hosts: set[str] = set()
        self.set_port(port)

    def set_port(self, port: int) -> None:
        self.port = port
        hosts = [*_LOOPBACK_HOSTS, *self._extra_hosts]
        self._allowed_hosts = {f"{host}:{port}" for host in hosts}
        self.rest.base_url = f"http://127.0.0.1:{port}"

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> HttpResponse:
        origin = headers.get("Origin")
        cors = origin is not None and origin.rstrip("/").lower() in self._allowed_origins
        if cors and method == "OPTIONS" and self._host_allowed(headers):
            # Browsers preflight cross-origin requests without credentials.
            return HttpResponse(204, None, self._cors_headers(origin, preflight=True))
        response = self._route(method, target, headers, body)
        if cors:
            response.headers.update(self._cors_headers(origin))
        return response

    def _host_allowed(self, headers: Mapping[str, str]) -> bool:
        return (headers.get("Host") or "").strip().lower() in self._allowed_hosts

    @staticmethod
    def _cors_headers(origin: str, *, preflight: bool = False) -> dict[str, str]:
        headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        if preflight:
            headers.update(
                {
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": (
                        "Authorization, Content-Type, Accept, MCP-Protocol-Version, Mcp-Method, Mcp-Name"
                    ),
                    "Access-Control-Max-Age": "600",
                }
            )
        return headers

    def _route(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> HttpResponse:
        parsed = urlsplit(target)
        path = parsed.path or "/"
        if not self._host_allowed(headers):
            return self._error(
                403, "forbidden_host", "requests must address the hub by its loopback host and port"
            )
        origin = headers.get("Origin")
        if origin is not None and origin.rstrip("/").lower() not in self._allowed_origins:
            # Browsers send Origin; refusing it blocks drive-by and DNS-rebinding requests.
            return self._error(403, "forbidden_origin", f"origin {origin!r} is not allowed")
        if path == "/healthz":
            if method not in ("GET", "HEAD"):
                return self._error(405, "method_not_allowed", "use GET", {"Allow": "GET, HEAD"})
            return HttpResponse(
                200, {"status": "ok", "service": "privy-hub", "version": self.service.version}
            )
        if not self._authorized(headers.get("Authorization")):
            response = self._error(401, "unauthorized", "send 'Authorization: Bearer <token from hub-token>'")
            response.headers["WWW-Authenticate"] = 'Bearer realm="privy-hub"'
            return response
        if path == MCP_PATH:
            status, payload, extra = self.mcp.handle(method, headers, body)
            return HttpResponse(status, payload, extra)
        query = parse_qs(parsed.query, keep_blank_values=True)
        status, payload, extra = self.rest.handle(method, path, query, body)
        return HttpResponse(status, payload, extra)

    def _authorized(self, header: str | None) -> bool:
        if not header:
            return False
        scheme, _, value = header.strip().partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(value.strip().encode("utf-8"), self._token)

    @staticmethod
    def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> HttpResponse:
        return HttpResponse(status, {"error": {"code": code, "message": message}}, dict(headers or {}))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "privy-hub"
    sys_version = ""
    server: HubHTTPServer

    def _dispatch(self) -> None:
        if self.headers.get("Transfer-Encoding"):
            self._send(
                HubApp._error(411, "length_required", "send Content-Length; chunked bodies are not supported")
            )
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(
                HubApp._error(
                    413, "payload_too_large", f"request bodies are limited to {MAX_BODY_BYTES} bytes"
                )
            )
            self.close_connection = True
            return
        body = self.rfile.read(length) if length else b""
        started = time.monotonic()
        try:
            response = self.server.app.handle(self.command, self.path, Headers(self.headers.items()), body)
            payload = response.body()
        except Exception:  # noqa: BLE001 - keep the server alive
            log.exception("unhandled error for %s %s", self.command, self.path)
            response = HubApp._error(500, "internal_error", "internal error; see the hub log")
            payload = response.body()
        self._send(response, payload)
        log.debug(
            "%s %s -> %d (%d ms)",
            self.command,
            urlsplit(self.path).path,
            response.status,
            (time.monotonic() - started) * 1000,
        )

    def _send(self, response: HttpResponse, body: bytes | None = None) -> None:
        body = response.body() if body is None else body
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

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
        host, port = self.server.server_address[:2]
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
    allowed_origins: Iterable[str] = (),
    version: str,
    client_factory: Any = None,
) -> HubRuntime:
    """Create the hub's components and bind its HTTP server (without serving yet)."""
    ensure_home(home)
    relays_path = relays_file or home / RELAYS_FILENAME
    token = load_or_create_token(home)
    store = RelaysStore(relays_path)
    store.snapshot()
    registry = TargetRegistry(store, client_factory=client_factory, probe_ttl_s=probe_ttl_s)
    jobs = JobManager(registry, max_running=max_jobs, audit=AuditLog(home / AUDIT_FILENAME))
    service = HubService(registry=registry, jobs=jobs, version=version)
    extra_hosts = [] if host in ("127.0.0.1", "localhost", "::1") else [host]
    app = HubApp(service, token=token, port=port, allowed_origins=allowed_origins, extra_hosts=extra_hosts)
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
