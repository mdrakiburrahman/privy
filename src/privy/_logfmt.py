"""Console formatting shared by the listener and the hub.

Both processes dump each request and response as a box so an operator watching
the console can follow exactly what ran where and what came back.
"""

from __future__ import annotations

from typing import Any

#: Longest code or output excerpt printed inside a box.
MAX_LOG_BYTES = 4000
BOX_RULE = "└" + "─" * 48


def short_id(value: Any) -> str:
    text = str(value or "-")
    return text if len(text) <= 12 else text[:8] + "…"


def decode_for_log(data: bytes, limit: int = MAX_LOG_BYTES) -> str:
    """Decode output for a box, truncating very long output."""
    if not data:
        return ""
    view = data[:limit]
    text = view.decode("utf-8", "replace").rstrip("\n")
    if len(data) > limit:
        text += f"\n… [truncated {len(data) - limit} more bytes]"
    return text


def clip_text(text: str, limit: int = MAX_LOG_BYTES) -> str:
    """Shorten submitted code for a box."""
    return text if len(text) <= limit else text[:limit] + "\n… [truncated]"


def indent(text: str, prefix: str = "    ") -> str:
    if not text:
        return prefix + "(empty)"
    return "\n".join(prefix + line for line in text.splitlines())


def box(title: str, lines: list[str]) -> str:
    """A titled box: ``┌── title``, the body lines, then a closing rule."""
    return "\n".join([f"\n┌── {title}", *lines, BOX_RULE])
