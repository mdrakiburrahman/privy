"""Bounded, per-hub read projections; SQL mutations remain authoritative."""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from typing import Any

from privy.hub.sql_store import StorageError

log = logging.getLogger("privy.hub")


@dataclass
class _Read:
    load: Any
    key: Any
    value: Any = None
    ready: bool = False
    updated: float = 0
    future: Future | None = None
    error: Exception | None = None
    size: int = 0
    revision: int = 0


class BackgroundReads:
    """Coalesce bounded background reads and fail explicitly under backpressure."""

    def __init__(
        self,
        *,
        workers=8,
        capacity=64,
        queue_capacity=None,
        interval=1.0,
        max_age=5.0,
        clock=time.monotonic,
    ):
        queue_capacity = capacity if queue_capacity is None else queue_capacity
        if workers < 1:
            raise ValueError("read worker count must be positive")
        if capacity < 0 or queue_capacity < 0:
            raise ValueError("read cache and queue capacities must be non-negative")
        self.clock = clock
        self.interval, self.max_age, self.capacity = interval, max_age, capacity
        self._lock = threading.RLock()
        self._reads: OrderedDict[Any, _Read] = OrderedDict()
        self._inflight: dict[Any, _Read] = {}
        self._admission = threading.BoundedSemaphore(workers + queue_capacity)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="privy-read")
        self._stop = threading.Event()
        self._closing = False
        self._closed = False
        self._close_lock = threading.Lock()
        self._thread = threading.Thread(target=self._refresh, name="privy-read-refresh", daemon=True)
        self._thread.start()

    def _trim(self):
        while len(self._reads) > self.capacity:
            self._reads.popitem(last=False)

    def _lookup(self, key, load):
        read = self._reads.get(key)
        if read is None:
            read = self._inflight.get(key)
            if read is None or (read.future is not None and read.future.cancelled()):
                read = _Read(load, key)
            self._reads[key] = read
            self._trim()
        else:
            self._reads.move_to_end(key)
        read.load = load
        return read

    def _finished(self, key, read, future):
        self._admission.release()
        with self._lock:
            if self._inflight.get(key) is read and read.future is future:
                del self._inflight[key]

    def _submit(self, read, *, required=True):
        if read.future is not None and not read.future.done():
            return read.future
        if self._closed or self._closing:
            if required:
                raise StorageError("SQL read model is closing; retry after restart")
            return None
        if not self._admission.acquire(blocking=False):
            if required:
                raise StorageError("SQL read model is saturated; retry shortly")
            return None
        try:
            future = self._pool.submit(self._load, read)
        except BaseException:
            self._admission.release()
            raise
        read.future = future
        self._inflight[read.key] = read
        future.add_done_callback(lambda completed: self._finished(read.key, read, completed))
        return future

    def _load(self, read):
        with self._lock:
            revision, load = read.revision, read.load
        try:
            value = load()
        except Exception as exc:
            with self._lock:
                read.error = exc
            log.exception("Background SQL read refresh failed")
            raise
        with self._lock:
            if revision != read.revision:
                return read.value
            read.value, read.ready, read.updated, read.error = value, True, self.clock(), None
            if read.key[0] in ("output", "output-range"):
                rows = value["rows"] if isinstance(value, dict) else value
                read.size = sum(len(row.get("data", "")) + 1024 for row in rows)
            while sum(item.size for item in self._reads.values()) > 64 * 1024 * 1024:
                self._reads.popitem(last=False)
        return value

    def publish(self, key, value, load):
        with self._lock:
            read = self._reads.get(key) or self._inflight.get(key)
            if read is None:
                read = _Read(load, key)
            if key not in self._reads:
                self._reads[key] = read
                self._trim()
            read.revision += 1
            read.value, read.ready, read.updated, read.error = copy.deepcopy(value), True, self.clock(), None

    def get(self, key, load, *, wait=True, detached=True, refresh=False):
        with self._lock:
            read = self._lookup(key, load)
            fresh = not refresh and read.ready and self.clock() - read.updated < self.max_age
            if fresh:
                return copy.deepcopy(read.value) if detached else read.value
            future = self._submit(read)
        if not wait:
            return None
        try:
            future.result(timeout=4.0)
        except TimeoutError as exc:
            raise StorageError("SQL read model is still hydrating; retry shortly") from exc
        except Exception as exc:
            raise StorageError("SQL read model refresh failed") from exc
        with self._lock:
            return copy.deepcopy(read.value) if detached else read.value

    def invalidate(self, predicate):
        with self._lock:
            for key, read in self._reads.items():
                if predicate(key):
                    self._submit(read, required=False)

    def discard(self, predicate):
        with self._lock:
            for key in list(self._reads):
                if predicate(key):
                    del self._reads[key]
            for key, read in list(self._inflight.items()):
                if predicate(key) and read.future is not None:
                    read.future.cancel()

    def _refresh(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                for read in self._reads.values():
                    if self.clock() - read.updated >= self.interval:
                        self._submit(read, required=False)

    def close(self):
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                self._closing = True
                self._stop.set()
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise StorageError("SQL read refresh worker did not stop")
            self._pool.shutdown(wait=True, cancel_futures=True)
            with self._lock:
                self._closed = True


class SqlReadModel:
    def __init__(self, backend, *, start=True):
        self.backend = backend
        self.owner = backend.owner
        self.reads = BackgroundReads()
        self._lock = threading.RLock()
        self._records = {}
        self._pending = {}
        self._pending_revision = {}
        self._version = bytes(8)
        self._ready = threading.Event()
        self._updated = 0.0
        self._error = None
        self._stop = threading.Event()
        self._thread = None
        self._uses = {}
        self._use_errors = {}
        self._closed = False
        self._accounting_thread = threading.Thread(
            target=self._account, name="privy-read-accounting", daemon=True
        )
        self._accounting_thread.start()
        if start:
            self._thread = threading.Thread(target=self._refresh, name="privy-record-hydration", daemon=True)
            self._thread.start()

    def apply(self, kind, key, payload):
        if kind in ("probe", "call", "output"):
            # Aggregate/report snapshots are invalidated, never retain raw probe history.
            self.reads.invalidate(lambda key: key[0] in ("report", "history", "latest-probes"))
            if kind == "output":
                self.reads.invalidate(lambda key: key == ("output", payload["job_id"]))
            return
        with self._lock:
            identity = kind, key
            self._pending[identity] = copy.deepcopy(payload)
            self._pending_revision[identity] = self._pending_revision.get(identity, 0) + 1
        self.reads.invalidate(lambda key: key[0] in ("report", "history"))

    def refresh_once(self):
        pending_keys = self.backend.outbox.pending_keys()
        with self._lock:
            acknowledged = [
                (identity, self._pending_revision.get(identity, 0))
                for identity in self._pending
                if identity not in pending_keys
            ][:500]
        while True:
            if self._stop.is_set():
                return
            version, rows, complete = self.backend.read_changes(self._version)
            with self._lock:
                for kind, key, payload in rows:
                    self._records[kind, key] = payload
                self._version = version
            if complete:
                break
        # Key reconciliation observes old-version deletes, while bounded authoritative
        # reads close the gap between outbox acknowledgement and the commit-safe feed.
        keys = self.backend.read_record_keys()
        authoritative = {}
        by_kind = {}
        for (kind, key), _ in acknowledged:
            by_kind.setdefault(kind, []).append(key)
        for kind, selected in by_kind.items():
            rows = self.backend.get_many(kind, selected)
            authoritative.update({(kind, key): value for key, value in rows.items()})
        with self._lock:
            for identity in list(self._records):
                if identity not in keys and identity not in self._pending:
                    del self._records[identity]
            for identity, revision in acknowledged:
                if identity in authoritative:
                    self._records[identity] = authoritative[identity]
                else:
                    self._records.pop(identity, None)
                if self._pending_revision.get(identity) == revision:
                    self._pending.pop(identity, None)
                    self._pending_revision.pop(identity, None)
            self._updated = time.monotonic()
            self._error = None
            self._ready.set()
            live_jobs = {key for kind, key in self._records.keys() | self._pending.keys() if kind == "job"}
        self.reads.discard(
            lambda key: (
                key[0] in ("record", "records", "list", "scan", "counts")
                or (key[0] in ("output", "output-range") and key[1] not in live_jobs)
            )
        )

    def _refresh(self):
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception as exc:
                self._error = exc
                log.exception("SQL record hydration failed")
            self._stop.wait(1)

    def _ensure_ready(self):
        if not self._ready.wait(4):
            raise StorageError("SQL records are still hydrating; retry shortly")
        with self._lock:
            if time.monotonic() - self._updated >= 5:
                raise StorageError("SQL record read model is stale") from self._error

    def _snapshot(self):
        self._ensure_ready()
        with self._lock:
            records = self._records | self._pending
            return list(records.items())

    def get(self, kind, key):
        with self._lock:
            if (kind, key) in self._pending:
                return copy.deepcopy(self._pending[kind, key])
        if kind in ("probe", "call"):
            return self.backend.get(kind, key)
        if not self._ready.is_set():
            return self.reads.get(("record", kind, key), lambda: self.backend.get(kind, key))
        self._ensure_ready()
        with self._lock:
            return copy.deepcopy(self._records.get((kind, key)))

    def get_many(self, kind, keys):
        keys = tuple(dict.fromkeys(keys))
        if not self._ready.is_set():
            rows = self.reads.get(("records", kind, keys), lambda: self.backend.get_many(kind, keys))
            with self._lock:
                rows.update(
                    {
                        key: copy.deepcopy(self._pending[kind, key])
                        for key in keys
                        if (kind, key) in self._pending
                    }
                )
            return rows
        self._ensure_ready()
        with self._lock:
            return {
                key: copy.deepcopy(self._pending.get((kind, key), self._records.get((kind, key))))
                for key in keys
                if (kind, key) in self._pending or (kind, key) in self._records
            }

    def list(
        self,
        kind,
        *,
        limit=200,
        before=None,
        after=None,
        target=None,
        lease_id=None,
        client_id=None,
        states=None,
    ):
        if not 1 <= limit <= 100_000:
            raise StorageError("SQL page size must be between 1 and 100000")
        if kind == "probe" and limit == 1:
            rows = self.reads.get(("latest-probes",), self.backend.read_latest_probes)
            return [rows[target]] if target in rows else []
        if kind in ("probe", "call"):
            return self.backend.list(
                kind,
                limit=limit,
                before=before,
                after=after,
                target=target,
                lease_id=lease_id,
                client_id=client_id,
            )
        if not self._ready.is_set():
            filters = dict(
                limit=limit,
                before=before,
                after=after,
                target=target,
                lease_id=lease_id,
                client_id=client_id,
                states=states,
            )
            key = (
                "list",
                kind,
                limit,
                before,
                after,
                target,
                lease_id,
                client_id,
                tuple(sorted(states)) if states is not None else None,
            )
            return self.reads.get(key, lambda: self.backend.list(kind, **filters))
        rows = self._select(
            kind,
            before=before,
            after=after,
            target=target,
            lease_id=lease_id,
            client_id=client_id,
            states=states,
        )
        return copy.deepcopy(rows[:limit])

    def _select(
        self, kind, *, before=None, after=None, target=None, lease_id=None, client_id=None, states=None
    ):
        rows = [
            (key, row)
            for (row_kind, key), row in self._snapshot()
            if row_kind == kind
            and all(
                value is None or str(row.get(name, "")).casefold() == str(value).casefold()
                for name, value in (("target", target), ("lease_id", lease_id), ("client_id", client_id))
            )
            and (before is None or row.get("created_at", row.get("at", 0)) < before)
            and (after is None or row.get("created_at", row.get("at", 0)) >= after)
            and (states is None or row.get("state") in states)
        ]
        rows.sort(key=lambda item: (item[1].get("created_at", item[1].get("at", 0)), item[0]), reverse=True)
        return [row for _, row in rows]

    def scan(self, kind, **filters):
        if not self._ready.is_set():
            rows = self.reads.get(
                ("scan", kind, tuple(sorted(filters.items()))),
                lambda: list(self.backend.scan(kind, **filters)),
            )
            yield from rows
            return
        # No paging cap: purpose history and reports may span all retained jobs.
        yield from copy.deepcopy(self._select(kind, **filters))

    def count_jobs(self):
        if not self._ready.is_set():
            return self.reads.get(("counts",), self.backend.count_jobs)
        rows = [row for (kind, _), row in self._snapshot() if kind == "job"]
        return {
            "running": sum(r["state"] == "running" for r in rows),
            "queued": sum(r["state"] == "queued" for r in rows),
            "retained": len(rows),
        }

    def count_jobs_by_target(self, *, lease_id=None):
        if not self._ready.is_set():
            return self.reads.get(
                ("counts", "target", lease_id), lambda: self.backend.count_jobs_by_target(lease_id=lease_id)
            )
        counts = {}
        for (kind, _), row in self._snapshot():
            if kind != "job" or (lease_id is not None and row.get("lease_id") != lease_id):
                continue
            if row.get("target"):
                counts[row["target"]] = counts.get(row["target"], 0) + 1
        return counts

    def count_jobs_by_lease(self):
        if not self._ready.is_set():
            return self.reads.get(("counts", "lease"), self.backend.count_jobs_by_lease)
        counts = {}
        for (kind, _), row in self._snapshot():
            if kind != "job":
                continue
            if row.get("lease_id"):
                counts[row["lease_id"]] = counts.get(row["lease_id"], 0) + 1
        return counts

    def count_job_records(self, **filters):
        if not self._ready.is_set():
            key = (
                "counts",
                "records",
                tuple(
                    sorted(
                        (name, tuple(sorted(value)) if name == "states" and value is not None else value)
                        for name, value in filters.items()
                    )
                ),
            )
            return self.reads.get(key, lambda: self.backend.count_job_records(**filters))
        return len(self._select("job", **filters))

    def report_history(self, *, after=None, before=None):
        # Called only by the background report builder. Keep the SQL aggregates:
        # do not hydrate hundreds of thousands of raw probe JSON documents.
        return self.backend.report_history(after=after, before=before)

    def _output_snapshot(self, job_id, required=None):
        key = ("output", job_id)

        def load():
            deadline = time.monotonic() + 3
            while True:
                snapshot = self.backend.read_output_snapshot(job_id)
                if required is None or all(
                    snapshot["sizes"][stream] >= end for stream, end in required.items()
                ):
                    return snapshot
                if time.monotonic() >= deadline:
                    raise StorageError("SQL output is still catching up to job metadata")
                time.sleep(0.05)

        deadline = time.monotonic() + 4
        snapshot = self.reads.get(key, load, detached=False)
        # Metadata and output refresh independently. Never report an older
        # output snapshot as a genuine missing chunk when metadata advanced.
        while required is not None and any(
            snapshot["sizes"][stream] < end for stream, end in required.items()
        ):
            if time.monotonic() >= deadline:
                raise StorageError("SQL output is still catching up to job metadata")
            snapshot = self.reads.get(key, load, detached=False, refresh=True)
        return snapshot

    def output(self, job_id, stream, offset, limit):
        if limit <= 0:
            return []
        rows = self._output_snapshot(job_id, {stream: offset + limit})["rows"]
        # Large jobs keep only a bounded tail. Fetch earlier explicit ranges on
        # demand rather than falsely treating a truncated snapshot as complete.
        stream_rows = [row for row in rows if row["stream"] == stream]
        if (
            rows
            and rows[0]["index"] > 0
            and (not stream_rows or offset < min(row["start"] for row in stream_rows))
        ):
            return self.reads.get(
                ("output-range", job_id, stream, offset, limit),
                lambda: self.backend.output(job_id, stream, offset, limit),
            )
        return copy.deepcopy(
            [row for row in stream_rows if row["end"] > offset and row["start"] < offset + limit]
        )

    def pieces(self, job_id, index, limit=200):
        with self._lock:
            count = self._records.get(("job", job_id), {}).get("chunk_count", 0)
        return self.read_pieces(job_id, index, limit=limit, chunk_count=count)

    def read_pieces(self, job_id, index, *, limit=200, chunk_count=0):
        if limit <= 0:
            return []
        rows = self._output_snapshot(job_id, {"chunks": chunk_count})["rows"]
        if rows and index < rows[0]["index"]:
            return self.reads.get(
                ("output-range", job_id, index, limit), lambda: self.backend.pieces(job_id, index, limit)
            )
        return copy.deepcopy(
            sorted((row for row in rows if row["index"] >= index), key=lambda row: row["index"])[:limit]
        )

    def read_view_output(self, job_id, ranges):
        cursor_streams = [stream for stream, (offset, _, _) in ranges.items() if offset is None]
        # Cursor offsets participate in cross-hub consumption; never trust a
        # cached cursor when reserving output. Explicit offsets need no SQL.
        cursors = self.backend.read_cursors(job_id, cursor_streams) if cursor_streams else {}
        offsets, chunks = {}, {}
        for stream, (offset, total, limit) in ranges.items():
            offsets[stream] = cursors[stream] if offset is None else offset
            begin = min(max(0, offsets[stream]), total)
            chunks[stream] = self.output(job_id, stream, begin, max(0, min(limit, total - begin)))
        return offsets, chunks

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def defer_use(self, store, target, lease_id, origin):
        self._defer(("use", target, lease_id, origin), lambda: store.use(target, lease_id, origin=origin))

    def _defer(self, key, action):
        with self._lock:
            error = self._use_errors.pop(key, None)
            if error is not None:
                raise StorageError("Deferred read-path lease accounting failed") from error
            self._uses[key] = action

    def _account(self):
        while not self._stop.wait(0.25):
            with self._lock:
                pending, self._uses = self._uses, {}
            for key, use in pending.items():
                try:
                    use()
                except Exception as exc:
                    with self._lock:
                        self._use_errors[key] = exc
                    log.exception("Deferred read-path lease accounting failed")

    @property
    def closed(self):
        return self._closed

    def close(self):
        if self._closed:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=35)
            if self._thread.is_alive():
                raise StorageError("SQL record hydration did not stop; backend resources remain in use")
        self._accounting_thread.join(timeout=35)
        if self._accounting_thread.is_alive():
            raise StorageError("Deferred read accounting did not stop; backend resources remain in use")

        failures = []
        with self._lock:
            pending, self._uses = self._uses, {}
            failures.extend(self._use_errors.values())
            self._use_errors.clear()
        for use in pending.values():
            try:
                use()
            except Exception as exc:
                failures.append(exc)
                log.exception("Deferred read-path lease accounting failed during shutdown")
        try:
            self.reads.close()
        except Exception as exc:
            if not self.reads._closed:
                raise StorageError("SQL read workers did not stop; backend resources remain in use") from exc
            failures.append(exc)
        self._closed = True
        if failures:
            raise StorageError(
                f"Deferred read-path accounting or read shutdown failed ({len(failures)} error(s))"
            ) from failures[0]
