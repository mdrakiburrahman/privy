"""In-memory usage metrics for the hub's dashboard and ``GET /v1/stats``.

The hub remembers every job it starts (and, after a restart, every job recorded
in ``audit.jsonl`` during the last week), the probes it makes, and the HTTP calls
it answers. Nothing here talks to Azure: the numbers are what this hub did.

Jobs are kept until the hub exits, including their output buffers, so the
dashboard can show each target's command history like ``kubectl logs``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from privy.hub.config import Target
from privy.hub.jobs import (
    CANCELLED,
    ERROR,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TIMED_OUT,
    HubJob,
    StreamPiece,
    utc_iso,
)
from privy.hub.registry import ONLINE, UNKNOWN, ProbeResult, machine_name

log = logging.getLogger("privy.hub")

#: ``range`` name -> (window seconds, bucket seconds).
RANGES: dict[str, tuple[int, int]] = {
    "1h": (60 * 60, 60),
    "24h": (24 * 60 * 60, 15 * 60),
    "7d": (7 * 24 * 60 * 60, 2 * 60 * 60),
}
DEFAULT_RANGE = "24h"
#: Probes and HTTP calls older than this are dropped; jobs are kept until exit.
SAMPLE_RETENTION_S = RANGES["7d"][0]
#: Audit records older than this are not restored on start.
BACKFILL_S = RANGES["7d"][0]
#: Bound on audit bytes read on start (the newest part of the file is read).
BACKFILL_MAX_BYTES = 64 * 1024 * 1024
BAD_STATES = frozenset({FAILED, TIMED_OUT, ERROR})
TRANSFER_TYPES = ("upload", "download", "sync")
SURFACES = ("rest", "mcp", "ui", "health")


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile of ``values`` (``None`` when empty)."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def command_of(job_type: str, spec: dict[str, Any]) -> str:
    """What a job ran, as one string: the code, or the transfer's paths."""
    if job_type == "exec":
        return str(spec.get("code") or "")
    local, remote = spec.get("local_path") or "?", spec.get("remote_path") or "?"
    if job_type == "download":
        return f"{remote} → {local}"
    if job_type == "sync":
        return f"{local} → {remote} ({spec.get('destination_type') or 'directory'})"
    return f"{local} → {remote}"


@dataclass
class JobEntry:
    """One job in a target's history: a live :class:`HubJob`, or one restored from the audit log."""

    id: str
    target_id: str
    type: str
    language: str | None
    command: str
    origin: str
    created_at: float
    broadcast_id: str | None = None
    job: HubJob | None = None
    # Restored jobs only (live ones read these from ``job``).
    state: str = UNKNOWN
    exit_code: int | None = None
    error: str | None = None
    duration_ms: int | None = None
    bytes_moved: int = 0
    command_truncated: bool = False

    @property
    def restored(self) -> bool:
        return self.job is None

    def view(self) -> dict[str, Any]:
        """The job's current state as plain data."""
        result: dict[str, Any] = {
            "job_id": self.id,
            "target": self.target_id,
            "type": self.type,
            "language": self.language,
            "command": self.command,
            "origin": self.origin,
            "created_at": self.created_at,
            "broadcast_id": self.broadcast_id,
            "restored": self.restored,
            "command_truncated": self.command_truncated,
        }
        job = self.job
        if job is None:
            result.update(
                state=self.state,
                exit_code=self.exit_code,
                error=self.error,
                duration_ms=self.duration_ms,
                bytes_moved=self.bytes_moved,
                started_at=None,
                finished_at=None,
                progress=None,
                stdout_bytes=0,
                stderr_bytes=0,
            )
            return result
        with job.cond:
            transfer = dict(job.transfer or {})
            result.update(
                state=job.state,
                exit_code=job.exit_code,
                error=job.error,
                duration_ms=job.duration_ms(),
                started_at=job.started_at,
                finished_at=job.finished_at,
                progress=job.progress,
                stdout_bytes=job.stdout.total,
                stderr_bytes=job.stderr.total,
            )
        if self.type == "sync":
            result["bytes_moved"] = int(transfer.get("bytes_copied") or 0)
            result["sync"] = {
                name: transfer[name]
                for name in ("files_total", "files_copied", "files_skipped", "bytes_copied")
                if name in transfer
            }
        elif self.type in ("upload", "download"):
            result["bytes_moved"] = int(transfer.get("size") or 0)
        else:
            result["bytes_moved"] = 0
        return result

    def output(self, limit: int) -> tuple[list[StreamPiece], bool]:
        """The newest ``limit`` bytes of output in arrival order, and whether older output was dropped."""
        job = self.job
        if job is None or self.type != "exec":
            return [], False
        pieces, _, _ = job.next_pieces(0, 0)
        dropped = any(piece.skipped for piece in pieces)
        kept: list[StreamPiece] = []
        size = 0
        for piece in reversed(pieces):
            if not piece.data:
                continue
            room = limit - size
            if room <= 0:
                dropped = True
                break
            data = piece.data
            if len(data) > room:
                data, dropped = data[-room:], True
            kept.append(StreamPiece(piece.stream, data))
            size += len(data)
        kept.reverse()
        return kept, dropped


