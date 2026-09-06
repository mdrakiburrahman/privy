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
* For inline requests the *body* must be read on the control socket in arrival
  order, but execution then moves to the thread pool and the response is
  queued back to the listener thread for emission on that same control socket.
  This keeps request execution parallel without having worker threads race on
  the shared websocket. For rendezvous requests we hand the whole exchange to
  the pool because each one has its own dedicated sub-websocket.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import websocket

from privy._relay import create_listen_url, create_sas_token, fqdn
from privy.executor import execute, seed_inprocess_globals
from privy.protocol import ExecRequest, ExecResponse
from privy.proxy import PROXY_KIND, ProxyRequest, handle_proxy_request


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
    if req.action in ("poll", "cancel"):
        # One line — a long-running job produces many of these.
        log.debug("▶ %s %s job=%s wait=%ss", req.action.upper(), rid, _short_id(req.job_id), req.wait_s)
        return
    code_preview = (
        req.code if len(req.code) <= _MAX_LOG_BYTES else req.code[:_MAX_LOG_BYTES] + "\n… [truncated]"
    )
    msg = (
        f"\n┌── ▶ {req.action.upper():<7}{rid}  kind={req.kind}  mode={req.mode}  "
        f"timeout={req.timeout_s}s\n"
        f"{_indent(code_preview, '│   ')}\n"
        f"└────────────────────────────────────────────────"
    )
    log.info(msg)


