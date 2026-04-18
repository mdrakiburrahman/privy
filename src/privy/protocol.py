"""Wire protocol for privy execution requests / responses.

Kept deliberately small: everything is JSON-serializable and bytes are carried
as base64-encoded strings so non-UTF-8 output is lossless.
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

Kind = Literal["python", "bash"]
Mode = Literal["subprocess", "inprocess"]

PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT_S = 600.0


def b64encode_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode_str(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


@dataclass
class ExecRequest:
    kind: Kind
    code: str
    mode: Mode = "subprocess"
    timeout_s: float = DEFAULT_TIMEOUT_S
    protocol_version: int = PROTOCOL_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> ExecRequest:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        obj: dict[str, Any] = json.loads(raw)
        kind = obj.get("kind")
        if kind not in ("python", "bash"):
            raise ValueError(f"invalid kind: {kind!r}")
        mode = obj.get("mode", "subprocess")
        if mode not in ("subprocess", "inprocess"):
            raise ValueError(f"invalid mode: {mode!r}")
        if mode == "inprocess" and kind != "python":
            raise ValueError("inprocess mode is only valid for kind='python'")
        code = obj.get("code")
        if not isinstance(code, str):
            raise ValueError("code must be a string")
        timeout_s = float(obj.get("timeout_s", DEFAULT_TIMEOUT_S))
        return cls(
            kind=kind,
            code=code,
            mode=mode,
            timeout_s=timeout_s,
            protocol_version=int(obj.get("protocol_version", PROTOCOL_VERSION)),
        )


@dataclass
class ExecResponse:
    exit_code: int
    stdout_b64: str = ""
    stderr_b64: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    error: str | None = None
    protocol_version: int = PROTOCOL_VERSION

    @classmethod
    def from_output(
        cls,
        *,
        exit_code: int,
        stdout: bytes,
        stderr: bytes,
        duration_ms: int,
        timed_out: bool = False,
        error: str | None = None,
    ) -> ExecResponse:
        return cls(
            exit_code=exit_code,
            stdout_b64=b64encode_bytes(stdout),
            stderr_b64=b64encode_bytes(stderr),
            duration_ms=duration_ms,
            timed_out=timed_out,
            error=error,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> ExecResponse:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        obj: dict[str, Any] = json.loads(raw)
        return cls(
            exit_code=int(obj.get("exit_code", -1)),
            stdout_b64=obj.get("stdout_b64", "") or "",
            stderr_b64=obj.get("stderr_b64", "") or "",
            duration_ms=int(obj.get("duration_ms", 0)),
            timed_out=bool(obj.get("timed_out", False)),
            error=obj.get("error"),
            protocol_version=int(obj.get("protocol_version", PROTOCOL_VERSION)),
        )

    @property
    def stdout(self) -> bytes:
        return b64decode_str(self.stdout_b64) if self.stdout_b64 else b""

    @property
    def stderr(self) -> bytes:
        return b64decode_str(self.stderr_b64) if self.stderr_b64 else b""
