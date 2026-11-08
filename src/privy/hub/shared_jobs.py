"""Shared job metadata and lazy output; workers never migrate between owners."""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from privy._relay import redact_relay_secrets
from privy.hub import console
from privy.hub.config import Target
from privy.hub.jobs import (
    CANCELLED,
    ERROR,
    STREAMS,
    TERMINAL_STATES,
    TICK_S,
    AuditLog,
    Broadcast,
    HubJob,
    JobLookupError,
    JobManager,
    StreamPiece,
    _skip_continuation_bytes,
    _utf8_safe_length,
    utc_iso,
)
from privy.hub.sql_store import StorageError

log = logging.getLogger("privy.hub")
_PRIVATE = frozenset({"key", "token", "secret", "password", "authorization", "client", "socket", "request"})
IMPORT_MAX_BYTES = 64 * 1024 * 1024
IMPORT_LINE_BYTES = 1024 * 1024
IMPORT_HISTORY_S = 7 * 24 * 60 * 60
_IMPORT_SECRET = re.compile(
    r"""(?i)(\b(?:password|passwd|token|secret|accountkey|sharedaccesskey|connection_string|api_key)\b\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s;,]+)"""
)
_IMPORT_FIELDS = frozenset(
    {
        "ts",
        "event",
        "job_id",
        "type",
        "target",
        "origin",
        "lease_id",
        "client_id",
        "broadcast_id",
        "purpose",
        "language",
        "mode",
        "timeout_s",
        "code_sha256",
        "code",
        "code_truncated",
        "local_path",
        "remote_path",
        "overwrite",
        "destination_type",
        "chunk_size",
        "state",
        "exit_code",
        "error",
        "duration_ms",
        "stdout_bytes",
        "stderr_bytes",
        "size",
        "sha256",
        "files_total",
        "files_copied",
        "files_skipped",
        "files_done",
        "bytes_copied",
    }
)


def _audit_lines(stream):
    while True:
        raw = stream.readline(IMPORT_LINE_BYTES + 1)
        if not raw:
            return
        if len(raw) > IMPORT_LINE_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(IMPORT_LINE_BYTES + 1)
            log.warning("skipping oversized legacy audit line during shared import")
            continue
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(record, dict) or not isinstance(record.get("event"), str):
            continue
        try:
            at = dt.datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
            if at.tzinfo is None:
                continue
            at = at.timestamp()
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError, OSError):
            continue
        if math.isfinite(at):
            yield hashlib.sha256(raw).hexdigest(), at, record


def _import_redacted(record, local_path=None):
    local_path = local_path or record.get("local_path")
    secrets = [
        value
        for name, value in record.items()
        if name.lower() in _PRIVATE and isinstance(value, str) and value
    ]
    safe = shared_payload(
        {key: value for key, value in record.items() if key in _IMPORT_FIELDS},
        local_path=local_path if isinstance(local_path, str) else None,
    )

    def scrub(value):
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "<redacted>")
            return _IMPORT_SECRET.sub(r"\1<redacted>", value)
        return value

    return scrub(safe)


