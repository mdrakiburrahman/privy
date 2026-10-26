"""Hub-side jobs: commands and file transfers running on targets, plus broadcasts.

Every remote command runs through :meth:`privy.RelayClient.send` on a worker
thread, streaming its output into bounded buffers. Callers can:

* read only the output produced since their previous read (a per-job cursor),
* follow the output live, chunk by chunk, in arrival order, or
* page through the retained output by byte offset.

Jobs live in hub memory only.
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from privy._relay import redact_relay_secrets
from privy.hub import console
from privy.hub.config import Target
from privy.hub.registry import TargetRegistry
from privy.hub.sync import sync
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
STREAMS = ("stdout", "stderr")

DEFAULT_MAX_RUNNING = 32
DEFAULT_RETENTION_S = 60 * 60.0
DEFAULT_MAX_RETAINED = 200
DEFAULT_OUTPUT_LIMIT_BYTES = 1024 * 1024
AUDIT_CODE_CHARS = 4096
MAX_TRACKED_CHUNKS = 20_000

#: ``on_tick()`` is called while a caller waits, at least every :data:`TICK_S`.
TICK_S = 0.25


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


def _skip_continuation_bytes(data: bytes) -> int:
    lead = 0
    while lead < len(data) and lead < 3 and data[lead] & 0b1100_0000 == 0b1000_0000:
        lead += 1
    return lead


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


@dataclass
class StreamPiece:
    """A slice of output in arrival order.

    ``skipped`` counts earlier bytes (of either stream) that the hub no longer
    held, or is ``-1`` when it no longer knows how much earlier output there was.
    """

    stream: str
    data: bytes
    skipped: int = 0


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
        self.progress_step = 0
        self.transfer: dict[str, Any] | None = None
        self.sync_progress: dict[str, Any] | None = None
        self.stdout = OutputBuffer(output_limit)
        self.stderr = OutputBuffer(output_limit)
        #: Read cursor shared by run/get_job callers that pass no explicit offsets.
        self.cursor = {"stdout": 0, "stderr": 0}
        #: ``(stream, start, end)`` of each chunk in arrival order, for live streams.
        self.chunks: list[tuple[str, int, int]] = []
        self.chunks_base = 0
        self.cond = threading.Condition()
        self.client: Any = None
        self.request: ExecRequest | None = None

    @property
    def done(self) -> bool:
        return self.state in TERMINAL_STATES

    def buffer(self, stream: str) -> OutputBuffer:
        return self.stdout if stream == "stdout" else self.stderr

    def duration_ms(self) -> int | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return int((end - self.started_at) * 1000)

    # ---- views --------------------------------------------------------------

    def _summary_locked(self, *, include_created: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {"job_id": self.id, "target": self.target.id}
        if self.type == "exec":
            result["language"] = self.spec["language"]
        else:
            result["type"] = self.type
        result["state"] = self.state
        result["done"] = self.done
        if self.exit_code is not None:
            result["exit_code"] = self.exit_code
        if self.error:
            result["error"] = self.error
        duration = self.duration_ms()
        if duration is not None:
            result["duration_ms"] = duration
        if self.broadcast_id:
            result["broadcast_id"] = self.broadcast_id
        if include_created:
            result["created_at"] = utc_iso(self.created_at)
        return result

    def summary(self, *, include_created: bool = False) -> dict[str, Any]:
        with self.cond:
            return self._summary_locked(include_created=include_created)

    def _delta_locked(
        self,
        max_bytes: int,
        offsets: Mapping[str, int | None] | None,
        advance: bool,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for stream in STREAMS:
            buffer = self.buffer(stream)
            explicit = None if offsets is None else offsets.get(stream)
            start = self.cursor[stream] if explicit is None else explicit
            end = buffer.total
            begin, data = buffer.read(max(start, end - max(0, max_bytes)), end)
            lead = _skip_continuation_bytes(data)
            data, begin = data[lead:], begin + lead
            if not self.done:
                data = data[: _utf8_safe_length(data)]
            result[stream] = data.decode("utf-8", "replace")
            result[f"{stream}_offset"] = begin + len(data)
            if begin > start:
                result[f"{stream}_skipped_bytes"] = begin - start
            if advance and explicit is None:
                self.cursor[stream] = max(self.cursor[stream], begin + len(data))
        return result

    def view(
        self,
        *,
        max_output_bytes: int,
        offsets: Mapping[str, int | None] | None = None,
        advance: bool = True,
    ) -> dict[str, Any]:
        """Lean JSON view. Exec jobs carry the output since the read cursor (and advance it)."""
        with self.cond:
            result = self._summary_locked()
            if self.type == "exec":
                result.update(self._delta_locked(max_output_bytes, offsets, advance))
            else:
                result["local_path"] = self.spec["local_path"]
                result["remote_path"] = self.spec["remote_path"]
                if self.progress is not None:
                    result["progress"] = {"bytes_done": self.progress[0], "bytes_total": self.progress[1]}
                if self.type == "sync":
                    result["destination_type"] = self.spec["destination_type"]
                    result["progress"] = dict(self.sync_progress or {"phase": "queued"})
                if self.transfer is not None:
                    result["result"] = dict(self.transfer)
            return result

    def read_output(self, stream: str, offset: int, limit: int) -> dict[str, Any]:
        with self.cond:
            buffer = self.buffer(stream)
            begin, data = buffer.read(offset, limit)
            total = buffer.total
            done = self.done
        if begin + len(data) < total:
            data = data[: _utf8_safe_length(data)]
        end = begin + len(data)
        result: dict[str, Any] = {
            "job_id": self.id,
            "stream": stream,
            "offset": begin,
            "next_offset": end,
            "total_bytes": total,
            "data": data.decode("utf-8", "replace"),
            "eof": done and end >= total,
            "done": done,
        }
        if begin > offset:
            result["skipped_bytes"] = begin - offset
        return result

    def peek(self, stream: str, since: int, limit: int) -> tuple[int, bytes]:
        """Output after ``since`` without moving any cursor (the newest ``limit`` bytes)."""
        with self.cond:
            buffer = self.buffer(stream)
            end = buffer.total
            return buffer.read(max(since, end - limit), end)

    def next_pieces(self, index: int, timeout: float) -> tuple[list[StreamPiece], int, bool]:
        """Wait up to ``timeout`` for chunks after ``index``.

        Returns the new pieces in arrival order, the next index, and whether the
        job has finished and every chunk has been returned. Bytes the buffers no
        longer hold are skipped, and counted on the next piece that follows them.
        """
        with self.cond:
            if index - self.chunks_base >= len(self.chunks) and not self.done:
                self.cond.wait(timeout)
            pieces: list[StreamPiece] = []
            if index < self.chunks_base:
                pieces.append(StreamPiece("stdout", b"", skipped=-1))
                index = self.chunks_base
            lost = 0
            for stream, start, end in self.chunks[index - self.chunks_base :]:
                buffer = self.buffer(stream)
                begin = max(start, buffer.start)
                if begin >= end:
                    lost += end - start
                    continue
                lost += begin - start
                _, data = buffer.read(begin, end - begin)
                pieces.append(StreamPiece(stream, data, skipped=lost))
                lost = 0
            if lost:
                pieces.append(StreamPiece("stdout", b"", skipped=lost))
            index = self.chunks_base + len(self.chunks)
            return pieces, index, self.done


@dataclass
class Broadcast:
    """The same command started on several targets at once."""

    id: str
    created_at: float
    patterns: list[str]
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
        on_job: Callable[[HubJob], None] | None = None,
    ) -> None:
        self._registry = registry
        #: Called with every job the hub starts (the dashboard's history).
        self.on_job = on_job
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

    @property
    def audit(self) -> AuditLog:
        return self._audit

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

    def start_sync(
        self,
        target: Target,
        *,
        local_path: str,
        remote_path: str,
        destination_type: str,
        overwrite: bool,
        origin: str,
    ) -> HubJob:
        job = HubJob(
            job_type="sync",
            target=target,
            spec={
                "local_path": local_path,
                "remote_path": remote_path,
                "destination_type": destination_type,
                "overwrite": overwrite,
            },
            origin=origin,
            output_limit=0,
        )
        return self._launch(job, self._sync_body)

    def start_broadcast(
        self,
        runs: Sequence[tuple[Target, str]],
        *,
        patterns: Sequence[str],
        skipped: Sequence[dict[str, Any]],
        code: str,
        mode: str,
        timeout_s: float,
        origin: str,
    ) -> tuple[Broadcast, list[HubJob]]:
        """Start ``code`` on every ``(target, language)`` pair."""
        broadcast_id = "b" + uuid.uuid4().hex[:11]
        self._ensure_capacity(len(runs))
        self._audit.record(
            "broadcast.start",
            broadcast_id=broadcast_id,
            origin=origin,
            patterns=list(patterns),
            targets=[target.id for target, _ in runs],
            skipped=[item["target"] for item in skipped],
        )
        console.broadcast_started(
            broadcast_id,
            patterns=list(patterns),
            targets=[target for target, _ in runs],
            skipped=list(skipped),
            code=code,
            origin=origin,
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
            for target, language in runs
        ]
        broadcast = Broadcast(
            id=broadcast_id,
            created_at=time.time(),
            patterns=list(patterns),
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
    def wait(job: HubJob, wait_s: float, on_tick: Callable[[], None] | None = None) -> None:
        JobManager.wait_all([job], wait_s, on_tick)

    @staticmethod
    def wait_all(
        jobs: Sequence[HubJob],
        wait_s: float,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        """Wait until every job is done or ``wait_s`` passes.

        ``on_tick`` runs outside any lock whenever a job reports new output or
        state, and at least every :data:`TICK_S` seconds while waiting.
        """
        deadline = time.monotonic() + max(0.0, wait_s)
        for job in jobs:
            while True:
                remaining = deadline - time.monotonic()
                with job.cond:
                    if job.done or remaining <= 0:
                        break
                    job.cond.wait(min(remaining, TICK_S) if on_tick else remaining)
                if on_tick is not None:
                    on_tick()
        if on_tick is not None:
            on_tick()

    def cancel(self, job_id: str) -> HubJob:
        job = self.get(job_id)
        with job.cond:
            if job.done:
                return job
            job.cancel_requested = True
            console.cancel_requested(job)
            if job.state == QUEUED:
                self._finish_locked(job, CANCELLED, error="cancelled before it started")
                return job
            remote = job.remote_job_id
        if job.type in ("exec", "sync") and remote:
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
            if self.on_job is not None:
                self.on_job(job)
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
        return job

    def _host(self, target: Target) -> str | None:
        cached = self._registry.cached(target)
        return (cached.info or {}).get("hostname") if cached and cached.info else None

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
                console.job_started(job, self._host(job.target))
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
                job.progress_step = console.transfer_progress(job, done, total, job.progress_step)
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

    def _sync_body(self, job: HubJob) -> None:
        client = self._registry.client(job.target)

        def check_cancel() -> None:
            with job.cond:
                if job.cancel_requested:
                    raise _TransferCancelled()

        def report(update: dict[str, Any]) -> None:
            with job.cond:
                job.sync_progress = dict(update)
                job.transfer = dict(update)
                job.progress = update["bytes_done"], update["bytes_total"]
                job.progress_step = console.transfer_progress(job, *job.progress, job.progress_step)
                job.cond.notify_all()

        def run_remote(code: str) -> str:
            # Frozen Windows listeners may have no external Python installation.
            request = ExecRequest(
                kind="python", code=f"exec({code!r}, {{}})", mode="inprocess", timeout_s=600
            )
            with job.cond:
                job.client, job.request, job.remote_job_id = client, request, None
            check_cancel()
            try:
                result = client.send(request, on_submit=lambda remote_id: self._adopt_remote(job, remote_id))
            finally:
                with job.cond:
                    job.remote_job_id = None
            check_cancel()
            if result.timed_out or result.error or result.exit_code != 0:
                raise TransferError(
                    "sync_prepare_failed",
                    f"remote sync preparation failed: {result.error or result.stderr or result.exit_code}",
                )
            return result.stdout

        sync(client, **job.spec, check_cancel=check_cancel, report=report, run_remote=run_remote)
        with job.cond:
            if job.cancel_requested:
                self._finish_locked(job, CANCELLED, error="cancelled")
            else:
                job.exit_code = 0
                self._finish_locked(job, SUCCEEDED)

    def _append_output(self, job: HubJob, stream: str, data: bytes) -> None:
        if not data:
            return
        with job.cond:
            buffer = job.buffer(stream)
            start = buffer.total
            buffer.append(data)
            job.chunks.append((stream, start, buffer.total))
            if len(job.chunks) > MAX_TRACKED_CHUNKS:
                drop = len(job.chunks) - MAX_TRACKED_CHUNKS
                del job.chunks[:drop]
                job.chunks_base += drop
            job.cond.notify_all()
        console.echo(job, stream, data)

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
        """Settle ``job`` (caller holds ``job.cond``).

        The audit record and the console response box are written before waiters
        wake, so the console shows the result before the caller's response line.
        """
        if job.done:
            return
        job.state = state
        job.error = error
        job.finished_at = time.time()
        self._audit.record("job.finish", **self._audit_fields(job, include_spec=False))
        console.job_finished(job)
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
                if job.type == "sync":
                    fields["destination_type"] = job.spec["destination_type"]
        else:
            fields.update(
                state=job.state,
                exit_code=job.exit_code,
                error=job.error,
                duration_ms=job.duration_ms(),
            )
            if job.type == "exec":
                fields.update(stdout_bytes=job.stdout.total, stderr_bytes=job.stderr.total)
            elif job.type == "sync" and job.transfer is not None:
                fields.update(
                    (name, job.transfer[name])
                    for name in ("files_copied", "files_skipped", "files_done", "bytes_copied")
                )
            elif job.transfer is not None:
                fields.update(size=job.transfer.get("size"), sha256=job.transfer.get("sha256"))
        return fields