@dataclass(frozen=True)
class ProbeSample:
    at: float
    status: str
    latency_ms: int | None


@dataclass(frozen=True)
class CallSample:
    at: float
    surface: str
    status: int
    seconds: float


class HubMetrics:
    """Everything the dashboard reports, held in memory."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.started_at = clock()
        self._lock = threading.Lock()
        self._entries: list[JobEntry] = []
        self._by_id: dict[str, JobEntry] = {}
        self._probes: dict[str, deque[ProbeSample]] = {}
        self._last_probe: dict[str, ProbeResult] = {}
        self._status_since: dict[str, float] = {}
        self._calls: deque[CallSample] = deque()
        self.restored_jobs = 0
        self.restored_since: float | None = None

    # ---- recording ------------------------------------------------------------

    def job_added(self, job: HubJob) -> None:
        entry = JobEntry(
            id=job.id,
            target_id=job.target.id,
            type=job.type,
            language=job.spec.get("language") if job.type == "exec" else None,
            command=command_of(job.type, job.spec),
            origin=job.origin,
            created_at=job.created_at,
            broadcast_id=job.broadcast_id,
            job=job,
        )
        with self._lock:
            self._entries.append(entry)
            self._by_id[job.id] = entry

    def probe(self, target: Target, result: ProbeResult) -> None:
        sample = ProbeSample(result.checked_at, result.status, result.latency_ms)
        with self._lock:
            samples = self._probes.setdefault(target.id, deque())
            previous = self._last_probe.get(target.id)
            if previous is None or previous.status != result.status:
                self._status_since[target.id] = result.checked_at
            self._last_probe[target.id] = result
            samples.append(sample)
            self._trim(samples, lambda item: item.at)

    def call(self, surface: str, status: int, seconds: float) -> None:
        with self._lock:
            self._calls.append(CallSample(self._clock(), surface, status, seconds))
            self._trim(self._calls, lambda item: item.at)

    def _trim(self, samples: deque[Any], at: Callable[[Any], float]) -> None:
        cutoff = self._clock() - SAMPLE_RETENTION_S
        while samples and at(samples[0]) < cutoff:
            samples.popleft()

    def restore(self, path: Path) -> int:
        """Load the last week's jobs from the audit log (commands and outcomes, not output)."""
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - BACKFILL_MAX_BYTES))
                raw = stream.read()
        except OSError:
            return 0
        if size > BACKFILL_MAX_BYTES:
            raw = raw.split(b"\n", 1)[-1]
        cutoff = self._clock() - BACKFILL_S
        restored: dict[str, JobEntry] = {}
        for line in raw.decode("utf-8", "replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            event, job_id = record.get("event"), record.get("job_id")
            if not isinstance(job_id, str) or event not in ("job.start", "job.finish"):
                continue
            at = _parse_ts(record.get("ts"))
            if at is None or at < cutoff:
                continue
            if event == "job.start":
                job_type = str(record.get("type") or "exec")
                restored[job_id] = JobEntry(
                    id=job_id,
                    target_id=str(record.get("target") or "?"),
                    type=job_type,
                    language=record.get("language") if job_type == "exec" else None,
                    command=command_of(job_type, record),
                    origin=str(record.get("origin") or "?"),
                    created_at=at,
                    broadcast_id=record.get("broadcast_id"),
                    command_truncated=bool(record.get("code_truncated")),
                )
            elif job_id in restored:
                entry = restored[job_id]
                entry.state = str(record.get("state") or UNKNOWN)
                entry.exit_code = _int_or_none(record.get("exit_code"))
                entry.error = record.get("error") if isinstance(record.get("error"), str) else None
                entry.duration_ms = _int_or_none(record.get("duration_ms"))
                entry.bytes_moved = _int_or_none(record.get("bytes_copied") or record.get("size")) or 0
        entries = sorted(restored.values(), key=lambda item: item.created_at)
        for entry in entries:
            if entry.state == UNKNOWN:
                entry.error = "the hub stopped before this job finished"
        with self._lock:
            fresh = [entry for entry in entries if entry.id not in self._by_id]
            self._entries[:0] = fresh
            self._by_id.update((entry.id, entry) for entry in fresh)
            self.restored_jobs = len(fresh)
            self.restored_since = fresh[0].created_at if fresh else None
        return len(fresh)

    # ---- reading --------------------------------------------------------------

    def entries(self, target_id: str | None = None) -> list[JobEntry]:
        with self._lock:
            entries = list(self._entries)
        if target_id is not None:
            entries = [entry for entry in entries if entry.target_id == target_id]
        return entries

    def entry(self, job_id: str) -> JobEntry | None:
        with self._lock:
            return self._by_id.get(job_id)

    def last_probe(self, target_id: str) -> ProbeResult | None:
        with self._lock:
            return self._last_probe.get(target_id)

    def report(
        self,
        targets: Iterable[Target],
        probes: dict[str, ProbeResult | None],
        *,
        range_name: str = DEFAULT_RANGE,
        jobs_running: int = 0,
        jobs_queued: int = 0,
        max_running: int = 0,
    ) -> dict[str, Any]:
        """Every number on the dashboard, as JSON-ready data."""
        window, bucket = RANGES[range_name]
        now = self._clock()
        count = window // bucket
        # Buckets end at the next bucket boundary so a refresh does not shift them.
        end = (int(now) // bucket + 1) * bucket
        start = end - count * bucket
        with self._lock:
            entries = list(self._entries)
            calls = [call for call in self._calls if call.at >= start]
            samples = {key: [s for s in value if s.at >= start] for key, value in self._probes.items()}
            status_since = dict(self._status_since)

        views = [entry.view() for entry in entries if entry.created_at >= start]
        configured = {target.id: target for target in targets}
        target_ids = list(configured) + sorted(
            {view["target"] for view in views} - set(configured), key=str.lower
        )

        def index(at: float) -> int:
            return min(count - 1, max(0, int((at - start) // bucket)))

        def empty() -> list[int]:
            return [0] * count

        series = {name: empty() for name in ("jobs", "failed", "exec", "transfer", "sync")}
        per_target: dict[str, dict[str, Any]] = {
            target_id: {"series": empty(), "failed_series": empty(), "views": []} for target_id in target_ids
        }
        for view in views:
            slot = index(view["created_at"])
            bad = view["state"] in BAD_STATES
            series["jobs"][slot] += 1
            series[
                "sync" if view["type"] == "sync" else "transfer" if view["type"] in TRANSFER_TYPES else "exec"
            ][slot] += 1
            data = per_target[view["target"]]
            data["series"][slot] += 1
            data["views"].append(view)
            if bad:
                series["failed"][slot] += 1
                data["failed_series"][slot] += 1

        all_last_used: dict[str, float] = {}
        for entry in entries:
            all_last_used[entry.target_id] = max(all_last_used.get(entry.target_id, 0.0), entry.created_at)

        rows = []
        for target_id in target_ids:
            data = per_target[target_id]
            target_views = data["views"]
            target = configured.get(target_id)
            probe = probes.get(target_id)
            finished = [view for view in target_views if view["state"] in (SUCCEEDED, *BAD_STATES)]
            ok = sum(1 for view in finished if view["state"] == SUCCEEDED)
            durations = [view["duration_ms"] for view in target_views if view["duration_ms"] is not None]
            origins = Counter(view["origin"] for view in target_views)
            target_samples = samples.get(target_id, [])
            online_samples = sum(1 for sample in target_samples if sample.status == ONLINE)
            latencies = [
                s.latency_ms for s in target_samples if s.status == ONLINE and s.latency_ms is not None
            ]
            info = (probe.info or {}) if probe is not None else {}
            platform = info.get("platform") or {}
            last_used = all_last_used.get(target_id)
            top_origin = origins.most_common(1)[0] if origins else None
            rows.append(
                {
                    "id": target_id,
                    "machine": machine_name(target.path if target else target_id.rsplit("/", 1)[-1]),
                    "relay": target.relay if target else target_id.split("/", 1)[0],
                    "namespace": target.namespace if target else None,
                    "hybrid_connection": target.path if target else target_id.rsplit("/", 1)[-1],
                    "description": target.description if target else None,
                    "configured": target is not None,
                    "auth": ("token" if target.token is not None else "key") if target else None,
                    "status": probe.status if probe is not None else UNKNOWN,
                    "status_since": utc_iso(status_since.get(target_id)),
                    "reason": probe.error if probe is not None else None,
                    "latency_ms": probe.latency_ms if probe is not None else None,
                    "latency_p50_ms": percentile(latencies, 0.5),
                    "host": info.get("hostname"),
                    "user": info.get("user"),
                    "os": platform.get("description") or platform.get("system"),
                    "listener_version": info.get("privy_version"),
                    "uptime_pct": round(100 * online_samples / len(target_samples), 1)
                    if target_samples
                    else None,
                    "jobs": len(target_views),
                    "running": sum(1 for view in target_views if view["state"] in (RUNNING, QUEUED)),
                    "failed": sum(1 for view in target_views if view["state"] in BAD_STATES),
                    "success_pct": round(100 * ok / len(finished), 1) if finished else None,
                    "duration_p50_ms": percentile(durations, 0.5),
                    "bytes_moved": sum(view["bytes_moved"] for view in target_views),
                    "top_origin": {
                        "origin": top_origin[0],
                        "share_pct": round(100 * top_origin[1] / len(target_views)),
                    }
                    if top_origin
                    else None,
                    "last_used": utc_iso(last_used),
                    "last_used_ts": last_used,
                    "kept_jobs": sum(1 for entry in entries if entry.target_id == target_id),
                    "series": data["series"],
                    "failed_series": data["failed_series"],
                }
            )
        rows.sort(key=lambda row: (-row["jobs"], -(row["last_used_ts"] or 0), row["id"].lower()))
        shared = Counter(row["hybrid_connection"].lower() for row in rows)
        for row in rows:
            name = row["hybrid_connection"]
            row["label"] = row["id"] if shared[name.lower()] > 1 else name

        durations = [view["duration_ms"] for view in views if view["duration_ms"] is not None]
        states = Counter(view["state"] for view in views)
        commands: dict[str, dict[str, Any]] = {}
        for view in views:
            key = " ".join(view["command"].split())[:160]
            if not key:
                continue
            item = commands.setdefault(key, {"command": key, "count": 0, "failed": 0, "targets": set()})
            item["count"] += 1
            item["failed"] += view["state"] in BAD_STATES
            item["targets"].add(view["target"])
        top_commands = sorted(commands.values(), key=lambda item: (-item["count"], item["command"]))[:8]
        for item in top_commands:
            item["targets"] = sorted(item["targets"])
        failures = [view for view in views if view["state"] in BAD_STATES]
        failures.sort(key=lambda view: view["created_at"], reverse=True)
        latencies_all = [
            sample.latency_ms
            for target_samples in samples.values()
            for sample in target_samples
            if sample.status == ONLINE and sample.latency_ms is not None
        ]
        surfaces = Counter(call.surface for call in calls)
        statuses = sum(1 for view in rows if view["status"] == ONLINE and view["configured"])
        bytes_by_type = Counter()
        for view in views:
            bytes_by_type[view["type"]] += view["bytes_moved"]
        return {
            "generated_at": utc_iso(now),
            "range": range_name,
            "window_s": window,
            "bucket_s": bucket,
            "start": utc_iso(start),
            "start_ts": start,
            "end_ts": end,
            "hub_started_at": utc_iso(self.started_at),
            "restored_jobs": self.restored_jobs,
            "history_since": utc_iso(entries[0].created_at) if entries else None,
            "kept_jobs": len(entries),
            "targets_total": len(configured),
            "targets_online": statuses,
            "targets_used": sum(1 for row in rows if row["jobs"]),
            "jobs": {
                "total": len(views),
                "succeeded": states.get(SUCCEEDED, 0),
                "failed": sum(states.get(state, 0) for state in BAD_STATES),
                "cancelled": states.get(CANCELLED, 0),
                "unknown": states.get(UNKNOWN, 0),
                "running": jobs_running,
                "queued": jobs_queued,
                "max_running": max_running,
                "by_type": dict(Counter(view["type"] for view in views)),
                "by_origin": dict(Counter(view["origin"] for view in views)),
                "by_language": dict(Counter(view["language"] for view in views if view["language"])),
                "duration_p50_ms": percentile(durations, 0.5),
                "duration_p95_ms": percentile(durations, 0.95),
                "duration_max_ms": max(durations) if durations else None,
            },
            "bytes_moved": {name: bytes_by_type.get(name, 0) for name in TRANSFER_TYPES},
            "calls": {
                "total": len(calls),
                "by_surface": {name: surfaces.get(name, 0) for name in SURFACES},
                "errors": sum(1 for call in calls if call.status >= 400),
            },
            "probe_latency_ms": {
                "p50": percentile(latencies_all, 0.5),
                "p95": percentile(latencies_all, 0.95),
            },
            "series": series,
            "targets": rows,
            "top_commands": top_commands,
            "recent_failures": [_public(view) for view in failures[:10]],
        }


def _public(view: dict[str, Any]) -> dict[str, Any]:
    result = dict(view)
    result["created_at"] = utc_iso(view["created_at"])
    for name in ("started_at", "finished_at"):
        result[name] = utc_iso(view.get(name))
    result.pop("progress", None)
    return result


def public_report(report: dict[str, Any]) -> dict[str, Any]:
    """The report without internal timestamps, for ``GET /v1/stats``."""
    result = {key: value for key, value in report.items() if not key.endswith("_ts")}
    result["targets"] = [
        {key: value for key, value in row.items() if not key.endswith("_ts")} for row in report["targets"]
    ]
    return result


def _parse_ts(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
