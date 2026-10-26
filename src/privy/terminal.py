"""Bounded, owner-scoped persistent terminal sessions and their versioned Relay RPC."""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

from privy import terminal_backends

log = logging.getLogger(__name__)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")
MAX_INPUT = 32768
MAX_OUTPUT = 1024 * 1024
MAX_SESSIONS = 4


class _Invalid(ValueError):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code, self.extra = code, extra


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _Invalid("invalid_request", f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise _Invalid("invalid_request", f"{name} must be a 16-128 character identifier")
    return value


class _Session:
    def __init__(self, backend: Any, owner: str, ttl: float, clock: Any) -> None:
        self.id = uuid.uuid4().hex
        self.backend, self.owner, self.ttl, self.clock = backend, owner, ttl, clock
        self.last_input = clock()
        self.condition = threading.Condition(threading.RLock())
        self.operation = threading.Lock()
        self.output = bytearray()
        self.start = 0
        self.next_seq = 0
        self.last_write: tuple[int, bytes] | None = None
        self.state = "running"
        self.error: str | None = None
        self.exit_code: int | None = None
        self.reader = threading.Thread(target=self._read, name=f"privy-terminal-{self.id[:8]}", daemon=True)
        self.reader.start()

    def _read(self) -> None:
        try:
            while True:
                with self.condition:
                    if self.state != "running":
                        return
                chunk = self.backend.read()
                if chunk is None:
                    continue
                if not chunk:
                    self.close("exited")
                    return
                with self.condition:
                    self.output.extend(chunk)
                    overflow = max(0, len(self.output) - MAX_OUTPUT)
                    if overflow:
                        del self.output[:overflow]
                        self.start += overflow
                    self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                if self.state != "running":
                    return
            log.exception("Native terminal reader failed")
            with self.condition:
                self.error = f"terminal I/O failed: {type(exc).__name__}"
            self.close("closed")

    def close(self, state: str) -> None:
        with self.operation:
            with self.condition:
                if self.state != "running" and not self.cleanup_pending:
                    return
                if state == "expired" and self.clock() - self.last_input < self.ttl:
                    return
                self.state = state
                self.condition.notify_all()
            try:
                self.backend.close()
            except Exception:
                log.exception("Native terminal cleanup failed")
                with self.condition:
                    self.error = "terminal cleanup failed; retry disconnect"
                raise
            else:
                with self.condition:
                    if self.cleanup_pending:
                        self.error = None
            finally:
                with self.condition:
                    self.exit_code = self.backend.exit_code
                    self.condition.notify_all()
        if threading.current_thread() is not self.reader:
            self.reader.join(timeout=2)
            if self.reader.is_alive():
                log.error("Terminal reader did not stop after native cleanup")

    @property
    def cleanup_pending(self) -> bool:
        return (self.error or "").startswith("terminal cleanup")

    def describe(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "state": self.state,
            "next_seq": self.next_seq,
            "expires_in_s": max(0, self.ttl - (self.clock() - self.last_input)),
            "exit_code": self.exit_code,
            "start_offset": self.start,
            "offset": self.start + len(self.output),
            "data_b64": "",
        }


class TerminalManager:
    def __init__(
        self, idle_ttl_s: float = 3600, *, clock: Any = time.monotonic, backend_factory: Any = None
    ) -> None:
        if not math.isfinite(idle_ttl_s) or idle_ttl_s <= 0:
            raise ValueError("terminal idle TTL must be positive and finite")
        self.ttl, self.clock = idle_ttl_s, clock
        self.backend_factory = backend_factory or terminal_backends.spawn
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        self._requests: OrderedDict[tuple[str, str], tuple[str | None, float]] = OrderedDict()
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None

    def capabilities(self) -> dict[str, Any] | None:
        available = terminal_backends.supported_shells()
        shells = [shell for shell in ("powershell", "bash") if shell in available]
        return {"version": 1, "shells": shells, "idle_ttl_s": self.ttl} if shells else None

    @staticmethod
    def _reply(**fields: Any) -> dict[str, Any]:
        return {"kind": "terminal", "protocol_version": 1, "ok": True, **fields}

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if (
                not isinstance(payload, dict)
                or payload.get("kind") != "terminal"
                or type(payload.get("protocol_version")) is not int
                or payload["protocol_version"] != 1
            ):
                raise _Invalid("invalid_request", "unsupported terminal protocol")
            owner = _token(payload.get("owner"), "owner")
            action = payload.get("action")
            if action not in ("open", "read", "write", "resize", "close"):
                raise _Invalid("invalid_request", "invalid terminal action")
            if action == "open":
                return self._open(payload, owner)
            if action == "close":
                return self._close_request(payload, owner)
            session = self._session(payload, owner)
            self._expire(session)
            if action == "read":
                return self._read(session, payload)
            if action == "write":
                return self._write(session, payload)
            cols, rows = self._dimensions(payload)
            with session.operation:
                if session.state != "running":
                    raise _Invalid("terminal_closed", f"terminal is {session.state}")
                session.backend.resize(cols, rows)
            return self._reply(**session.describe())
        except _Invalid as exc:
            return {
                "kind": "terminal",
                "protocol_version": 1,
                "ok": False,
                "error": {"code": exc.code, "message": str(exc)},
                **exc.extra,
            }
        except Exception as exc:
            log.exception("Terminal operation failed")
            return {
                "kind": "terminal",
                "protocol_version": 1,
                "ok": False,
                "error": {
                    "code": "terminal_error",
                    "message": f"native terminal operation failed: {type(exc).__name__}",
                },
            }

    @staticmethod
    def _dimensions(payload: dict[str, Any]) -> tuple[int, int]:
        return (
            _integer(payload.get("cols", 80), "cols", 2, 500),
            _integer(payload.get("rows", 24), "rows", 2, 300),
        )

    def _open(self, payload: dict[str, Any], owner: str) -> dict[str, Any]:
        request_id = _token(payload.get("request_id"), "request_id")
        shell = payload.get("shell")
        if shell not in ("powershell", "bash"):
            raise _Invalid("unsupported_shell", "only PowerShell and Bash terminals are supported")
        cols, rows = self._dimensions(payload)
        with self._lock:
            if self._stop.is_set():
                raise _Invalid("terminal_closed", "listener terminal manager is shutting down")
            self._prune()
            key = (owner, request_id)
            if key in self._requests:
                session_id, _ = self._requests[key]
                if session_id in self._sessions:
                    session = self._sessions[session_id]
                    with session.condition:
                        return self._reply(**session.describe())
                return self._reply(state="closed", session_id="", next_seq=0)
            if (
                sum(
                    session.state == "running" or session.cleanup_pending
                    for session in self._sessions.values()
                )
                >= MAX_SESSIONS
            ):
                raise _Invalid("capacity", "listener terminal capacity reached")
            if len(self._requests) >= 256:
                raise _Invalid("capacity", "terminal request retention capacity reached")
            backend = self.backend_factory(shell, cols, rows)
            session = _Session(backend, owner, self.ttl, self.clock)
            self._sessions[session.id] = session
            self._requests[key] = (session.id, self.clock())
            if self._sweeper is None:
                self._sweeper = threading.Thread(
                    target=self._sweep_loop, name="privy-terminal-expiry", daemon=True
                )
                self._sweeper.start()
            with session.condition:
                return self._reply(**session.describe())

    def _session(self, payload: dict[str, Any], owner: str) -> _Session:
        session_id = _token(payload.get("session_id"), "session_id")
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise _Invalid("terminal_missing", "terminal is missing or expired")
        if session.owner != owner:
            raise _Invalid("forbidden", "terminal belongs to another workspace")
        return session

    def _read(self, session: _Session, payload: dict[str, Any]) -> dict[str, Any]:
        offset = _integer(payload.get("offset", 0), "offset", 0, 2**53 - 1)
        maximum = _integer(payload.get("max_bytes", MAX_INPUT), "max_bytes", 1, MAX_INPUT)
        wait = payload.get("wait_s", 1)
        if (
            isinstance(wait, bool)
            or not isinstance(wait, (int, float))
            or not math.isfinite(wait)
            or not 0 <= wait <= 2
        ):
            raise _Invalid("invalid_request", "wait_s must be between 0 and 2 seconds")
        with session.condition:
            end = session.start + len(session.output)
            if offset < session.start:
                raise _Invalid(
                    "buffer_gap", "output exceeded the replay buffer", start_offset=session.start, offset=end
                )
            if offset > end:
                raise _Invalid("invalid_request", "output offset is beyond the terminal output")
            if offset == end and session.state == "running" and not session.error:
                session.condition.wait_for(
                    lambda: (
                        session.state != "running"
                        or session.start + len(session.output) > offset
                        or session.error
                    ),
                    timeout=wait,
                )
            if offset < session.start:
                raise _Invalid("buffer_gap", "output exceeded the replay buffer", start_offset=session.start)
            if session.error:
                raise _Invalid("terminal_error", session.error)
            relative = offset - session.start
            chunk = bytes(session.output[relative : relative + maximum])
            result = session.describe()
            result.update(data_b64=base64.b64encode(chunk).decode("ascii"), offset=offset + len(chunk))
            if result["state"] == "exited" and result["offset"] < session.start + len(session.output):
                result["state"] = "running"
            return self._reply(**result)

    def _write(self, session: _Session, payload: dict[str, Any]) -> dict[str, Any]:
        seq = _integer(payload.get("seq"), "seq", 0, 2**53 - 1)
        encoded = payload.get("data_b64")
        if not isinstance(encoded, str) or len(encoded) > 43692:
            raise _Invalid("invalid_request", "terminal input exceeds its limit")
        try:
            data = base64.b64decode(encoded, validate=True)
            data.decode("utf-8")
        except (ValueError, binascii.Error, UnicodeError):
            raise _Invalid("invalid_request", "input must be base64-encoded UTF-8") from None
        if not data or len(data) > MAX_INPUT:
            raise _Invalid("invalid_request", "terminal input must contain 1-32768 bytes")
        user_input = payload.get("user_input", True)
        if type(user_input) is not bool:
            raise _Invalid("invalid_request", "user_input must be a boolean")
        digest = hashlib.sha256(data).digest()
        failed = False
        with session.operation:
            with session.condition:
                if session.state != "running":
                    raise _Invalid("terminal_closed", f"terminal is {session.state}")
                if session.last_write == (seq, digest):
                    return self._reply(**session.describe())
                if seq != session.next_seq:
                    raise _Invalid("sequence_gap", "input sequence does not match; input was not replayed")
                session.next_seq += 1
                session.last_write = (seq, digest)
                if user_input:
                    session.last_input = self.clock()
            try:
                session.backend.write(data)
            except Exception:
                failed = True
                log.exception("Terminal input failed; closing the shell to prevent partial-input replay")
        if failed:
            session.close("closed")
            raise _Invalid("terminal_error", "input delivery failed; shell was closed to prevent replay")
        with session.condition:
            return self._reply(**session.describe())

    def _close_request(self, payload: dict[str, Any], owner: str) -> dict[str, Any]:
        session = None
        with self._lock:
            request_id = payload.get("request_id")
            if request_id is not None:
                key = (owner, _token(request_id, "request_id"))
                found = self._requests.get(key)
                if found:
                    session = self._sessions.get(found[0])
                else:
                    self._prune()
                    if len(self._requests) >= 256:
                        raise _Invalid("capacity", "terminal request retention capacity reached")
                    self._requests[key] = (None, self.clock())
            session_id = payload.get("session_id")
            if session_id is not None:
                selected = self._sessions.get(_token(session_id, "session_id"))
                if selected:
                    if selected.owner != owner:
                        raise _Invalid("forbidden", "terminal belongs to another workspace")
                    session = selected
        if session is None:
            return self._reply(state="closed", session_id=payload.get("session_id") or "", next_seq=0)
        session.close("closed")
        with session.condition:
            return self._reply(**session.describe())

    def _expire(self, session: _Session) -> None:
        if session.cleanup_pending:
            session.close(session.state)
            return
        with session.condition:
            expired = session.state == "running" and self.clock() - session.last_input >= self.ttl
        if expired:
            session.close("expired")

    def _prune(self) -> None:
        now = self.clock()
        for session in list(self._sessions.values()):
            try:
                self._expire(session)
            except Exception:
                log.exception("Terminal cleanup remains pending; retrying on the next sweep")
        self._requests = OrderedDict(
            (key, value)
            for key, value in self._requests.items()
            if now - value[1] < max(self.ttl, 60)
            or (
                value[0] in self._sessions
                and (self._sessions[value[0]].state == "running" or self._sessions[value[0]].cleanup_pending)
            )
        )
        retained = {value[0] for value in self._requests.values()}
        self._sessions = {key: value for key, value in self._sessions.items() if key in retained}

    def _sweep_loop(self) -> None:
        while not self._stop.wait(min(1, self.ttl / 2)):
            with self._lock:
                self._prune()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            sessions = list(self._sessions.values())
        failures = []
        for session in sessions:
            try:
                session.close("closed")
            except Exception as exc:
                failures.append(exc)
        if self._sweeper and self._sweeper is not threading.current_thread():
            self._sweeper.join(timeout=3)
        if failures:
            raise OSError(f"Failed to clean up {len(failures)} terminal session(s)") from failures[0]
