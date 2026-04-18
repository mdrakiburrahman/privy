"""HTTP reverse proxy through Azure Relay.

Server side (Fabric): receives ProxyRequest, forwards to a local HTTP
target, returns the response through the relay's native response framing.

Client side (laptop): tiny HTTP server that wraps browser requests as
ProxyRequest JSON, sends to relay, unwraps the response back to the browser.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

import requests as req_lib

from privy._relay import create_http_send_url, create_sas_token, fqdn

log = logging.getLogger("privy.proxy")

# ── Wire types ──────────────────────────────────────────────────────

PROXY_KIND = "http_proxy"


@dataclass
class ProxyRequest:
    """Browser HTTP request serialized for relay transport."""

    method: str
    path: str
    headers: dict[str, str]
    body_b64: str = ""
    kind: str = PROXY_KIND

    def to_json(self) -> str:
        return json.dumps({
            "kind": self.kind,
            "method": self.method,
            "path": self.path,
            "headers": self.headers,
            "body_b64": self.body_b64,
        })

    @classmethod
    def from_json(cls, raw: str | bytes) -> ProxyRequest:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        obj: dict[str, Any] = json.loads(raw)
        return cls(
            method=obj["method"],
            path=obj["path"],
            headers=obj.get("headers", {}),
            body_b64=obj.get("body_b64", ""),
        )


@dataclass
class ProxyResponse:
    """Upstream HTTP response serialized for relay transport."""

    status: int
    headers: dict[str, str]
    body_b64: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "kind": PROXY_KIND,
            "status": self.status,
            "headers": self.headers,
            "body_b64": self.body_b64,
        })

    @classmethod
    def from_json(cls, raw: str | bytes) -> ProxyResponse:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        obj: dict[str, Any] = json.loads(raw)
        return cls(
            status=obj["status"],
            headers=obj.get("headers", {}),
            body_b64=obj.get("body_b64", ""),
        )


# ── Server-side: forward to local target ────────────────────────────

def handle_proxy_request(proxy_req: ProxyRequest, target: str) -> ProxyResponse:
    """Forward a ProxyRequest to *target* and return a ProxyResponse."""
    url = f"{target}{proxy_req.path}"
    body = base64.b64decode(proxy_req.body_b64) if proxy_req.body_b64 else None

    try:
        http_req = urllib.request.Request(
            url,
            data=body,
            headers=proxy_req.headers,
            method=proxy_req.method,
        )
        # Remove hop-by-hop headers that shouldn't be forwarded
        for h in ("Host", "Transfer-Encoding", "Connection"):
            if h in http_req.headers:
                del http_req.headers[h]

        resp = urllib.request.urlopen(http_req, timeout=55)
        resp_body = resp.read()
        resp_headers = {k: v for k, v in resp.getheaders()}
        return ProxyResponse(
            status=resp.status,
            headers=resp_headers,
            body_b64=base64.b64encode(resp_body).decode("ascii"),
        )
    except urllib.error.HTTPError as e:
        resp_body = e.read() if e.fp else b""
        resp_headers = {k: v for k, v in e.headers.items()} if e.headers else {}
        return ProxyResponse(
            status=e.code,
            headers=resp_headers,
            body_b64=base64.b64encode(resp_body).decode("ascii"),
        )
    except Exception as exc:
        return ProxyResponse(
            status=502,
            headers={"Content-Type": "text/plain"},
            body_b64=base64.b64encode(f"Proxy error: {exc}".encode()).decode("ascii"),
        )


# ── Client-side: local HTTP server → relay → Fabric ────────────────

class ProxyHandler(BaseHTTPRequestHandler):
    """HTTP handler that forwards requests through Azure Relay."""

    # Set by ProxyClientServer before starting
    relay_namespace: str = ""
    relay_path: str = ""
    relay_keyrule: str = ""
    relay_key: str = ""

    def _proxy(self, method: str) -> None:
        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        # Build ProxyRequest
        headers = {k: v for k, v in self.headers.items()}
        proxy_req = ProxyRequest(
            method=method,
            path=self.path,
            headers=headers,
            body_b64=base64.b64encode(body).decode("ascii") if body else "",
        )

        # Send to relay
        ns = fqdn(self.relay_namespace)
        token = create_sas_token(ns, self.relay_path, self.relay_keyrule, self.relay_key)
        url = create_http_send_url(ns, self.relay_path, token)

        try:
            r = req_lib.post(
                url,
                headers={"Content-Type": "application/json"},
                data=proxy_req.to_json(),
                timeout=60,
            )
            r.raise_for_status()
            proxy_resp = ProxyResponse.from_json(r.text)
        except Exception as exc:
            self.send_error(502, f"Relay error: {exc}")
            return

        # Send response to browser
        self.send_response(proxy_resp.status)
        for k, v in proxy_resp.headers.items():
            # Skip hop-by-hop headers
            if k.lower() in ("transfer-encoding", "connection", "keep-alive"):
                continue
            self.send_header(k, v)
        # Add CORS headers for local dev
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        resp_body = base64.b64decode(proxy_resp.body_b64) if proxy_resp.body_b64 else b""
        self.wfile.write(resp_body)

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def do_PATCH(self):
        self._proxy("PATCH")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, format, *args):
        log.debug(format, *args)


class ProxyClientServer:
    """Local HTTP server that proxies browser traffic through Azure Relay.

    Example::

        from privy.proxy import ProxyClientServer
        proxy = ProxyClientServer(
            namespace="mdrrahman-dev-relay",
            path="demo",
            keyrule="demo-listen-send",
            key="...",
            local_port=3000,
        )
        proxy.serve_forever()
        # Open http://localhost:3000 in your browser
    """

    def __init__(
        self,
        *,
        namespace: str,
        path: str,
        keyrule: str,
        key: str,
        local_port: int = 3000,
    ) -> None:
        ProxyHandler.relay_namespace = namespace
        ProxyHandler.relay_path = path
        ProxyHandler.relay_keyrule = keyrule
        ProxyHandler.relay_key = key
        self._port = local_port
        self._server: HTTPServer | None = None

    def serve_forever(self) -> None:
        self._server = HTTPServer(("127.0.0.1", self._port), ProxyHandler)
        log.info("Proxy listening on http://127.0.0.1:%d", self._port)
        print(f"Open http://localhost:{self._port} in your browser")
        self._server.serve_forever()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
