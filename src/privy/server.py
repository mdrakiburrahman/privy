"""Azure Relay listener that executes incoming privy requests.

Wire protocol on the listener (control) websocket mirrors the Azure Relay
Hybrid Connection pattern used in the reference ``relay-demo``:

* A sender makes an HTTPS ``connect`` POST to the Relay. Azure picks between
  two delivery modes:
    - **inline**: the control websocket receives a request frame that already
      contains ``"method": "POST"``; the body arrives as the next frame on the
      same control socket; the response is also sent on the control socket.
    - **rendezvous**: the control socket receives just ``{"request":
      {"address": "wss://…"}}``; we open that sub-websocket and the request
      frame + body arrive there; we also send the response there.
* For inline requests we serve sequentially (single control socket), but small
  inline POSTs complete very quickly. For rendezvous requests we hand work to
  a thread pool so multiple senders can execute concurrently.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import websocket

from privy._relay import create_listen_url, create_sas_token, fqdn
from privy.executor import execute
from privy.protocol import ExecRequest, ExecResponse
from privy.proxy import PROXY_KIND, ProxyRequest, ProxyResponse, handle_proxy_request


def _ensure_default_logging() -> None:
    """Attach a pretty console handler to the privy logger if nothing is set up.

    Idempotent: a flag on the logger prevents duplicate handlers on reconnect.
    """
    privy_log = logging.getLogger("privy")
    if getattr(privy_log, "_privy_handler_attached", False):
        return
    if privy_log.handlers or logging.getLogger().handlers:
        # Caller already configured logging; respect it.
        privy_log._privy_handler_attached = True  # type: ignore[attr-defined]
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    privy_log.addHandler(handler)
    privy_log.setLevel(logging.INFO)
    privy_log._privy_handler_attached = True  # type: ignore[attr-defined]


log = logging.getLogger("privy.server")

_MAX_LOG_BYTES = 4000  # truncate very long output in the console dump


def _short_id(request_id: Any) -> str:
    s = str(request_id or "-")
    return s if len(s) <= 12 else s[:8] + "…"


def _decode_for_log(data: bytes) -> str:
    if not data:
        return ""
    truncated = len(data) > _MAX_LOG_BYTES
    view = data[:_MAX_LOG_BYTES]
    text = view.decode("utf-8", "replace").rstrip("\n")
    if truncated:
        text += f"\n… [truncated {len(data) - _MAX_LOG_BYTES} more bytes]"
    return text


def _indent(text: str, prefix: str = "    ") -> str:
    if not text:
        return prefix + "(empty)"
    return "\n".join(prefix + line for line in text.splitlines())


def _log_request(req: ExecRequest, request_id: Any) -> None:
    rid = _short_id(request_id)
    code_preview = (
        req.code if len(req.code) <= _MAX_LOG_BYTES else req.code[:_MAX_LOG_BYTES] + "\n… [truncated]"
    )
    msg = (
        f"\n┌── ▶ REQUEST  {rid}  kind={req.kind}  mode={req.mode}  timeout={req.timeout_s}s\n"
        f"{_indent(code_preview, '│   ')}\n"
        f"└────────────────────────────────────────────────"
    )
    log.info(msg)


def _log_response(resp: ExecResponse, request_id: Any) -> None:
    rid = _short_id(request_id)
    status = "✓" if resp.exit_code == 0 and not resp.timed_out else "✗"
    header = (
        f"\n┌── ◀ RESPONSE {rid}  {status} exit={resp.exit_code}  "
        f"{resp.duration_ms}ms"
        + ("  timed_out" if resp.timed_out else "")
        + (f"  error={resp.error}" if resp.error else "")
    )
    parts = [header]
    stdout_text = _decode_for_log(resp.stdout)
    stderr_text = _decode_for_log(resp.stderr)
    parts.append("│ stdout:")
    parts.append(_indent(stdout_text, "│   "))
    parts.append("│ stderr:")
    parts.append(_indent(stderr_text, "│   "))
    parts.append("└────────────────────────────────────────────────")
    log.info("\n".join(parts))


class RelayServer:
    """Long-running listener that executes requests arriving via Azure Relay.

    The caller passes credentials explicitly — the class never reads files or
    environment variables.

    Example (Fabric notebook)::

        from privy import RelayServer
        RelayServer(
            namespace="mdrrahman-dev-relay",
            path="demo",
            keyrule="demo-listen-send",
            key="<primary-key>",
        ).serve_forever()
    """

    def __init__(
        self,
        *,
        namespace: str,
        path: str,
        keyrule: str,
        key: str,
        max_workers: int = 8,
        recv_timeout_s: float = 1.0,
        proxy_target: str | None = None,
    ) -> None:
        if not all([namespace, path, keyrule, key]):
            raise ValueError("namespace, path, keyrule and key are all required")
        self._namespace = namespace
        self._path = path
        self._keyrule = keyrule
        self._key = key
        self._max_workers = max_workers
        self._recv_timeout_s = recv_timeout_s
        self._proxy_target = proxy_target

        self._stop = threading.Event()
        self._listening = threading.Event()
        self._pool: ThreadPoolExecutor | None = None

    # ---- lifecycle -----------------------------------------------------

    def stop(self) -> None:
        """Signal the serve loop to exit after the current iteration."""
        self._stop.set()

    def wait_until_listening(self, timeout: float | None = None) -> bool:
        """Block until the listener websocket is connected (useful for tests)."""
        return self._listening.wait(timeout)

    def serve_forever(self) -> None:
        """Run the listen loop forever, reconnecting with exponential backoff."""
        _ensure_default_logging()
        backoff = 1.0
        self._pool = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="privy-worker")
        try:
            while not self._stop.is_set():
                try:
                    self._serve_once()
                    backoff = 1.0
                except KeyboardInterrupt:
                    log.info("Exiting listener.")
                    return
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "Listener error (%s: %s). Reconnecting in %ss…",
                        type(exc).__name__,
                        exc,
                        backoff,
                    )
                    if self._stop.wait(backoff):
                        return
                    backoff = min(backoff * 2, 30.0)
        finally:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
            self._listening.clear()

    # ---- internals -----------------------------------------------------

    def _listen_url(self) -> str:
        ns = fqdn(self._namespace)
        token = create_sas_token(ns, self._path, self._keyrule, self._key)
        return create_listen_url(ns, self._path, token)

    def _serve_once(self) -> None:
        ns = fqdn(self._namespace)
        ws = websocket.create_connection(self._listen_url())
        ws.settimeout(self._recv_timeout_s)
        self._listening.set()
        log.info("Listening on Azure Relay: wss://%s/$hc/%s", ns, self._path)

        try:
            while not self._stop.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if raw is None or raw == "":
                    log.warning("Control channel closed by peer.")
                    return

                try:
                    frame: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError:
                    log.debug("Dropping non-JSON control frame: %r", raw)
                    continue

                req_meta = frame.get("request")
                if not req_meta:
                    log.debug("Ignoring non-request frame: %s", frame)
                    continue

                if "method" in req_meta:
                    # Inline mode: body (if any) comes on the same control
                    # socket, response goes back on the same control socket.
                    # We must serve synchronously here — the control socket is
                    # a single byte stream.
                    self._handle_inline(ws, req_meta)
                else:
                    # Rendezvous mode: hand off to a worker which opens a
                    # dedicated sub-websocket per request, leaving the
                    # listener free to accept more traffic.
                    if self._pool is None:  # pragma: no cover
                        raise RuntimeError("worker pool not initialised")
                    self._pool.submit(self._handle_rendezvous, req_meta)
        finally:
            self._listening.clear()
            try:
                ws.close()
            except Exception:  # pragma: no cover
                pass

    # Each handler below follows the same three-step shape:
    #   1) read the optional body from `opws`
    #   2) execute the request
    #   3) write a response frame (+ body frame) back on `opws`

    def _handle_inline(self, ws: websocket.WebSocket, req_meta: dict[str, Any]) -> None:
        try:
            payload_raw = self._maybe_recv_body(ws, req_meta)
            result = self._execute(payload_raw, request_id=req_meta.get("id"))
            if isinstance(result, str):
                # Proxy response — already JSON
                self._send_response(ws, req_meta.get("id"), result)
            else:
                self._send_response(ws, req_meta.get("id"), result.to_json())
        except Exception as exc:  # noqa: BLE001
            log.exception("inline request handler crashed: %s", exc)

    def _handle_rendezvous(self, req_meta: dict[str, Any]) -> None:
        addr = req_meta.get("address")
        if not addr:
            log.warning("Rendezvous request missing address; dropping.")
            return
        try:
            opws = websocket.create_connection(addr)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to open rendezvous %s: %s", addr, exc)
            return
        try:
            try:
                first_raw = opws.recv()
            except Exception as exc:  # noqa: BLE001
                log.exception("rendezvous recv failed: %s", exc)
                return
            try:
                first = json.loads(first_raw) if first_raw else {}
            except json.JSONDecodeError:
                log.warning("rendezvous first frame not JSON: %r", first_raw)
                return
            inner = first.get("request", {})
            payload_raw = self._maybe_recv_body(opws, inner)
            result = self._execute(payload_raw, request_id=inner.get("id") or req_meta.get("id"))
            if isinstance(result, str):
                self._send_response(opws, inner.get("id") or req_meta.get("id"), result)
            else:
                self._send_response(opws, inner.get("id") or req_meta.get("id"), result.to_json())
        finally:
            try:
                opws.close()
            except Exception:  # pragma: no cover
                pass

    @staticmethod
    def _maybe_recv_body(ws: websocket.WebSocket, meta: dict[str, Any]) -> str | None:
        if not meta.get("body"):
            return None
        body = ws.recv()
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return body

    @staticmethod
    def _send_response(ws: websocket.WebSocket, request_id: Any, body_json: str) -> None:
        frame = {
            "response": {
                "requestId": request_id,
                "body": True,
                "statusCode": 200,
                "responseHeaders": {"Content-Type": "application/json"},
            }
        }
        ws.send(json.dumps(frame))
        ws.send(body_json)

    def _execute(self, payload_raw: str | None, *, request_id: Any) -> ExecResponse | str:
        """Execute a request. Returns ExecResponse for code, or JSON string for proxy."""
        if not payload_raw:
            return ExecResponse.from_output(
                exit_code=2,
                stdout=b"",
                stderr=b"empty request body\n",
                duration_ms=0,
                error="empty_body",
            )

        # Check if this is an HTTP proxy request
        try:
            raw_obj = json.loads(payload_raw)
            if raw_obj.get("kind") == PROXY_KIND and self._proxy_target:
                proxy_req = ProxyRequest.from_json(payload_raw)
                log.info("PROXY %s %s → %s", proxy_req.method, proxy_req.path, self._proxy_target)
                proxy_resp = handle_proxy_request(proxy_req, self._proxy_target)
                return proxy_resp.to_json()
        except (json.JSONDecodeError, KeyError):
            pass

        try:
            req = ExecRequest.from_json(payload_raw)
        except Exception as exc:  # noqa: BLE001
            return ExecResponse.from_output(
                exit_code=2,
                stdout=b"",
                stderr=f"invalid request: {exc}\n".encode(),
                duration_ms=0,
                error="bad_request",
            )
        _log_request(req, request_id)
        resp = execute(req)
        _log_response(resp, request_id)
        return resp