def _import_job(start, finish, digest, at, import_hash):
    target_id = start.get("target")
    job_id = start.get("job_id")
    job_type = start.get("type", "exec")
    if (
        not isinstance(target_id, str)
        or not target_id
        or not isinstance(job_id, str)
        or not job_id
        or len(job_id) > 80
        or job_type not in ("exec", "upload", "download", "sync")
    ):
        return None
    if job_type == "exec":
        spec = {
            "language": start.get("language") or "python",
            "code": str(start.get("code") or ""),
            "mode": start.get("mode") or "subprocess",
            "timeout_s": start.get("timeout_s"),
        }
    else:
        spec = {
            "local_path": "<owner-local>",
            "remote_path": str(start.get("remote_path") or "?"),
            "overwrite": bool(start.get("overwrite")),
            "chunk_size": start.get("chunk_size"),
        }
        if job_type == "sync":
            spec["destination_type"] = start.get("destination_type") or "directory"
    terminal = finish is not None and finish[1].get("state") in TERMINAL_STATES
    outcome = finish[1] if terminal else {}
    duration = outcome.get("duration_ms")
    duration = duration if type(duration) is int and duration >= 0 else None
    transfer = None
    if job_type in ("upload", "download") and terminal:
        transfer = {key: outcome[key] for key in ("size", "sha256") if key in outcome}
    elif job_type == "sync" and terminal:
        transfer = {
            key: outcome[key]
            for key in ("files_copied", "files_skipped", "files_done", "bytes_copied")
            if key in outcome
        }
    return {
        "job_id": job_id,
        "target": target_id,
        "owner": f"file-import:{import_hash[:32]}",
        "type": job_type,
        "spec": spec,
        "created_at": at,
        "origin": start.get("origin") or "file-import",
        "purpose": start.get("purpose"),
        "lease_id": start.get("lease_id"),
        "client_id": start.get("client_id"),
        "broadcast_id": start.get("broadcast_id"),
        "lease_warning": None,
        "state": outcome["state"] if terminal else "unknown",
        "exit_code": outcome.get("exit_code"),
        "error": outcome.get("error")
        if terminal
        else "file hub stopped without a durable outcome; owner unavailable",
        "started_at": None,
        "finished_at": finish[0] if terminal else None,
        "duration_ms": duration,
        "remote_job_id": None,
        "cancel_requested": False,
        "progress": None,
        "sync_progress": None,
        "transfer": transfer,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "chunk_count": 0,
        "legacy_stdout_bytes": outcome.get("stdout_bytes", 0),
        "legacy_stderr_bytes": outcome.get("stderr_bytes", 0),
        "output_unavailable": True,
        "imported": True,
        "import_sha256": import_hash,
        "start_sha256": digest,
        "command_truncated": bool(start.get("code_truncated")),
    }


def shared_payload(value: Any, *, local_path: str | None = None) -> Any:
    """Only plain JSON data, without credentials or transferable owner-local paths."""
    if isinstance(value, Mapping):
        return {
            key: "<owner-local>" if key == "local_path" else shared_payload(item, local_path=local_path)
            for key, item in value.items()
            if isinstance(key, str)
            and key.lower() not in _PRIVATE
            and (item is None or isinstance(item, (str, bool, int, float, dict, list, tuple)))
        }
    if isinstance(value, (list, tuple)):
        return [
            shared_payload(item, local_path=local_path)
            for item in value
            if item is None or isinstance(item, (str, bool, int, float, dict, list, tuple))
        ]
    if isinstance(value, str):
        value = value.replace(local_path, "<owner-local>") if local_path else value
        return redact_relay_secrets(value)
    return value


def job_record(job: HubJob, owner: str) -> dict[str, Any]:
    with job.cond:
        record = {
            "job_id": job.id,
            "owner": owner,
            "created_at": job.created_at,
            "target": job.target.id,
            "type": job.type,
            "spec": job.spec,
            "origin": job.origin,
            "broadcast_id": job.broadcast_id,
            "purpose": job.purpose,
            "lease_id": job.lease_id,
            "client_id": job.client_id,
            "lease_warning": job.lease_warning,
            "state": job.state,
            "exit_code": job.exit_code,
            "error": job.error,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "remote_job_id": job.remote_job_id,
            "cancel_requested": job.cancel_requested,
            "progress": job.progress,
            "transfer": job.transfer,
            "sync_progress": job.sync_progress,
            "stdout_bytes": job.stdout.total,
            "stderr_bytes": job.stderr.total,
            "chunk_count": job.chunks_base + len(job.chunks),
        }
        return shared_payload(record, local_path=job.spec.get("local_path"))


class SharedAuditLog(AuditLog):
    def __init__(self, backend: Any) -> None:
        super().__init__(None)
        self.backend = backend

    def record(self, event: str, **fields: Any) -> None:
        at = time.time()
        payload = shared_payload(
            {"event": event, **fields, "owner": self.backend.owner, "created_at": at, "ts": utc_iso(at)},
            local_path=fields.get("local_path"),
        )
        self.backend.enqueue("audit", uuid.uuid4().hex, payload)


class _SharedBuffer:
    def __init__(self, total: int = 0) -> None:
        self.total = total
        self.start = 0


