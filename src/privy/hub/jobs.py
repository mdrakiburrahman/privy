"""Hub-side jobs: commands and file transfers running on targets, plus broadcasts.

Every remote command runs through :meth:`privy.RelayClient.send` on a worker
thread, streaming its output into bounded buffers so callers can return quickly
and come back for the rest. Jobs live in hub memory only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from privy._relay import redact_relay_secrets
from privy.hub.config import Target
from privy.hub.registry import TargetRegistry
from privy.protocol import ExecRequest
from privy.transfer import DEFAULT_CHUNK_SIZE, TransferError

log = logging.getLogger("privy.hub")

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMED_OUT = "timed_out"
CANCELLED = "cancelled"
ERROR = "error"
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, TIMED_OUT, CANCELLED, ERROR})

DEFAULT_MAX_RUNNING = 32
DEFAULT_RETENTION_S = 60 * 60.0
DEFAULT_MAX_RETAINED = 200
DEFAULT_OUTPUT_LIMIT_BYTES = 1024 * 1024
AUDIT_CODE_CHARS = 4096


class JobLookupError(LookupError):
    """No job (or broadcast) with that id is retained by the hub."""


class HubBusyError(RuntimeError):
    """Too many jobs are already queued."""


class _TransferCancelled(Exception):
    pass


def utc_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    moment = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utf8_safe_length(data: bytes) -> int:
    """Length of ``data`` without a trailing, incomplete UTF-8 sequence."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0b1100_0000 == 0b1000_0000:
            continue  # continuation byte; keep looking for the lead byte
        if byte & 0b1000_0000 == 0:
            return len(data)
        needed = 2 if byte >> 5 == 0b110 else 3 if byte >> 4 == 0b1110 else 4
        return len(data) if back >= needed else len(data) - back
    return len(data)