def _log_response(resp: ExecResponse, request_id: Any) -> None:
    rid = _short_id(request_id)
    if resp.state == "running":
        # Job accepted, or still going — nothing interesting to dump yet.
        log.debug("◀ %s job=%s state=running", rid, _short_id(resp.job_id))
        return
    status = "✓" if resp.exit_code == 0 and not resp.timed_out else "✗"
    header = (
        f"\n┌── ◀ RESPONSE {rid}  {status} exit={resp.exit_code}  "
        f"{resp.duration_ms}ms"
        + ("  timed_out" if resp.timed_out else "")
        + (f"  error={resp.error}" if resp.error else "")
        + (f"  job={_short_id(resp.job_id)}" if resp.job_id else "")
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


@dataclass
class _PendingInlineResponse:
    request_id: Any
    body_json: str


class _InlineResponsePump:
    """Thread-safe queue for inline control-channel responses.

    The Azure Relay control websocket is both the inbound listener socket and
    the outbound response channel for inline requests. Keep all control-socket
    I/O on the listener thread: workers execute requests in parallel, but only
    enqueue their completed responses here.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[_PendingInlineResponse] = deque()
        self._ready = threading.Event()
        self._closed = False

    def submit(self, request_id: Any, body_json: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._pending.append(_PendingInlineResponse(request_id=request_id, body_json=body_json))
            self._ready.set()
            return True

    def drain(self) -> list[_PendingInlineResponse]:
        with self._lock:
            items = list(self._pending)
            self._pending.clear()
            self._ready.clear()
            return items

    def has_pending(self) -> bool:
        return self._ready.is_set()

    def close(self) -> int:
        with self._lock:
            self._closed = True
            dropped = len(self._pending)
            self._pending.clear()
            self._ready.clear()
            return dropped


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
            # Expose this cell's live `spark`/`sc` to mode="inprocess" requests.
            inprocess_globals={"spark": spark, "sc": sc},
        ).serve_forever()
    """

    def __init__(
        self,
        *,
        namespace: str,
        path: str,
        keyrule: str,
        key: str,
        max_workers: int = 32,
        listener_connections: int = 1,
        recv_timeout_s: float = 1.0,
        proxy_target: str | None = None,
        inprocess_globals: dict[str, Any] | None = None,
    ) -> None:
        # NOTE: a long-polling ``action="poll"`` occupies a worker for the
        # duration of its wait, so the pool must comfortably exceed the number
        # of concurrent clients (e.g. dbt threads).
        if not all([namespace, path, keyrule, key]):
            raise ValueError("namespace, path, keyrule and key are all required")
        if (
            isinstance(listener_connections, bool)
            or not isinstance(listener_connections, int)
            or not 1 <= listener_connections <= 25
        ):
            raise ValueError("listener_connections must be between 1 and 25")
        self._namespace = namespace
        self._path = path
        self._keyrule = keyrule
        self._key = key
        self._max_workers = max_workers
        self._listener_connections = listener_connections
        self._recv_timeout_s = recv_timeout_s
        self._proxy_target = proxy_target

        if inprocess_globals:
            # Lets the host notebook expose live objects (e.g. Fabric's
            # `spark`/`sc`) to code later submitted with mode="inprocess".
            seed_inprocess_globals(inprocess_globals)

        self._stop = threading.Event()
        self._listening = threading.Event()
        self._listener_state_lock = threading.Lock()
        self._active_listener_count = 0
        self._pool: ThreadPoolExecutor | None = None

    # ---- lifecycle -----------------------------------------------------

    def stop(self) -> None:
        """Signal the serve loop to exit after the current iteration."""
        self._stop.set()

    def wait_until_listening(self, timeout: float | None = None) -> bool:
        """Block until the listener websocket is connected (useful for tests)."""
        return self._listening.wait(timeout)

    def serve_forever(self) -> None:
        """Run listener connections forever, reconnecting each independently."""
        _ensure_default_logging()
        self._pool = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="privy-worker")
        listeners = [
            threading.Thread(
                target=self._serve_listener,
                args=(index,),
                name=f"privy-listener-{index + 1}",
                daemon=True,
            )
            for index in range(self._listener_connections)
        ]
        try:
            for listener in listeners:
                listener.start()
            while not self._stop.wait(0.5):
                if not any(listener.is_alive() for listener in listeners):
                    raise RuntimeError("all Azure Relay listener threads exited")
        except KeyboardInterrupt:
            log.info("Exiting listener.")
            self.stop()
        finally:
            self.stop()
            for listener in listeners:
                listener.join(timeout=max(1.0, self._recv_timeout_s + 1.0))
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
            self._listening.clear()

    # ---- internals -----------------------------------------------------

    def _serve_listener(self, index: int) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._serve_once()
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Listener %s error (%s: %s). Reconnecting in %ss…",
                    index + 1,
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)

    def _listener_connected(self) -> None:
        with self._listener_state_lock:
            self._active_listener_count += 1
            self._listening.set()

    def _listener_disconnected(self) -> None:
        with self._listener_state_lock:
            self._active_listener_count = max(0, self._active_listener_count - 1)
            if self._active_listener_count == 0:
                self._listening.clear()

    def _listen_url(self) -> str:
        ns = fqdn(self._namespace)
        token = create_sas_token(ns, self._path, self._keyrule, self._key)
        return create_listen_url(ns, self._path, token)

    def _serve_once(self) -> None:
        ns = fqdn(self._namespace)
        ws = websocket.create_connection(self._listen_url())
        ws.settimeout(self._recv_timeout_s)
        self._listener_connected()
        log.info("Listening on Azure Relay: wss://%s/$hc/%s", ns, self._path)
        inline_responses = _InlineResponsePump()

        try:
            while not self._stop.is_set():
                if inline_responses.has_pending():
                    self._flush_inline_responses(ws, inline_responses)
                    continue
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    self._flush_inline_responses(ws, inline_responses)
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
                    # socket, and the response goes back on it too. Read the
                    # body here (ordering matters), then execute off-thread.
                    self._handle_inline(ws, req_meta, inline_responses)
                else:
                    # Rendezvous mode: hand off to a worker which opens a
                    # dedicated sub-websocket per request, leaving the
                    # listener free to accept more traffic.
                    if self._pool is None:  # pragma: no cover
                        raise RuntimeError("worker pool not initialised")
                    self._pool.submit(self._handle_rendezvous, req_meta)
                self._flush_inline_responses(ws, inline_responses)
        finally:
            self._listener_disconnected()
            dropped = inline_responses.close()
            if dropped:
                log.warning("Dropping %s inline response(s) on control-channel loss.", dropped)
            try:
                ws.close()
            except Exception:  # pragma: no cover
                pass

    # Each handler below follows the same three-step shape:
    #   1) read the optional body from `opws`
    #   2) execute the request
    #   3) write a response frame (+ body frame) back on `opws`

    def _handle_inline(
        self,
        ws: websocket.WebSocket,
        req_meta: dict[str, Any],
        inline_responses: _InlineResponsePump,
    ) -> None:
        try:
            payload_raw = self._maybe_recv_body(ws, req_meta)
        except Exception as exc:  # noqa: BLE001
            log.exception("inline body read failed: %s", exc)
            return

        request_id = req_meta.get("id")

        def work() -> None:
            try:
                result = self._execute(payload_raw, request_id=request_id)
                body = result if isinstance(result, str) else result.to_json()
                inline_responses.submit(request_id, body)
            except Exception as exc:  # noqa: BLE001
                log.exception("inline request handler crashed: %s", exc)

        if self._pool is None:  # pragma: no cover - defensive
            work()
        else:
            self._pool.submit(work)

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

    def _flush_inline_responses(
        self,
        ws: websocket.WebSocket,
        inline_responses: _InlineResponsePump,
    ) -> None:
        for pending in inline_responses.drain():
            self._send_response(ws, pending.request_id, pending.body_json)

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
