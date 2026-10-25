"""HTTP responses produced by the hub's REST and MCP layers."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: A streamed body: called with ``write(chunk)``; returns when the body is complete.
BodyWriter = Callable[[Callable[[bytes], None]], None]

JSON_TYPE = "application/json; charset=utf-8"
TEXT_TYPE = "text/plain; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"
EVENT_STREAM_TYPE = "text/event-stream; charset=utf-8"


def json_bytes(payload: Any) -> bytes:
    """UTF-8 JSON; falls back to ASCII escapes when ``payload`` holds an unpaired surrogate."""
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(payload).encode("ascii")


@dataclass
class HttpResponse:
    """A JSON ``payload``, a ``text`` body, or a ``stream`` written incrementally."""

    status: int
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    text: str | None = None
    content_type: str | None = None
    stream: BodyWriter | None = None
    #: Extra outcome shown on the hub's console line for this call.
    note: str | None = None
    #: Which client surface made the call (``rest``, ``mcp``, ``ui``, ``health``); derived when unset.
    surface: str | None = None

    def body(self) -> bytes:
        if self.text is not None:
            return self.text.encode("utf-8", "backslashreplace")
        if self.payload is None:
            return b""
        return json_bytes(self.payload)

    def media_type(self) -> str:
        if self.content_type:
            return self.content_type
        return JSON_TYPE if self.text is None else TEXT_TYPE


def error_response(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> HttpResponse:
    return HttpResponse(status, {"error": {"code": code, "message": message}}, dict(headers or {}))