class OutputBuffer:
    """Append-only byte stream keeping at most ``limit`` trailing bytes.

    Offsets are absolute positions in the full stream, so they stay valid after
    old bytes are dropped.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(0, limit)
        self._data = bytearray()
        self._start = 0

    @property
    def total(self) -> int:
        return self._start + len(self._data)

    @property
    def start(self) -> int:
        return self._start

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._data += chunk
        overflow = len(self._data) - self._limit
        if overflow > 0:
            del self._data[:overflow]
            self._start += overflow

    def read(self, offset: int, limit: int) -> tuple[int, bytes]:
        begin = min(max(offset, self._start), self.total)
        relative = begin - self._start
        return begin, bytes(self._data[relative : relative + max(0, limit)])

    def tail(self, limit: int) -> bytes:
        if limit <= 0:
            return b""
        return bytes(self._data[-limit:])


class HubJob:
    """One command or file transfer the hub runs on a target."""

    def __init__(
        self,
        *,
        job_type: str,
        target: Target,
        spec: dict[str, Any],
        origin: str,
        output_limit: int,
        broadcast_id: str | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.type = job_type
        self.target = target
        self.spec = spec
        self.origin = origin
        self.broadcast_id = broadcast_id
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.state = QUEUED
        self.exit_code: int | None = None
        self.error: str | None = None
        self.remote_job_id: str | None = None
        self.cancel_requested = False
        self.progress: tuple[int, int] | None = None
        self.transfer: dict[str, Any] | None = None
        self.stdout = OutputBuffer(output_limit)
        self.stderr = OutputBuffer(output_limit)
        self.cond = threading.Condition()
        self.client: Any = None
        self.request: ExecRequest | None = None

    @property
    def done(self) -> bool:
        return self.state in TERMINAL_STATES

    def duration_ms(self) -> int | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return int((end - self.started_at) * 1000)

    def view(self, *, max_output_bytes: int) -> dict[str, Any]:
        """JSON-friendly snapshot; output is the tail of each stream."""
        with self.cond:
            result: dict[str, Any] = {
                "job_id": self.id,
                "type": self.type,
                "target": self.target.id,
                "state": self.state,
                "done": self.done,
                "exit_code": self.exit_code,
                "error": self.error,
                "created_at": utc_iso(self.created_at),
                "started_at": utc_iso(self.started_at),
                "finished_at": utc_iso(self.finished_at),
                "duration_ms": self.duration_ms(),
            }
            if self.broadcast_id:
                result["broadcast_id"] = self.broadcast_id
            if self.type == "exec":
                result.update(
                    language=self.spec["language"],
                    mode=self.spec["mode"],
                    timeout_s=self.spec["timeout_s"],
                    remote_job_id=self.remote_job_id,
                )
                for name, buffer in (("stdout", self.stdout), ("stderr", self.stderr)):
                    tail = buffer.tail(max_output_bytes)
                    result[name] = tail.decode("utf-8", "replace")
                    result[f"{name}_bytes"] = buffer.total
                    result[f"{name}_truncated"] = buffer.total > len(tail)
            else:
                result.update(
                    direction=self.type,
                    local_path=self.spec["local_path"],
                    remote_path=self.spec["remote_path"],
                    overwrite=self.spec["overwrite"],
                    progress=(
                        None
                        if self.progress is None
                        else {"bytes_done": self.progress[0], "bytes_total": self.progress[1]}
                    ),
                    result=self.transfer,
                )
            return result

    def read_output(self, stream: str, offset: int, limit: int) -> dict[str, Any]:
        with self.cond:
            buffer = self.stdout if stream == "stdout" else self.stderr
            begin, data = buffer.read(offset, limit)
            total = buffer.total
            done = self.done
        if begin + len(data) < total:
            data = data[: _utf8_safe_length(data)]
        end = begin + len(data)
        return {
            "job_id": self.id,
            "stream": stream,
            "offset": begin,
            "next_offset": end,
            "total_bytes": total,
            "skipped_bytes": max(0, begin - offset),
            "data": data.decode("utf-8", "replace"),
            "eof": done and end >= total,
            "done": done,
        }


@dataclass
class Broadcast:
    """The same command started on several targets at once."""

    id: str
    created_at: float
    patterns: list[str]
    language: str
    job_ids: list[str]
    skipped: list[dict[str, Any]] = field(default_factory=list)


class AuditLog:
    """Append-only JSON Lines record of everything the hub was asked to run."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._warned = False

    def record(self, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        line = json.dumps({"ts": utc_iso(time.time()), "event": event, **fields}, ensure_ascii=False)
        with self._lock:
            try:
                # backslashreplace keeps a stray surrogate as a JSON escape instead of failing.
                with self.path.open("a", encoding="utf-8", errors="backslashreplace") as stream:
                    stream.write(line + "\n")
            except OSError as exc:
                if not self._warned:
                    log.warning("Cannot write audit log %s: %s", self.path, exc)
                    self._warned = True


class JobManager:
    """Start, track, cancel, and retain hub jobs."""

    def __init__(
        self,
        registry: TargetRegistry,
        *,
        max_running: int = DEFAULT_MAX_RUNNING,
        max_queued: int | None = None,
        retention_s: float = DEFAULT_RETENTION_S,
        max_retained: int = DEFAULT_MAX_RETAINED,
        output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
        audit: AuditLog | None = None,
    ) -> None:
        self._registry = registry
        self._slots = threading.BoundedSemaphore(max_running)
        self.max_running = max_running
        self._max_queued = max_queued if max_queued is not None else max_running * 8
        self._retention_s = retention_s
        self._max_retained = max_retained
        self._output_limit = output_limit_bytes
        self._audit = audit or AuditLog(None)
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, HubJob] = OrderedDict()
        self._broadcasts: OrderedDict[str, Broadcast] = OrderedDict()

    # ---- starting work ----------------------------------------------------

    def start_exec(
        self,
        target: Target,
        *,
        language: str,
        code: str,
        mode: str,
        timeout_s: float,
        origin: str,
        broadcast_id: str | None = None,
    ) -> HubJob:
        job = HubJob(
            job_type="exec",
            target=target,
            spec={"language": language, "code": code, "mode": mode, "timeout_s": timeout_s},
            origin=origin,
            output_limit=self._output_limit,
            broadcast_id=broadcast_id,
        )
        return self._launch(job, self._exec_body)

    def start_transfer(
        self,
        target: Target,
        *,
        direction: str,
        local_path: str,
        remote_path: str,
        overwrite: bool,
        origin: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> HubJob:
        if direction not in ("upload", "download"):
            raise ValueError(f"invalid transfer direction: {direction!r}")
        job = HubJob(
            job_type=direction,
            target=target,
            spec={
                "local_path": local_path,
                "remote_path": remote_path,
                "overwrite": overwrite,
                "chunk_size": chunk_size,
            },
            origin=origin,
            output_limit=0,
        )
        return self._launch(job, self._transfer_body)

    def start_broadcast(
        self,
        targets: Sequence[Target],
        *,
        patterns: Sequence[str],
        skipped: Sequence[dict[str, Any]],
        language: str,
        code: str,
        mode: str,
        timeout_s: float,
        origin: str,
    ) -> tuple[Broadcast, list[HubJob]]:
        broadcast_id = "b" + uuid.uuid4().hex[:11]
        self._ensure_capacity(len(targets))
        self._audit.record(
            "broadcast.start",
            broadcast_id=broadcast_id,
            origin=origin,
            patterns=list(patterns),
            targets=[target.id for target in targets],
            skipped=[item["target"] for item in skipped],
            language=language,
        )
        jobs = [
            self.start_exec(
                target,
                language=language,
                code=code,
                mode=mode,
                timeout_s=timeout_s,
                origin=origin,
                broadcast_id=broadcast_id,
            )
            for target in targets
        ]
        broadcast = Broadcast(
            id=broadcast_id,
            created_at=time.time(),
            patterns=list(patterns),
            language=language,
            job_ids=[job.id for job in jobs],
            skipped=list(skipped),
        )
        with self._lock:
            self._broadcasts[broadcast_id] = broadcast
        return broadcast, jobs

    # ---- lookup and control -----------------------------------------------

    def get(self, job_id: str) -> HubJob:
        self._reap()
        with self._lock:
            job = self._jobs.get(job_id.strip())
        if job is None:
            raise JobLookupError(f"unknown job {job_id!r}; finished jobs are kept for a limited time")
        return job

    def get_broadcast(self, broadcast_id: str) -> tuple[Broadcast, list[HubJob]]:
        self._reap()
        with self._lock:
            broadcast = self._broadcasts.get(broadcast_id.strip())
            if broadcast is None:
                raise JobLookupError(f"unknown broadcast {broadcast_id!r}")
            jobs = [self._jobs[job_id] for job_id in broadcast.job_ids if job_id in self._jobs]
        return broadcast, jobs

    def list(self, *, target_id: str | None = None, limit: int = 20) -> list[HubJob]:
        self._reap()
        with self._lock:
            jobs = list(self._jobs.values())
        if target_id is not None:
            jobs = [job for job in jobs if job.target.id.lower() == target_id.lower()]
        return list(reversed(jobs))[:limit]

    def counts(self) -> dict[str, int]:
        with self._lock:
            states = [job.state for job in self._jobs.values()]
        return {
            "queued": states.count(QUEUED),
            "running": states.count(RUNNING),
            "retained": len(states),
        }

    @staticmethod
    def wait(job: HubJob, wait_s: float) -> None:
        deadline = time.monotonic() + max(0.0, wait_s)
        with job.cond:
            while not job.done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                job.cond.wait(remaining)

    @staticmethod
    def wait_all(jobs: Sequence[HubJob], wait_s: float) -> None:
        deadline = time.monotonic() + max(0.0, wait_s)
        for job in jobs:
            JobManager.wait(job, deadline - time.monotonic())

    def cancel(self, job_id: str) -> HubJob:
        job = self.get(job_id)
        with job.cond:
            if job.done:
                return job
            job.cancel_requested = True
            if job.state == QUEUED:
                self._finish_locked(job, CANCELLED, error="cancelled before it started")
                return job
            remote = job.remote_job_id
        if job.type == "exec" and remote:
            self._cancel_remote(job)
        return job

    # ---- internals --------------------------------------------------------

    def _ensure_capacity(self, count: int) -> None:
        with self._lock:
            queued = sum(1 for job in self._jobs.values() if job.state == QUEUED)
        if queued + count > self._max_queued:
            raise HubBusyError(
                f"{queued} job(s) are already queued (limit {self._max_queued}); retry after some finish"
            )

    def _launch(self, job: HubJob, body: Callable[[HubJob], None]) -> HubJob:
        self._ensure_capacity(1)
        with self._lock:
            self._jobs[job.id] = job
        try:
            self._audit.record("job.start", **self._audit_fields(job, include_spec=True))
            thread = threading.Thread(
                target=self._run,
                args=(job, body),
                name=f"privy-hub-job-{job.id}",
                daemon=True,
            )
            thread.start()
        except BaseException:
            # Never leave a job queued that no thread will ever run.
            with self._lock:
                self._jobs.pop(job.id, None)
            raise
        log.info("job %s queued on %s (%s)", job.id, job.target.id, self._describe(job))
        return job

    def _run(self, job: HubJob, body: Callable[[HubJob], None]) -> None:
        with self._slots:
            with job.cond:
                # A job cancelled while queued is already finished.
                start = not job.done
                if start:
                    job.state = RUNNING
                    job.started_at = time.time()
                    job.cond.notify_all()
            if start:
                self._run_body(job, body)
        self._reap()

    def _run_body(self, job: HubJob, body: Callable[[HubJob], None]) -> None:
        try:
            body(job)
        except _TransferCancelled:
            with job.cond:
                self._finish_locked(job, CANCELLED, error="cancelled")
        except TransferError as exc:
            with job.cond:
                self._finish_locked(job, FAILED, error=f"{exc.code}: {exc}")
        except Exception as exc:  # noqa: BLE001 - every failure becomes job state
            status, reason = self._registry.note_failure(job.target, exc)
            with job.cond:
                if job.cancel_requested:
                    self._finish_locked(job, CANCELLED, error="cancelled")
                else:
                    self._finish_locked(job, ERROR, error=f"{status}: {reason}")
        with job.cond:
            # Defensive: a body that returned without settling the job.
            self._finish_locked(job, ERROR, error="job ended without a result")

    def _exec_body(self, job: HubJob) -> None:
        client = self._registry.client(job.target)
        request = ExecRequest(
            kind=job.spec["language"],
            code=job.spec["code"],
            mode=job.spec["mode"],
            timeout_s=job.spec["timeout_s"],
        )
        with job.cond:
            job.client = client
            job.request = request
        result = client.send(
            request,
            on_output=lambda stream, data: self._append_output(job, stream, data),
            on_submit=lambda remote_job_id: self._adopt_remote(job, remote_job_id),
        )
        with job.cond:
            job.exit_code = result.exit_code
            if job.cancel_requested or result.error == "cancelled":
                state, error = CANCELLED, "cancelled"
            elif result.timed_out:
                state, error = TIMED_OUT, result.error or "timeout"
            elif result.error == "unknown_job":
                state, error = ERROR, "the listener no longer knows this job (was it restarted?)"
            elif result.exit_code == 0 and not result.error:
                state, error = SUCCEEDED, None
            else:
                state, error = FAILED, result.error
            self._finish_locked(job, state, error=error)

    def _transfer_body(self, job: HubJob) -> None:
        client = self._registry.client(job.target)

        def progress(done: int, total: int) -> None:
            with job.cond:
                job.progress = (done, total)
                job.cond.notify_all()
                if job.cancel_requested:
                    raise _TransferCancelled()

        spec = job.spec
        if job.type == "upload":
            result = client.upload_file(
                spec["local_path"],
                spec["remote_path"],
                chunk_size=spec["chunk_size"],
                overwrite=spec["overwrite"],
                progress=progress,
            )
        else:
            result = client.download_file(
                spec["remote_path"],
                spec["local_path"],
                chunk_size=spec["chunk_size"],
                overwrite=spec["overwrite"],
                progress=progress,
            )
        with job.cond:
            job.transfer = result.to_dict()
            job.exit_code = 0
            self._finish_locked(job, SUCCEEDED)

    def _append_output(self, job: HubJob, stream: str, data: bytes) -> None:
        with job.cond:
            (job.stdout if stream == "stdout" else job.stderr).append(data)
            job.cond.notify_all()

    def _adopt_remote(self, job: HubJob, remote_job_id: str) -> None:
        with job.cond:
            job.remote_job_id = remote_job_id
            cancel_now = job.cancel_requested
        if cancel_now:
            self._cancel_remote(job)

    def _cancel_remote(self, job: HubJob) -> None:
        with job.cond:
            client, request, remote = job.client, job.request, job.remote_job_id
        if client is None or request is None or remote is None:
            return
        try:
            client.cancel(request, remote)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning(
                "cancel of job %s on %s failed: %s",
                job.id,
                job.target.id,
                redact_relay_secrets(str(exc)),
            )

    def _finish_locked(self, job: HubJob, state: str, *, error: str | None = None) -> None:
        """Settle ``job`` (caller holds ``job.cond``); audit before waking waiters."""
        if job.done:
            return
        job.state = state
        job.error = error
        job.finished_at = time.time()
        self._audit.record("job.finish", **self._audit_fields(job, include_spec=False))
        log.info(
            "job %s %s on %s%s%s",
            job.id,
            job.state,
            job.target.id,
            "" if job.exit_code is None else f" exit={job.exit_code}",
            "" if not job.error else f" ({job.error})",
        )
        job.cond.notify_all()

    def _reap(self) -> None:
        now = time.time()
        with self._lock:
            finished = [job for job in self._jobs.values() if job.done and job.finished_at is not None]
            drop = {job.id for job in finished if now - job.finished_at > self._retention_s}
            keep = [job for job in finished if job.id not in drop]
            overflow = len(keep) - self._max_retained
            if overflow > 0:
                drop.update(job.id for job in sorted(keep, key=lambda item: item.finished_at)[:overflow])
            for job_id in drop:
                self._jobs.pop(job_id, None)
            for broadcast_id, broadcast in list(self._broadcasts.items()):
                # Keep a broadcast whose targets were all skipped for as long as a job would be.
                expired = now - broadcast.created_at > self._retention_s
                if expired and not any(job_id in self._jobs for job_id in broadcast.job_ids):
                    self._broadcasts.pop(broadcast_id, None)

    @staticmethod
    def _describe(job: HubJob) -> str:
        if job.type == "exec":
            return f"{job.spec['language']}, timeout {job.spec['timeout_s']:g}s"
        return f"{job.type} {job.spec['local_path']} <-> {job.spec['remote_path']}"

    @staticmethod
    def _audit_fields(job: HubJob, *, include_spec: bool) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "job_id": job.id,
            "type": job.type,
            "target": job.target.id,
            "origin": job.origin,
        }
        if job.broadcast_id:
            fields["broadcast_id"] = job.broadcast_id
        if include_spec:
            if job.type == "exec":
                code = job.spec["code"]
                fields.update(
                    language=job.spec["language"],
                    mode=job.spec["mode"],
                    timeout_s=job.spec["timeout_s"],
                    code_sha256=hashlib.sha256(code.encode("utf-8", "surrogatepass")).hexdigest(),
                    code=code[:AUDIT_CODE_CHARS],
                    code_truncated=len(code) > AUDIT_CODE_CHARS,
                )
            else:
                fields.update(
                    local_path=job.spec["local_path"],
                    remote_path=job.spec["remote_path"],
                    overwrite=job.spec["overwrite"],
                )
        else:
            fields.update(
                state=job.state,
                exit_code=job.exit_code,
                error=job.error,
                duration_ms=job.duration_ms(),
            )
            if job.type == "exec":
                fields.update(stdout_bytes=job.stdout.total, stderr_bytes=job.stderr.total)
            elif job.transfer is not None:
                fields.update(size=job.transfer.get("size"), sha256=job.transfer.get("sha256"))
        return fields
