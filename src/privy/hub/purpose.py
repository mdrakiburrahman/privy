"""Ephemeral, request-declared HYCO purpose history. Never sent to a listener."""

from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

MAX_PURPOSE_CHARS = 240
RETENTION_S = 7 * 24 * 60 * 60
MAX_OVERVIEW_BUCKETS = 120
WINDOWS = {"1h": 3600, "24h": 86400, "7d": RETENTION_S}


def normalize_purpose(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("purpose must be a string")
    if len(value) > MAX_PURPOSE_CHARS:
        raise ValueError(f"purpose must be at most {MAX_PURPOSE_CHARS} characters")
    if any(unicodedata.category(char).startswith("C") and char not in "\t\r\n" for char in value):
        raise ValueError("purpose must not contain control characters or invalid Unicode")
    return " ".join(value.split()) or None


def purpose_color(purpose: str) -> int:
    return int.from_bytes(hashlib.sha256(purpose.encode("utf-8")).digest()[:2], "big") % 8


@dataclass
class _Group:
    id: int
    purpose: str | None
    first_at: float
    last_at: float
    count: int = 1


@dataclass
class _History:
    groups: deque[_Group] = field(default_factory=deque)
    latest: str | None = None
    latest_at: float | None = None


class PurposeHistory:
    """Groups are bounded by age; the latest label lasts only until process exit."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._targets: dict[str, _History] = {}
        self._sequence = 0
        self._epoch = secrets.token_hex(8)
        self.started_at = clock()

    @staticmethod
    def _trim(history: _History, cutoff: float) -> None:
        # Retain one predecessor to establish the state at the left window edge.
        while len(history.groups) > 1 and history.groups[1].first_at <= cutoff:
            history.groups.popleft()

    def record(self, target_id: str, purpose: str | None) -> None:
        purpose = normalize_purpose(purpose)
        if not target_id:
            raise ValueError("target must not be empty")
        with self._lock:
            history = self._targets.setdefault(target_id, _History())
            at = max(self._clock(), history.groups[-1].last_at if history.groups else self.started_at)
            self._trim(history, at - RETENTION_S)
            if history.groups and history.groups[-1].purpose == purpose:
                group = history.groups[-1]
                group.count += 1
                group.last_at = at
            else:
                self._sequence += 1
                history.groups.append(_Group(self._sequence, purpose, at, at))
            if purpose is not None:
                history.latest, history.latest_at = purpose, at

    def _snapshot(
        self, target_id: str, start: float, end: float
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        history = self._targets.get(target_id)
        if history is None:
            return {"latest": None, "latest_at": None, "latest_unknown": True}, []
        self._trim(history, end - RETENTION_S)
        metadata = {
            "latest": history.latest,
            "latest_at": history.latest_at,
            "latest_unknown": history.groups[-1].purpose is None,
        }
        groups = list(history.groups)
        result = []
        for i, group in enumerate(groups):
            until = groups[i + 1].first_at if i + 1 < len(groups) else end
            if until < start or group.first_at > end:
                continue
            result.append(
                {
                    "id": group.id,
                    "purpose": group.purpose,
                    "first_at": group.first_at,
                    "last_at": group.last_at,
                    "count": group.count,
                    "start": max(start, group.first_at),
                    "end": min(end, until),
                    "kind": "unknown" if group.purpose is None else "purpose",
                    "color": purpose_color(group.purpose) if group.purpose else None,
                    "label": hashlib.sha256(group.purpose.encode("utf-8")).hexdigest()[:4].upper()
                    if group.purpose
                    else None,
                }
            )
        return metadata, result

    @staticmethod
    def _overview(groups: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
        segments = [dict(group) for group in groups]
        if not segments or segments[0]["start"] > start:
            segments.insert(
                0,
                {
                    "id": None,
                    "purpose": None,
                    "start": start,
                    "end": segments[0]["start"] if segments else end,
                    "kind": "unknown",
                    "color": None,
                },
            )
        if len(segments) <= MAX_OVERVIEW_BUCKETS or end <= start:
            return segments
        width = (end - start) / MAX_OVERVIEW_BUCKETS
        overview = []
        index = 0
        for slot in range(MAX_OVERVIEW_BUCKETS):
            left, right = start + slot * width, min(end, start + (slot + 1) * width)
            while index + 1 < len(segments) and segments[index]["end"] < left:
                index += 1
            overlapping = []
            pos = index
            while pos < len(segments) and segments[pos]["start"] < right:
                if segments[pos]["end"] >= left:
                    overlapping.append(segments[pos])
                pos += 1
            if len(overlapping) == 1:
                overview.append({**overlapping[0], "start": left, "end": right})
            else:
                overview.append(
                    {"id": None, "purpose": None, "start": left, "end": right, "kind": "mixed", "color": None}
                )
        return overview

    def summaries(self, target_ids: Iterable[str], *, start: float, end: float) -> dict[str, Any]:
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise ValueError("invalid purpose timeline window")
        result = {}
        with self._lock:
            for target_id in target_ids:
                metadata, groups = self._snapshot(target_id, start, end)
                result[target_id] = {
                    **metadata,
                    "groups": len(groups),
                    "segments": self._overview(groups, start, end) if metadata["latest"] else [],
                    "start": start,
                    "end": end,
                }
        return result

    def history(
        self, target_id: str, *, range_name: str = "24h", cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        if range_name not in WINDOWS:
            raise ValueError("range must be one of 1h, 24h, 7d")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        before = None
        if cursor is not None:
            parts = cursor.split(":")
            if len(parts) != 3 or parts[0] != self._epoch or parts[1] != range_name or not parts[2].isdigit():
                raise ValueError("purpose history cursor is invalid or from a previous hub process")
            before = int(parts[2])
        end = self._clock()
        start = end - WINDOWS[range_name]
        with self._lock:
            metadata, groups = self._snapshot(target_id, start, end)
            if before is not None and not any(g["id"] == before for g in groups):
                raise ValueError("purpose history cursor expired or belongs to another target or range")
            selected = [g for g in reversed(groups) if before is None or g["id"] < before]
            page = selected[:limit]
            next_cursor = f"{self._epoch}:{range_name}:{page[-1]['id']}" if len(selected) > limit else None
        return {
            "target": target_id,
            "range": range_name,
            "start": start,
            "end": end,
            "history_since": self.started_at,
            "ephemeral": True,
            **metadata,
            "groups": page,
            "next_cursor": next_cursor,
        }
