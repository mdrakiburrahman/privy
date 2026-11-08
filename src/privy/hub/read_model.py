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
    """Coalesce misses, refresh off-thread, and fail explicitly on stale/error reads."""

    def __init__(self, *, workers=8, capacity=64, interval=1.0, max_age=5.0, clock=time.monotonic):
        self.clock = clock
        self.interval, self.max_age, self.capacity = interval, max_age, capacity
        self._lock = threading.RLock()
        self._reads: OrderedDict[Any, _Read] = OrderedDict()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="privy-read")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._refresh, name="privy-read-refresh", daemon=True)
        self._thread.start()

    def _submit(self, read):
        if read.future is None or read.future.done():
            read.future = self._pool.submit(self._load, read)
        return read.future

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
                read.size = sum(len(row.get("data", "")) + 1024 for row in value)
            while sum(item.size for item in self._reads.values()) > 64 * 1024 * 1024:
                self._reads.popitem(last=False)
        return value

    def publish(self, key, value, load):
        with self._lock:
            read = self._reads.get(key)
            if read is None:
                read = _Read(load, key)
                self._reads[key] = read
                while len(self._reads) > self.capacity:
                    self._reads.popitem(last=False)
            read.revision += 1
            read.value, read.ready, read.updated, read.error = copy.deepcopy(value), True, self.clock(), None

    def get(self, key, load, *, wait=True, detached=True):
        with self._lock:
            read = self._reads.get(key)
            if read is None:
                read = _Read(load, key)
                self._reads[key] = read
                while len(self._reads) > self.capacity:
                    self._reads.popitem(last=False)
            else:
                read.load = load
                self._reads.move_to_end(key)
            fresh = read.ready and self.clock() - read.updated < self.max_age
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
                    self._submit(read)

    def discard(self, predicate):
        with self._lock:
            for key in list(self._reads):
                if predicate(key):
                    del self._reads[key]

    def _refresh(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                for read in self._reads.values():
                    if self.clock() - read.updated >= self.interval:
                        self._submit(read)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._pool.shutdown(wait=True, cancel_futures=True)


class SqlReadModel:
    def __init__(self, backend, *, start=True):
        self.backend = backend
        self.owner = backend.owner
        self.reads = BackgroundReads()
        self._lock = threading.RLock()
        self._records = {}
        self._pending = {}
        self._version = bytes(8)
        self._ready = threading.Event()
        self._updated = 0.0
        self._error = None
        self._stop = threading.Event()
        self._thread = None
        self._uses = {}
        self._use_errors = {}
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
            self._records[kind, key] = copy.deepcopy(payload)
            self._pending[kind, key] = copy.deepcopy(payload)
        self.reads.invalidate(lambda key: key[0] in ("report", "history"))

    def refresh_once(self):
        with self._lock:
            pending = self.backend.outbox.pending_keys()
            self._pending = {key: value for key, value in self._pending.items() if key in pending}
        while True:
            if self._stop.is_set():
                return
            version, rows, complete = self.backend.read_changes(self._version)
            with self._lock:
                for kind, key, payload in rows:
                    identity = kind, key
                    pending = self._pending.get(identity)
                    if pending is None or pending == payload:
                        self._records[identity] = payload
                        self._pending.pop(identity, None)
                self._version = version
            if complete:
                break
        # Key reconciliation also observes old-version deletes, without tombstones.
        keys = self.backend.read_record_keys()
        with self._lock:
            for identity in list(self._records):
                if identity not in keys and identity not in self._pending:
                    del self._records[identity]
            self._updated = time.monotonic()
            self._error = None
            self._ready.set()
        self.reads.discard(lambda key: key[0] in ("record", "records", "list", "scan", "counts"))

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
            return list(self._records.items())

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
                key: copy.deepcopy(self._records[kind, key]) for key in keys if (kind, key) in self._records
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
        rows = [
            row
            for (row_kind, _), row in self._snapshot()
            if row_kind == kind
            and all(
                value is None or str(row.get(name, "")).casefold() == str(value).casefold()
                for name, value in (("target", target), ("lease_id", lease_id), ("client_id", client_id))
            )
            and (before is None or row.get("created_at", row.get("at", 0)) < before)
            and (after is None or row.get("created_at", row.get("at", 0)) >= after)
            and (states is None or row.get("state") in states)
        ]
        rows.sort(
            key=lambda row: (row.get("created_at", row.get("at", 0)), row.get("job_id", row.get("id", ""))),
            reverse=True,
        )
        return copy.deepcopy(rows[:limit])

    def scan(self, kind, **filters):
        if not self._ready.is_set():
            rows = self.reads.get(
                ("scan", kind, tuple(sorted(filters.items()))),
                lambda: list(self.backend.scan(kind, **filters)),
            )
            yield from rows
            return
        # No paging cap: purpose history and reports may span all retained jobs.
        self._snapshot()
        with self._lock:
            size = len(self._records)
        if size <= 100_000:
            yield from self.list(kind, limit=100_000, **filters)
            return
        rows = [row for (row_kind, _), row in self._snapshot() if row_kind == kind]
        for name in ("target", "lease_id", "client_id"):
            if filters.get(name) is not None:
                rows = [row for row in rows if str(row.get(name, "")).casefold() == filters[name].casefold()]
        if filters.get("before") is not None:
            rows = [row for row in rows if row.get("created_at", row.get("at", 0)) < filters["before"]]
        if filters.get("after") is not None:
            rows = [row for row in rows if row.get("created_at", row.get("at", 0)) >= filters["after"]]
        rows.sort(
            key=lambda row: (row.get("created_at", row.get("at", 0)), row.get("job_id", row.get("id", ""))),
            reverse=True,
        )
        yield from copy.deepcopy(rows)

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
        states = filters.pop("states", None)
        return sum(states is None or row.get("state") in states for row in self.scan("job", **filters))

    def report_history(self, *, after=None, before=None):
        # Called only by the background report builder. Keep the SQL aggregates:
        # do not hydrate hundreds of thousands of raw probe JSON documents.
        return self.backend.report_history(after=after, before=before)

    def output(self, job_id, stream, offset, limit):
        if limit <= 0:
            return []
        rows = self.reads.get(
            ("output", job_id), lambda: self.backend.read_output_snapshot(job_id), detached=False
        )
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
        rows = self.reads.get(("output", job_id), lambda: self.backend.read_output_snapshot(job_id))
        if rows and index < rows[0]["index"]:
            return self.reads.get(
                ("output-range", job_id, index, limit), lambda: self.backend.pieces(job_id, index, limit)
            )
        return sorted((row for row in rows if row["index"] >= index), key=lambda row: row["index"])[:limit]

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
        key = target, lease_id, origin
        with self._lock:
            error = self._use_errors.pop(key, None)
            if error is not None:
                raise StorageError("Deferred read-path lease accounting failed") from error
            self._uses[key] = lambda: store.use(target, lease_id, origin=origin)

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

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=35)
            if self._thread.is_alive():
                raise StorageError("SQL record hydration did not stop")
        self._accounting_thread.join(timeout=35)
        if self._accounting_thread.is_alive():
            raise StorageError("Deferred read accounting did not stop")
        with self._lock:
            pending, self._uses = self._uses, {}
        for use in pending.values():
            use()
        self.reads.close()
