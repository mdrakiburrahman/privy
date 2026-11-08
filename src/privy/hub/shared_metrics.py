"""Shared dashboard snapshots built from summaries, never eager output reads."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from privy.hub.jobs import HubJob, StreamPiece, utc_iso
from privy.hub.metrics import DEFAULT_RANGE, CallSample, HubMetrics, JobEntry, ReportSelection, command_of
from privy.hub.purpose import PurposeHistory, normalize_purpose
from privy.hub.registry import ProbeResult
from privy.hub.shared_jobs import SharedJob, shared_payload, shared_records

log = logging.getLogger(__name__)


class _SharedEntry(JobEntry):
    def view(self):
        result = super().view()
        if self.job._record.get("imported"):
            result.update(
                restored=True,
                command_truncated=self.job._record.get("command_truncated", False),
                duration_ms=self.job._record.get("duration_ms"),
            )
        return result

    def output(self, limit):
        self.job.refresh()
        if self.type != "exec" or limit <= 0:
            return [], False
        # Page backwards from the tail, stopping once the requested bytes fit.
        count = self.job._record.get("chunk_count", 0)
        kept, size, dropped = [], 0, False
        while count > 0 and size < limit:
            index = max(0, count - 200)
            pieces, _, _ = self.job.next_pieces(index, 0)
            dropped |= len(pieces) < count - index
            for piece in reversed(pieces[: count - index]):
                room = max(0, limit - size)
                if not room:
                    dropped = True
                    break
                data = piece.data[-room:]
                dropped |= len(data) < len(piece.data)
                kept.append(StreamPiece(piece.stream, data))
                size += len(data)
            count = index
        return list(reversed(kept)), dropped or count > 0


def _entry(backend, record):
    job = SharedJob(backend, record, snapshot=True)
    return _SharedEntry(
        id=job.id,
        target_id=job.target.id,
        type=job.type,
        language=job.spec.get("language"),
        command=command_of(job.type, job.spec),
        origin=job.origin,
        created_at=job.created_at,
        broadcast_id=job.broadcast_id,
        lease_id=job.lease_id,
        client_id=job.client_id,
        job=job,
    )


class _SharedPurposes:
    def __init__(self, backend, clock):
        self.backend, self._clock = backend, clock

    def record(self, target_id, purpose):
        # Explicit non-job usage (e.g. a terminal) has no admission record.
        at = self._clock()
        key = uuid.uuid4().hex
        self.backend.enqueue(
            "purpose",
            key,
            shared_payload(
                {
                    "id": key,
                    "owner": self.backend.owner,
                    "created_at": at,
                    "target": target_id,
                    "purpose": normalize_purpose(purpose),
                }
            ),
        )

    def _snapshot(self, *, target_id=None, start=None, rows=None):
        if rows is None:
            filters = {"target": target_id}
            if start is not None:
                filters["after"] = start
            rows = list(shared_records(self.backend, "job", **filters))
            rows += list(shared_records(self.backend, "purpose", **filters))
            if start is not None:
                for kind in ("job", "purpose"):
                    rows += self.backend.list(kind, target=target_id, limit=1, before=start)
        else:
            rows = list(rows)
        rows.sort(key=lambda r: (r["created_at"], r.get("job_id", r.get("id", ""))))
        at = [rows[0]["created_at"] if rows else self._clock()]
        history = PurposeHistory(clock=lambda: at[0])
        history._epoch = "shared"
        for row in rows:
            at[0] = row["created_at"]
            history.record(row["target"], row.get("purpose"))
        history._clock = self._clock
        return history

    def summaries(self, target_ids, *, start, end):
        result = {}
        for target_id in target_ids:
            snapshot = self._snapshot(target_id=target_id, start=start)
            result.update(snapshot.summaries([target_id], start=start, end=end))
        return result

    def history(self, target_id, **kwargs):
        result = self._snapshot(target_id=target_id).history(target_id, **kwargs)
        result["ephemeral"] = False
        return result


class SharedMetrics(HubMetrics):
    def __init__(self, backend: Any, registry=None, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__(clock=clock)
        self.backend = backend
        self.registry = registry
        self.purposes = _SharedPurposes(backend, clock)

    def job_added(self, job: HubJob) -> None:
        # The manager writes admission after all callbacks (including lease pinning).
        job.owner = self.backend.owner

    def probe(self, target, result) -> None:
        self.backend.enqueue(
            "probe",
            uuid.uuid4().hex,
            shared_payload(
                {
                    "owner": self.backend.owner,
                    "created_at": result.checked_at,
                    "at": result.checked_at,
                    "target": target.id,
                    "status": result.status,
                    "latency_ms": result.latency_ms,
                    "info": result.info,
                    "legacy": result.legacy,
                    "error": result.error,
                }
            ),
        )

    def call(self, surface, status, seconds) -> None:
        at = self._clock()
        self.backend.enqueue(
            "call",
            uuid.uuid4().hex,
            {
                "owner": self.backend.owner,
                "created_at": at,
                "at": at,
                "surface": surface,
                "status": status,
                "seconds": seconds,
            },
        )

    def restore(self, path) -> int:
        # SQL is authoritative. No local audit file or outputs are loaded on startup.
        return 0

    def entries(
        self,
        target_id=None,
        *,
        after=None,
        before=None,
        lease_id=None,
        limit=None,
        states: set[str] | frozenset[str] | None = None,
    ):
        if limit is None:
            rows = shared_records(
                self.backend,
                "job",
                target=target_id,
                after=after,
                before=before,
                lease_id=lease_id,
            )
            if states is not None:
                rows = (row for row in rows if row.get("state") in states)
        else:
            rows = self.backend.list(
                "job",
                limit=limit,
                target=target_id,
                lease_id=lease_id,
                after=after,
                before=before,
                states=states,
            )
        return sorted(
            (_entry(self.backend, row) for row in rows),
            key=lambda entry: (entry.created_at, entry.id),
        )

    def count_jobs_by_target(self, *, lease_id=None):
        count = getattr(self.backend, "count_jobs_by_target", None)
        if count is not None:
            return count(lease_id=lease_id)
        return super().count_jobs_by_target(lease_id=lease_id)

    def count_jobs_by_lease(self):
        count = getattr(self.backend, "count_jobs_by_lease", None)
        if count is not None:
            return count()
        return super().count_jobs_by_lease()

    def count_job_records(self, *, lease_id=None, states=None, after=None, before=None):
        count = getattr(self.backend, "count_job_records", None)
        if count is not None:
            return count(lease_id=lease_id, states=states, after=after, before=before)
        return super().count_job_records(lease_id=lease_id, states=states, after=after, before=before)

    def entry(self, job_id):
        record = self.backend.get("job", job_id)
        return _entry(self.backend, record) if record is not None else None

    def last_probe(self, target_id):
        rows = self.backend.list("probe", target=target_id, limit=1)
        return self._probe_result(rows[0]) if rows else None

    @staticmethod
    def _probe_result(row):
        return ProbeResult(
            status=row["status"],
            checked_at=row["at"],
            latency_ms=row.get("latency_ms"),
            info=row.get("info"),
            legacy=row.get("legacy", False),
            error=row.get("error"),
        )

    def report(self, targets, probes, **kwargs):
        profile_started = time.perf_counter()
        profile = os.environ.get("PRIVY_SQL_PROFILE") == "1"
        snapshot = HubMetrics(clock=self._clock)
        snapshot.started_at = self.started_at
        selection = kwargs.get("selection") or ReportSelection(kwargs.get("range_name", DEFAULT_RANGE))
        now = self._clock()
        window_start, window_end, _, _ = selection.window(now, self.started_at)
        after = None if selection.range_name == "all" else window_start
        before = window_end if selection.range_name == "custom" else None
        history_read_started = time.perf_counter()
        report_history = getattr(self.backend, "report_history", None)
        report_counts = None
        if report_history is not None:
            history = report_history(after=after, before=before)
            snapshot._entries = sorted(
                (_entry(self.backend, row) for row in history["jobs"]),
                key=lambda entry: (entry.created_at, entry.id),
            )
            purpose_rows = [*history["jobs"], *history["purposes"], *history["latest_before"]]
            probe_rows = history["probes"]
            calls = history["calls"]
            report_counts = history["job_counts"]
        else:
            snapshot._entries = self.entries(after=after, before=before)
            purpose_rows = [entry.job._record for entry in snapshot._entries]
            purpose_rows += list(shared_records(self.backend, "purpose", after=after, before=before))
        targets = list(targets)
        if report_history is None and after is not None:
            if hasattr(self.backend, "latest_before"):
                purpose_rows += self.backend.latest_before(after)
            else:
                target_ids = {target.id for target in targets} | {
                    entry.target_id for entry in snapshot._entries
                }
                for target_id in target_ids:
                    for kind in ("job", "purpose"):
                        purpose_rows += self.backend.list(kind, target=target_id, before=after, limit=1)
        snapshot.purposes = self.purposes._snapshot(rows=purpose_rows)
        earliest = [entry.created_at for entry in snapshot._entries]
        shared_probes = dict(probes)
        if report_history is None:
            probe_rows = list(
                self.backend.probe_history(after=after, before=before)
                if hasattr(self.backend, "probe_history")
                else shared_records(self.backend, "probe", after=after, before=before)
            )
        probe_assembly_started = time.perf_counter()
        for row in sorted(probe_rows, key=lambda r: r["at"]):
            target_id = row["target"]
            result = self._probe_result(row)
            # HubMetrics.probe needs only the target's id.
            target = type("ProbeTarget", (), {"id": target_id})()
            snapshot.probe(target, result)
            previous = shared_probes.get(target_id)
            if previous is None or result.checked_at > previous.checked_at:
                shared_probes[target_id] = result
            earliest.append(row["at"])
        probe_processing_ms = (time.perf_counter() - probe_assembly_started) * 1000
        if report_history is None:
            calls = list(shared_records(self.backend, "call", after=after, before=before))
        calls.sort(key=lambda row: row["at"])
        snapshot._calls.extend(CallSample(r["at"], r["surface"], r["status"], r["seconds"]) for r in calls)
        earliest.extend(r["at"] for r in calls)
        snapshot.started_at = min([self.started_at, *earliest])
        snapshot.restored_jobs = sum(entry.job.owner != self.backend.owner for entry in snapshot._entries)
        history_read_ms = (time.perf_counter() - history_read_started) * 1000
        if any(name not in kwargs for name in ("jobs_running", "jobs_queued", "jobs_retained")):
            counts = report_counts if report_counts is not None else self.backend.count_jobs()
            kwargs.setdefault("jobs_running", counts["running"])
            kwargs.setdefault("jobs_queued", counts["queued"])
            kwargs.setdefault("jobs_retained", counts["retained"])
        report_started = time.perf_counter()
        result = snapshot.report(targets, shared_probes, **kwargs)
        report_math_ms = (time.perf_counter() - report_started) * 1000
        result["hub_started_at"] = utc_iso(self.started_at)
        if profile:
            log.info(
                "sql_profile report_assembly thread=%s duration_ms=%.1f probe_assembly_ms=%.1f "
                "history_read_ms=%.1f metrics_report_ms=%.1f jobs=%d purposes=%d probes=%d calls=%d",
                threading.get_ident(),
                (time.perf_counter() - profile_started) * 1000,
                probe_processing_ms,
                history_read_ms,
                report_math_ms,
                len(snapshot._entries),
                max(0, len(purpose_rows) - len(snapshot._entries)),
                len(probe_rows),
                len(calls),
            )
        return result


SharedHubMetrics = SharedMetrics
