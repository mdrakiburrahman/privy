"""Same-origin browser bridge to explicitly capable, native listener terminals."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from privy.client import RelayHTTPError
from privy.hub.registry import ONLINE, TargetLookupError
from privy.hub.responses import HttpResponse, error_response

log = logging.getLogger(__name__)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")
_PATH = re.compile(r"/v1/terminals/([A-Za-z0-9_-]{16,128})/(read|write|resize|close)")
_MAX_BINDINGS = 256


def terminal_languages(info: Any) -> list[str]:
    if not isinstance(info, dict):
        return []
    capabilities = info.get("capabilities")
    capability = capabilities.get("terminal") if isinstance(capabilities, dict) else None
    if not isinstance(capability, dict) or type(capability.get("version")) is not int:
        return []
    if capability["version"] != 1 or not isinstance(capability.get("shells"), list):
        return []
    runtimes = info.get("runtimes")
    if not isinstance(runtimes, dict):
        return []
    return [
        shell
        for shell in ("powershell", "bash")
        if shell in capability["shells"] and isinstance(runtimes.get(shell), str) and runtimes[shell]
    ]


@dataclass
class _Binding:
    owner: str
    target: str
    client: Any
    created: float
    remote_id: str | None = None
    closed: bool = False
    opening: bool = True


class TerminalBridge:
    def __init__(self, service: Any) -> None:
        self.service = service
        self._bindings: dict[str, _Binding] = {}
        self._lock = threading.Lock()

    def route(self, method: str, path: str, headers: Any, body: bytes) -> HttpResponse | None:
        match = _PATH.fullmatch(path)
        if path != "/v1/terminals" and match is None:
            return None
        if method != "POST":
            return error_response(405, "method_not_allowed", "use POST", {"Allow": "POST"})
        if headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            return error_response(415, "json_required", "terminal operations require application/json")
        try:
            args = json.loads(body)
        except (ValueError, UnicodeError):
            return error_response(400, "invalid_request", "body must be a JSON object")
        if not isinstance(args, dict) or not self._token(args.get("owner")):
            return error_response(400, "invalid_request", "a valid terminal workspace owner is required")
        if "data_b64" in args and (not isinstance(args["data_b64"], str) or len(args["data_b64"]) > 43692):
            return error_response(
                400, "invalid_request", "terminal input must be at most 32768 decoded bytes"
            )
        try:
            if match is None:
                return self._open(args)
            return self._operation(match[1], match[2], args)
        except TargetLookupError as exc:
            return error_response(404, "unknown_target", str(exc))
        except RelayHTTPError as exc:
            log.warning("Terminal Relay request failed: %s", exc)
            return error_response(
                502,
                "relay_unavailable",
                "Listener connection failed. Reconnect or disconnect; input was not replayed.",
            )

    @staticmethod
    def _token(value: Any) -> bool:
        return isinstance(value, str) and _TOKEN.fullmatch(value) is not None

    def _open(self, args: dict[str, Any]) -> HttpResponse:
        request_id, target_id, shell = args.get("request_id"), args.get("target"), args.get("shell")
        if (
            not self._token(request_id)
            or not isinstance(target_id, str)
            or shell not in ("powershell", "bash")
        ):
            return error_response(
                400, "invalid_request", "request_id, exact target ID, and PowerShell or Bash are required"
            )
        target = self.service.registry.resolve(target_id)
        if target.id != target_id:
            return error_response(400, "invalid_target", "use the exact target ID")
        probe = self.service.registry.probe(target, refresh=True)
        if probe.status != ONLINE or shell not in terminal_languages(probe.info):
            return error_response(
                409,
                "terminal_unavailable",
                "This listener is offline or does not advertise this interactive shell capability. Upgrade or refresh targets.",
            )
        with self._lock:
            now = time.monotonic()
            self._bindings = {
                key: value for key, value in self._bindings.items() if now - value.created < 3900
            }
            binding = self._bindings.get(request_id)
            if binding is not None:
                if binding.owner != args["owner"] or binding.target != target_id:
                    return error_response(403, "forbidden", "terminal belongs to another workspace")
                return error_response(
                    409,
                    "already_opened",
                    "This terminal request already exists. Reconnect or disconnect it; do not resubmit.",
                )
            if len(self._bindings) >= _MAX_BINDINGS:
                return error_response(
                    429,
                    "terminal_capacity",
                    "Terminal workspace capacity reached; close an existing terminal.",
                )
            binding = _Binding(args["owner"], target_id, self.service.registry.client(target), now)
            self._bindings[request_id] = binding
        result = self._call(
            binding,
            {
                "action": "open",
                "owner": binding.owner,
                "request_id": request_id,
                "shell": shell,
                "cols": args.get("cols", 80),
                "rows": args.get("rows", 24),
            },
        )
        with self._lock:
            binding.opening = False
            if result.get("ok"):
                binding.remote_id = result.get("session_id")
            else:
                binding.closed = True
            cancelled = binding.closed
        if cancelled and result.get("ok"):
            self._close_remote(request_id, binding)
            return error_response(409, "terminal_closed", "Workspace was closed while connecting.")
        if result.get("ok"):
            self.service.jobs.audit.record(
                "terminal.open", target=target_id, terminal_id=request_id, shell=shell
            )
        return self._response(result, request_id)

    def _operation(self, session_id: str, action: str, args: dict[str, Any]) -> HttpResponse:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None and action == "close":
                target_id = args.get("target")
                if not isinstance(target_id, str):
                    return error_response(
                        400, "invalid_request", "target is required to close a pending workspace"
                    )
                target = self.service.registry.resolve(target_id)
                if target.id != target_id:
                    return error_response(400, "invalid_target", "use the exact target ID")
                if len(self._bindings) >= _MAX_BINDINGS:
                    return error_response(429, "terminal_capacity", "Terminal workspace capacity reached")
                binding = _Binding(
                    args["owner"],
                    target_id,
                    self.service.registry.client(target),
                    time.monotonic(),
                    closed=True,
                )
                self._bindings[session_id] = binding
            if binding is None:
                return error_response(
                    404,
                    "terminal_missing",
                    "Terminal no longer belongs to this hub. Open a new shell explicitly.",
                )
            if binding.owner != args["owner"]:
                return error_response(403, "forbidden", "terminal belongs to another workspace")
            if not binding.closed:
                binding.created = time.monotonic()
            if binding.closed and action != "close":
                return error_response(410, "terminal_closed", "This terminal has been closed.")
            if action == "close":
                binding.closed = True
        if action == "close":
            result = self._close_remote(session_id, binding)
            self.service.jobs.audit.record("terminal.close", target=binding.target, terminal_id=session_id)
        elif binding.remote_id is None:
            return error_response(
                409,
                "terminal_connecting",
                "Terminal creation is still unresolved. Disconnect before opening another.",
            )
        else:
            payload = {
                key: args[key]
                for key in ("offset", "wait_s", "max_bytes", "seq", "data_b64", "user_input", "cols", "rows")
                if key in args
            }
            result = self._call(
                binding,
                {
                    **payload,
                    "action": action,
                    "owner": binding.owner,
                    "session_id": binding.remote_id,
                },
            )
            if result.get("ok") and result.get("state") in ("closed", "expired", "exited", "missing"):
                with self._lock:
                    binding.closed = True
                self.service.jobs.audit.record(
                    "terminal.finish",
                    target=binding.target,
                    terminal_id=session_id,
                    state=result["state"],
                )
        return self._response(result, session_id)

    @staticmethod
    def _close_remote(request_id: str, binding: _Binding) -> dict[str, Any]:
        return TerminalBridge._call(
            binding,
            {
                "action": "close",
                "session_id": binding.remote_id,
                "request_id": request_id,
                "owner": binding.owner,
            },
        )

    @staticmethod
    def _call(binding: _Binding, payload: dict[str, Any]) -> dict[str, Any]:
        result = binding.client.terminal(payload)
        if (
            not isinstance(result, dict)
            or result.get("kind") != "terminal"
            or result.get("protocol_version") != 1
            or type(result.get("ok")) is not bool
            or (not result["ok"] and not isinstance(result.get("error"), dict))
        ):
            raise RelayHTTPError("Listener returned an invalid terminal response")
        if payload["action"] == "open" and result["ok"] and not isinstance(result.get("session_id"), str):
            raise RelayHTTPError("Listener returned an invalid terminal session")
        return result

    @staticmethod
    def _response(result: dict[str, Any], session_id: str) -> HttpResponse:
        if (
            not isinstance(result, dict)
            or result.get("kind") != "terminal"
            or type(result.get("ok")) is not bool
        ):
            return error_response(
                502, "invalid_terminal_response", "Listener returned an invalid terminal response."
            )
        status = 200
        if not result["ok"]:
            code = (result.get("error") or {}).get("code")
            status = (
                403
                if code == "forbidden"
                else 409
                if code in ("buffer_gap", "sequence_gap", "capacity")
                else 400
            )
        return HttpResponse(status, {**result, "session_id": session_id})

    def close(self) -> None:
        with self._lock:
            pending = [(key, value) for key, value in self._bindings.items() if not value.closed]
            for _, binding in pending:
                binding.closed = True
        for key, binding in pending:
            try:
                self._close_remote(key, binding)
            except RelayHTTPError:
                log.warning(
                    "Could not close terminal on %s during hub shutdown; listener idle expiry remains active",
                    binding.target,
                )