class SharedJob(HubJob):
    """Read-only projection of a durable job, including jobs owned by other hubs."""

    poll_interval = TICK_S

    def __init__(self, backend: Any, record: dict[str, Any], *, snapshot: bool = False) -> None:
        self.backend = backend
        self._snapshot = snapshot
        target_id = record["target"]
        relay, _, path = target_id.partition("/")
        super().__init__(
            job_type=record["type"],
            target=Target(target_id, relay, "", path),
            spec=record["spec"],
            origin=record["origin"],
            output_limit=0,
        )
        self.id = record["job_id"]
        self.stdout = _SharedBuffer()
        self.stderr = _SharedBuffer()
        self._record: dict[str, Any] = {}
        self._apply(record)

    def _apply(self, record: dict[str, Any]) -> None:
        self._record = record
        for name in (
            "owner",
            "created_at",
            "type",
            "spec",
            "origin",
            "broadcast_id",
            "purpose",
            "lease_id",
            "client_id",
            "lease_warning",
            "state",
            "exit_code",
            "error",
            "started_at",
            "finished_at",
            "remote_job_id",
            "cancel_requested",
            "transfer",
            "sync_progress",
        ):
            setattr(self, name, record.get(name))
        progress = record.get("progress")
        self.progress = tuple(progress) if progress else None
        for stream in STREAMS:
            self.buffer(stream).total = int(record.get(f"{stream}_bytes", 0))

    def refresh(self) -> None:
        if self._snapshot:
            return
        record = self.backend.get("job", self.id)
        if record is not None:
            with self.cond:
                self._apply(record)

    @property
    def done(self) -> bool:
        self.refresh()
        return self.state in TERMINAL_STATES

    def duration_ms(self) -> int | None:
        if self._record.get("imported"):
            return self._record.get("duration_ms")
        return super().duration_ms()

    def summary(self, *, include_created: bool = False, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self.refresh()
        result = super().summary(include_created=include_created)
        result["owner"] = self.owner
        return result

    def _bytes(self, stream: str, offset: int, limit: int, *, chunks=None) -> tuple[int, bytes]:
        # Never skip a temporarily absent chunk: the output row may still be flushing.
        begin = min(max(0, offset), self.buffer(stream).total)
        end = min(begin + max(0, limit), self.buffer(stream).total)
        if chunks is None:
            chunks = self.backend.output(self.id, stream, begin, max(0, end - begin)) if end > begin else []
        data = bytearray()
        position = begin
        for chunk in chunks:
            if chunk["start"] > position:
                break
            raw = base64.b64decode(chunk["data"])
            right = min(end, chunk["end"])
            if right > position:
                data.extend(raw[position - chunk["start"] : right - chunk["start"]])
                position = right
        return begin, bytes(data)

    def read_output(self, stream: str, offset: int, limit: int, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self.refresh()
        done = self.state in TERMINAL_STATES
        begin, data = self._bytes(stream, offset, limit)
        total = self.buffer(stream).total
        if begin + len(data) < total or not done:
            data = data[: _utf8_safe_length(data)]
        return {
            "job_id": self.id,
            "stream": stream,
            "offset": begin,
            "next_offset": begin + len(data),
            "total_bytes": total,
            "data": data.decode("utf-8", "replace"),
            "eof": done and begin + len(data) >= total,
            "done": done,
        }

    def peek(self, stream: str, since: int, limit: int) -> tuple[int, bytes]:
        self.refresh()
        return self._bytes(stream, max(since, self.buffer(stream).total - max(0, limit)), limit)

    def read_cursor(self) -> dict[str, int]:
        self.cursor = self.backend.read_cursors(self.id, STREAMS)
        return dict(self.cursor)

    def view(
        self, *, max_output_bytes: int, offsets=None, advance: bool = True, refresh: bool = True
    ) -> dict[str, Any]:
        result = self.summary(refresh=refresh)
        if self.type != "exec":
            result.update(local_path=self.spec["local_path"], remote_path=self.spec["remote_path"])
            if self.progress is not None:
                result["progress"] = {"bytes_done": self.progress[0], "bytes_total": self.progress[1]}
            if self.type == "sync":
                result.update(
                    destination_type=self.spec["destination_type"],
                    progress=dict(self.sync_progress or {"phase": "queued"}),
                )
            if self.transfer is not None:
                result["result"] = dict(self.transfer)
            return result
        cursor_streams = [stream for stream in STREAMS if offsets is None or offsets.get(stream) is None]
        read_batch = getattr(self.backend, "read_view_output", None)
        if read_batch is not None:
            cursors, chunks = read_batch(
                self.id,
                {
                    stream: (
                        None if offsets is None else offsets.get(stream),
                        self.buffer(stream).total,
                        max_output_bytes,
                    )
                    for stream in STREAMS
                },
            )
        else:
            cursors = self.backend.read_cursors(self.id, cursor_streams) if cursor_streams else {}
            chunks = {}
        candidates = {}
        claims = {}
        for stream in STREAMS:
            explicit = None if offsets is None else offsets.get(stream)
            start = cursors[stream] if explicit is None else explicit
            # Unlike a bounded memory buffer, shared output has not been discarded.
            # Consume a contiguous prefix; a summary is not evidence that its
            # newest output chunks have reached SQL yet.
            desired = start
            begin, data = self._bytes(stream, desired, max_output_bytes, chunks=chunks.get(stream))
            lead = _skip_continuation_bytes(data)
            begin, data = begin + lead, data[lead:]
            if begin + len(data) < self.buffer(stream).total or self.state not in TERMINAL_STATES:
                data = data[: _utf8_safe_length(data)]
            end = begin + len(data)
            if advance and explicit is None and data:
                claims[stream] = end
            candidates[stream] = (start, begin, data)
        claim_batch = getattr(self.backend, "advance_cursors", None)
        previous_offsets = (
            claim_batch(self.id, claims)
            if claims and claim_batch is not None
            else {stream: self.backend.advance_cursor(self.id, stream, end) for stream, end in claims.items()}
        )
        for stream in STREAMS:
            start, begin, data = candidates[stream]
            if stream in claims:
                previous = previous_offsets[stream]
                # Another hub may have consumed some or all of our candidate bytes.
                consumed = min(len(data), max(0, previous - begin))
                begin, data = begin + consumed, data[consumed:]
                start = max(start, previous)
                self.cursor[stream] = max(previous, claims[stream])
            if not data:
                begin = start
            result[stream] = data.decode("utf-8", "replace")
            result[f"{stream}_offset"] = begin + len(data)
            if begin > start:
                result[f"{stream}_skipped_bytes"] = begin - start
        return result

    def next_pieces(self, index: int, timeout: float) -> tuple[list[StreamPiece], int, bool]:
        deadline = time.monotonic() + max(0, timeout)
        while True:
            self.refresh()
            rows = self.backend.pieces(self.id, index, limit=200)
            pieces = []
            for row in rows:
                if row["index"] != index:
                    break
                pieces.append(StreamPiece(row["stream"], base64.b64decode(row["data"])))
                index += 1
            complete = self.state in TERMINAL_STATES and index >= self._record.get("chunk_count", 0)
            if pieces or complete or time.monotonic() >= deadline:
                return pieces, index, complete
            time.sleep(min(TICK_S, max(0, deadline - time.monotonic())))


class SharedJobManager(JobManager):
    """Keep local workers in memory and expose SQL-backed jobs without recreating them."""

    def __init__(self, registry, *, backend: Any, cancel_poll_s: float = TICK_S, **kwargs) -> None:
        self.backend = backend
        kwargs.setdefault("audit", SharedAuditLog(backend))
        super().__init__(registry, **kwargs)
        self._cancel_poll_s = max(0.01, cancel_poll_s)
        self._stop = threading.Event()
        self._audit_import_stop = threading.Event()
        self._audit_import_thread: threading.Thread | None = None
        self._pending_lock = threading.Lock()
        self._pending_finishes: dict[str, HubJob] = {}
        self._cancel_thread = threading.Thread(
            target=self._monitor_cancels, name="privy-shared-cancel", daemon=True
        )
        self._cancel_thread.start()

    def _set_migration_status(self, status: dict[str, Any]) -> None:
        setter = getattr(self.backend, "set_migration_status", None)
        if setter is not None:
            setter(status)

    def start_audit_import(self, path: Path) -> None:
        if self._audit_import_thread is not None:
            return
        self._set_migration_status({"state": "running", "source": "audit.jsonl"})

        def run() -> None:
            try:
                imported = self.import_audit(path, timeout=300, cancel_event=self._audit_import_stop)
                self._set_migration_status(
                    {"state": "complete", "imported_jobs": imported, "source": "audit.jsonl"}
                )
                log.info("Azure SQL audit import completed; imported %s jobs", imported)
            except Exception as exc:
                if self._audit_import_stop.is_set():
                    self._set_migration_status({"state": "stopped", "source": "audit.jsonl"})
                    return
                self._set_migration_status({"state": "failed", "error": str(exc), "source": "audit.jsonl"})
                log.exception("Azure SQL audit import failed; it can be retried on restart")

        self._audit_import_thread = threading.Thread(target=run, name="privy-sql-audit-import", daemon=True)
        self._audit_import_thread.start()

    def import_audit(
        self,
        path: Path,
        *,
        now: float | None = None,
        timeout: float = 300,
        cancel_event: threading.Event | None = None,
    ) -> int:
        """Import a supplied migration home's bounded, last-week audit once.

        No worker or lease callback is invoked. Flush both before resuming a
        partial import and before committing its content-addressed marker.
        """
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("audit import time must be finite")
        try:
            stream = Path(path).open("rb")
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise StorageError("cannot read migration audit") from exc
        with stream:
            initial_stat = os.fstat(stream.fileno())
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(0)
            content = hashlib.sha256()
            remaining = size
            while remaining:
                if cancel_event is not None and cancel_event.is_set():
                    raise StorageError("audit import stopped before completion")
                data = stream.read(min(remaining, IMPORT_LINE_BYTES))
                if not data:
                    raise StorageError("migration audit changed during hashing")
                content.update(data)
                remaining -= len(data)
            digest = content.hexdigest()
            transaction = getattr(self.backend, "transaction", None)
            guard = transaction("privy.audit.import") if transaction else contextlib.nullcontext()
            with guard:
                marker_key = f"audit:{digest}"
                if self.backend.get("import", marker_key) is not None:
                    return 0
                begin = max(0, size - IMPORT_MAX_BYTES)
                stream.seek(begin)
                if begin:
                    stream.readline(IMPORT_LINE_BYTES + 1)
                starts, finishes, local_paths = {}, {}, {}
                cutoff = now - IMPORT_HISTORY_S
                for line_hash, at, raw in _audit_lines(stream):
                    if cancel_event is not None and cancel_event.is_set():
                        raise StorageError("audit import stopped before completion")
                    if not cutoff <= at <= now or raw["event"] not in ("job.start", "job.finish"):
                        continue
                    job_id = raw.get("job_id")
                    if not isinstance(job_id, str):
                        continue
                    safe = _import_redacted(raw)
                    if raw["event"] == "job.start":
                        starts.setdefault(job_id, (line_hash, at, safe))
                        local_path = raw.get("local_path")
                        if isinstance(local_path, str):
                            local_paths[job_id] = local_path
                    else:
                        previous = finishes.get(job_id)
                        if previous is None or at >= previous[0]:
                            finishes[job_id] = (at, safe)
                existing_jobs = (
                    self.backend.existing("job", list(starts))
                    if hasattr(self.backend, "existing")
                    else {job_id for job_id in starts if self.backend.get("job", job_id) is not None}
                )
                imported = 0
                for index, (job_id, (line_hash, at, start)) in enumerate(starts.items(), 1):
                    if cancel_event is not None and cancel_event.is_set():
                        raise StorageError("audit import stopped before completion")
                    finish = finishes.get(job_id)
                    finish = finish if finish is not None and finish[0] >= at else None
                    if finish is not None:
                        finish = (finish[0], _import_redacted(finish[1], local_paths.get(job_id)))
                    record = _import_job(start, finish, line_hash, at, digest)
                    if record is None:
                        continue
                    if job_id not in existing_jobs:
                        self.backend.enqueue("job", job_id, record)
                        imported += 1
                    self.backend.enqueue(
                        "audit",
                        f"file:{line_hash}",
                        {
                            **start,
                            "created_at": at,
                            "owner": record["owner"],
                            "imported": True,
                        },
                    )
                    if index % 1000 == 0 and hasattr(self.backend, "set_migration_status"):
                        self._set_migration_status(
                            {
                                "state": "running",
                                "phase": "writing",
                                "jobs_found": len(starts),
                                "jobs_processed": index,
                                "source": "audit.jsonl",
                            }
                        )
                # Read again for deterministic finish audit keys, without keeping
                # all raw audit lines or loading any shared output.
                stream.seek(begin)
                if begin:
                    stream.readline(IMPORT_LINE_BYTES + 1)
                for line_hash, at, raw in _audit_lines(stream):
                    if cancel_event is not None and cancel_event.is_set():
                        raise StorageError("audit import stopped before completion")
                    if (
                        cutoff <= at <= now
                        and raw["event"] == "job.finish"
                        and isinstance(raw.get("job_id"), str)
                        and raw["job_id"] in starts
                    ):
                        self.backend.enqueue(
                            "audit",
                            f"file:{line_hash}",
                            {
                                **_import_redacted(raw, local_paths.get(raw["job_id"])),
                                "created_at": at,
                                "owner": f"file-import:{digest[:32]}",
                                "imported": True,
                            },
                        )
                final_stat = os.fstat(stream.fileno())
                if final_stat.st_size != size or final_stat.st_mtime_ns != initial_stat.st_mtime_ns:
                    raise StorageError("migration audit changed during import; no marker committed")
                if not self.backend.flush(timeout=timeout):
                    raise StorageError("cannot commit audit import before durable delivery")
                self.backend.put(
                    "import",
                    marker_key,
                    {
                        "owner": self.backend.owner,
                        "created_at": now,
                        "sha256": digest,
                        "jobs": len(starts),
                        "window_s": IMPORT_HISTORY_S,
                    },
                )
                return imported

    def _admit(self, job: HubJob) -> None:
        self.backend.put("job", job.id, job_record(job, self.backend.owner))
        job._shared_admitted = True

    def _launch_failed(self, job: HubJob, admitted: bool) -> None:
        if not getattr(job, "_shared_admitted", False):
            # A failed SQL drain may have left a durable admission in the outbox.
            # Its outcome is uncertain, so never release its pin.
            log.error("shared job %s admission failed; retaining any admission pin", job.id)
            return
        with job.cond:
            self._finish_locked(job, ERROR, error="hub could not launch the worker")

    def _job_changed(self, job: HubJob) -> None:
        try:
            self.backend.enqueue("job", job.id, job_record(job, self.backend.owner))
        except Exception:
            log.exception("cannot persist shared job %s; its pin must remain conservative", job.id)
            raise

    @staticmethod
    def _audit_fields(job: HubJob, *, include_spec: bool) -> dict[str, Any]:
        return shared_payload(
            JobManager._audit_fields(job, include_spec=include_spec),
            local_path=job.spec.get("local_path"),
        )

    def _finish_locked(self, job: HubJob, state: str, *, error: str | None = None) -> None:
        if job.done:
            return
        job.state, job.error, job.finished_at = state, error, time.time()
        with self._pending_lock:
            self._pending_finishes[job.id] = job
        self._complete_locked(job)
        job.cond.notify_all()

    def _complete_locked(self, job: HubJob) -> None:
        try:
            if not getattr(job, "_shared_completion_durable", False):
                self._job_changed(job)
                job._shared_completion_durable = True
            if not getattr(job, "_shared_unpinned", False):
                if self.on_finish is not None:
                    self.on_finish(job)
                job._shared_unpinned = True
            if not getattr(job, "_shared_audited", False):
                self._audit.record("job.finish", **self._audit_fields(job, include_spec=False))
                job._shared_audited = True
            console.job_finished(job)
            with self._pending_lock:
                self._pending_finishes.pop(job.id, None)
        except Exception:
            log.exception("shared job %s completion failed; retaining pin and retrying", job.id)

    def _retry_finishes(self) -> None:
        with self._pending_lock:
            jobs = list(self._pending_finishes.values())
        for job in jobs:
            with job.cond:
                self._complete_locked(job)

    def _output_record(self, job: HubJob, stream: str, start: int, data: bytes) -> None:
        index = job.chunks_base + len(job.chunks)
        self.backend.enqueue(
            "output",
            f"{job.id}:{index:020d}",
            {
                "job_id": job.id,
                "owner": self.backend.owner,
                "target": job.id,
                "index": index,
                "stream": stream,
                "start": start,
                "end": start + len(data),
                "data": base64.b64encode(data).decode("ascii"),
                "created_at": time.time(),
            },
        )

    def _launch(self, job, body):
        super()._launch(job, body)
        return self.get(job.id)

    def _run(self, job, body):
        try:
            super()._run(job, body)
        except Exception:
            log.exception("shared worker %s failed; preserving its pin until durable completion", job.id)
            with job.cond:
                self._finish_locked(job, ERROR, error="shared worker could not persist its state")

    def get(self, job_id: str) -> SharedJob:
        reader = getattr(self.backend, "read_model", self.backend)
        record = reader.get("job", job_id.strip())
        if record is None:
            raise JobLookupError(f"unknown job {job_id!r}")
        return SharedJob(reader, record)

    def list(self, *, target_id=None, limit=20, lease_id=None, client_id=None):
        reader = getattr(self.backend, "read_model", self.backend)
        return [
            SharedJob(reader, row, snapshot=True)
            for row in reader.list(
                "job",
                limit=limit,
                target=target_id,
                lease_id=lease_id,
                client_id=client_id,
            )
        ]

    def counts(self):
        return getattr(self.backend, "read_model", self.backend).count_jobs()

    def start_broadcast(self, *args, **kwargs):
        broadcast, jobs = super().start_broadcast(*args, **kwargs)
        self.backend.put("broadcast", broadcast.id, {**asdict(broadcast), "owner": self.backend.owner})
        return broadcast, jobs

    def get_broadcast(self, broadcast_id):
        record = self.backend.get("broadcast", broadcast_id.strip())
        if record is None:
            raise JobLookupError(f"unknown broadcast {broadcast_id!r}")
        broadcast = Broadcast(
            **{name: record[name] for name in ("id", "created_at", "patterns", "job_ids", "skipped")}
        )
        return broadcast, [self.get(job_id) for job_id in broadcast.job_ids]

    def cancel(self, job_id, *, current: SharedJob | None = None):
        if current is not None and current.id != job_id.strip():
            raise JobLookupError(f"unknown job {job_id!r}")
        job = current if current is not None else self.get(job_id)
        if not job.done:
            self.backend.request_cancel(job.id)
        return job

    def _process_cancels(self) -> None:
        for job_id in self.backend.pending_cancels():
            with self._lock:
                job = self._jobs.get(job_id)
            # Old incarnations never acknowledge, execute, or unpin abandoned work.
            if job is None:
                continue
            try:
                with job.cond:
                    if not job.done:
                        if not job.cancel_requested:
                            console.cancel_requested(job)
                        job.cancel_requested = True
                        self._job_changed(job)
                        if job.state == "queued":
                            self._finish_locked(job, CANCELLED, error="cancelled before it started")
                        elif job.type in ("exec", "sync"):
                            if not job.remote_job_id:
                                continue
                            job.client.cancel(job.request, job.remote_job_id)
                self.backend.ack_cancel(job_id)
            except Exception:
                log.exception("shared cancellation of %s failed; will retry", job_id)

    def _adopt_remote(self, job: HubJob, remote_job_id: str) -> None:
        with job.cond:
            job.remote_job_id = remote_job_id
            self._job_changed(job)
        # The durable cancellation monitor invokes and retries cancellation.
        # Do not use the file-mode best-effort path, which swallows failures.

    def _monitor_cancels(self) -> None:
        while not self._stop.wait(self._cancel_poll_s):
            try:
                self._retry_finishes()
                self._process_cancels()
            except Exception:
                log.exception("shared cancellation polling failed; will retry")

    def close(self, timeout: float = 10) -> bool:
        self._stop.set()
        self._audit_import_stop.set()
        if self._audit_import_thread is not None:
            self._audit_import_thread.join(timeout)
            if self._audit_import_thread.is_alive():
                log.error("Azure SQL audit import did not stop; storage must remain open")
                return False
        self._cancel_thread.join(timeout)
        self._retry_finishes()
        flushed = self.backend.flush(timeout=timeout)
        with self._pending_lock:
            return flushed and not self._pending_finishes

    @property
    def audit_import_running(self) -> bool:
        return bool(self._audit_import_thread and self._audit_import_thread.is_alive())


def shared_records(backend: Any, kind: str, *, before=None, **filters):
    """Page summary records, never output, without retaining them at startup."""
    if hasattr(backend, "scan"):
        yield from backend.scan(kind, before=before, **filters)
        return
    while True:
        page = backend.list(kind, limit=200, before=before, **filters)
        if len(page) < 200:
            yield from page
            return
        next_before = min(row.get("created_at", row.get("at", 0)) for row in page)
        if before is not None and next_before >= before:
            raise StorageError("shared record pagination did not advance")
        yield from (row for row in page if row.get("created_at", row.get("at", 0)) > next_before)
        # A timestamp-only cursor must drain its entire boundary timestamp before
        # advancing, otherwise equally timed jobs/probes silently disappear.
        limit = 200
        while True:
            boundary = backend.list(
                kind, limit=limit, before=math.nextafter(next_before, math.inf), **filters
            )
            equal = [row for row in boundary if row.get("created_at", row.get("at", 0)) == next_before]
            if len(boundary) < limit or len(equal) < len(boundary):
                yield from equal
                break
            if limit == 100_000:
                raise StorageError("shared timestamp exceeds the maximum SQL page size")
            limit = min(100_000, limit * 2)
        before = next_before
