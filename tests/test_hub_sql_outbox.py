import base64
import json
import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from privy.hub.sql_store import DurableOutbox, SqlBackend, StorageError


def test_outbox_replays_ordered_records_after_restart(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    outbox = DurableOutbox(path, max_bytes=4096)
    first = outbox.append("job", "full-id", {"state": "running"}, "owner")
    second = outbox.append("job", "full-id", {"state": "succeeded"}, "owner")
    outbox.close()
    restored = DurableOutbox(path, max_bytes=4096)
    rows = restored.pending()
    assert [row[0] for row in rows] == [first, second]
    restored.ack(first)
    assert restored.pending()[0][0] == second
    restored.close()


def test_outbox_full_rejects_without_dropping_pending_data(tmp_path):
    outbox = DurableOutbox(tmp_path / "outbox.sqlite3", max_bytes=512)
    outbox.append("output", "one", {"data": "x" * 200}, "owner")
    with pytest.raises(StorageError, match="full"):
        outbox.append("output", "two", {"data": "x" * 400}, "owner")
    assert len(outbox.pending()) == 1
    assert outbox.status()["rejected_writes"] == 1
    outbox.close()
    restored = DurableOutbox(tmp_path / "outbox.sqlite3", max_bytes=512)
    assert restored.status()["rejected_writes"] == 1
    restored.close()


def test_acknowledgment_does_not_reset_sequence(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    outbox = DurableOutbox(path, max_bytes=4096)
    first = outbox.append("audit", "one", {}, "owner")
    outbox.ack(first)
    outbox.close()
    outbox = DurableOutbox(path, max_bytes=4096)
    assert outbox.append("audit", "two", {}, "owner") > first
    outbox.close()


def test_delivery_worker_does_not_sleep_between_backlogged_batches():
    backend = object.__new__(SqlBackend)
    backend._stop = threading.Event()
    backend._last_error = None
    backend._wake = SimpleNamespace(
        wait=lambda timeout: pytest.fail("delivery worker slept with a pending backlog"),
        clear=lambda: None,
    )
    drained = []

    def drain():
        drained.append(500)
        if len(drained) == 2:
            backend._stop.set()

    backend._drain = drain
    backend.heartbeat = lambda: None
    backend.collect = lambda: {}
    backend.outbox = SimpleNamespace(pending=lambda limit: [(1,)])
    backend._work()
    assert drained == [500, 500]


class _FakeSqlCursor:
    def __init__(self, database):
        self.database = database
        self.rows = []

    def execute(self, query, *params):
        self.database.executed_queries.append(query)
        if "FROM dbo.privy_coordination" in query and "kind='hub' AND id=?" in query:
            self.rows = [(1,)] if params[0] in self.database.active_owners else []
        elif query.lstrip().startswith("DECLARE @rows"):
            self.database.flush_batch(json.loads(params[0]))
            self.rows = []
        else:
            raise AssertionError(f"unexpected fake SQL query: {query[:80]}")

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _FakeSqlDatabase:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.executescript(
            "CREATE TABLE records(kind TEXT,id TEXT,payload TEXT,owner TEXT,revision INTEGER,at REAL,"
            "target TEXT,lease_id TEXT,client_id TEXT,PRIMARY KEY(kind,id));"
            "CREATE TABLE output(job_id TEXT,chunk_index INTEGER,stream TEXT,start_offset INTEGER,"
            "end_offset INTEGER,payload TEXT,PRIMARY KEY(job_id,chunk_index));"
            "CREATE TABLE coordination(kind TEXT,id TEXT,payload TEXT,PRIMARY KEY(kind,id));"
        )
        self.connection.commit()
        self.lock = threading.RLock()
        self.active_owners = set()
        self.requested_locks = []
        self.executed_queries = []

    @contextmanager
    def transaction(self, lock=None):
        self.requested_locks.append(lock)
        with self.lock:
            self.connection.execute("BEGIN")
            try:
                yield _FakeSqlCursor(self)
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def flush_batch(self, rows):
        latest = {}
        for row in rows:
            key = row["kind"], row["id"]
            if key not in latest or row["revision"] > latest[key]["revision"]:
                latest[key] = row
        for (kind, key), row in latest.items():
            existing = self.connection.execute(
                "SELECT owner FROM records WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
            if kind == "job" and existing and existing[0] != row["owner"]:
                raise StorageError("51001 Privy job ownership conflict")
        for (kind, key), row in latest.items():
            payload = row["payload"]
            if kind == "output":
                self.connection.execute(
                    "INSERT OR IGNORE INTO output VALUES(?,?,?,?,?,?)",
                    (
                        payload["job_id"],
                        payload["index"],
                        payload["stream"],
                        payload["start"],
                        payload["end"],
                        json.dumps(payload),
                    ),
                )
                continue
            existing = self.connection.execute(
                "SELECT owner,revision,at FROM records WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
            should_update = (
                not existing
                or (existing[0] == row["owner"] and existing[1] < row["revision"])
                or (kind != "job" and existing[0] != row["owner"] and existing[2] < row["at"])
            )
            if should_update:
                self.connection.execute(
                    "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        kind,
                        key,
                        json.dumps(payload),
                        row["owner"],
                        row["revision"],
                        row["at"],
                        row.get("target"),
                        row.get("lease_id"),
                        row.get("client_id"),
                    ),
                )

    def record(self, kind, key):
        row = self.connection.execute(
            "SELECT payload,owner,revision FROM records WHERE kind=? AND id=?", (kind, key)
        ).fetchone()
        return None if row is None else (json.loads(row[0]), row[1], row[2])

    def close(self):
        self.connection.close()


def _fake_backend(database, owner, outbox):
    backend = object.__new__(SqlBackend)
    backend.owner = owner
    backend._fenced = False
    backend._closed = False
    backend._stop = threading.Event()
    backend._wake = threading.Event()
    backend._flush_lock = threading.Lock()
    backend._last_error = None
    backend._migration_status = None
    backend.hub_id = "fake-hub"
    backend.transaction = database.transaction
    backend.outbox = outbox
    return backend


def test_previous_owner_outbox_replays_idempotently_without_relaunch_or_unpin(tmp_path):
    database = _FakeSqlDatabase(tmp_path / "fake-sql.sqlite3")
    database.active_owners.add("owner-b")
    job_id, lease_id = "job-full-id", "lease-full-id"
    pin = {
        "lease_id": lease_id,
        "running_jobs": [job_id],
        "job_owners": {job_id: "owner-a"},
    }
    database.connection.execute("INSERT INTO coordination VALUES('lease',?,?)", (lease_id, json.dumps(pin)))
    database.connection.commit()

    journal_path = tmp_path / "home" / "sql-outbox.sqlite3"
    journal_path.parent.mkdir()
    old_outbox = DurableOutbox(journal_path, max_bytes=4096)
    old_outbox.append(
        "job",
        job_id,
        {"job_id": job_id, "lease_id": lease_id, "owner": "owner-a", "state": "queued"},
        "owner-a",
    )
    finished_id = "finished-job"
    old_outbox.append(
        "job", finished_id, {"job_id": finished_id, "owner": "owner-a", "state": "succeeded"}, "owner-a"
    )
    old_outbox.append(
        "output",
        f"{finished_id}:0",
        {
            "job_id": finished_id,
            "index": 0,
            "stream": "stdout",
            "start": 0,
            "end": 5,
            "data": base64.b64encode(b"hello").decode(),
        },
        "owner-a",
    )
    old_outbox.append("audit", "finish-1", {"job_id": finished_id, "event": "job.finish"}, "owner-a")
    old_outbox.close()

    successor_outbox = DurableOutbox(journal_path, max_bytes=4096)
    backend = _fake_backend(database, "owner-b", successor_outbox)

    def lose_ack(_sequences):
        raise StorageError("simulated lost outbox acknowledgment")

    successor_outbox.ack_batch = lose_ack
    with pytest.raises(StorageError, match="lost outbox acknowledgment"):
        backend._drain()
    assert database.requested_locks == [None]
    assert any("WITH (HOLDLOCK)" in query for query in database.executed_queries)
    assert successor_outbox.status()["pending_writes"] == 4
    assert database.record("job", job_id)[0]["state"] == "queued"
    assert database.record("job", finished_id)[0]["state"] == "succeeded"
    assert database.connection.execute("SELECT COUNT(*) FROM output").fetchone()[0] == 1

    successor_outbox.close()
    replayed_outbox = DurableOutbox(journal_path, max_bytes=4096)
    backend.outbox = replayed_outbox
    backend._drain()
    assert replayed_outbox.status()["pending_writes"] == 0
    assert database.record("job", job_id)[0]["state"] == "queued"
    assert database.record("job", finished_id)[0]["state"] == "succeeded"
    assert database.connection.execute("SELECT COUNT(*) FROM output").fetchone()[0] == 1
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM records WHERE kind='audit' AND id='finish-1'"
        ).fetchone()[0]
        == 1
    )
    assert (
        json.loads(
            database.connection.execute(
                "SELECT payload FROM coordination WHERE kind='lease' AND id=?", (lease_id,)
            ).fetchone()[0]
        )
        == pin
    )

    replayed_outbox.close()
    database.close()


def test_fenced_old_owner_and_51001_batch_keep_pending_rows(tmp_path):
    database = _FakeSqlDatabase(tmp_path / "fake-sql.sqlite3")
    database.active_owners.add("owner-b")
    old_outbox = DurableOutbox(tmp_path / "old-home.sqlite3", max_bytes=4096)
    old_outbox.append("audit", "old-write", {"event": "job.finish"}, "owner-a")
    fenced = _fake_backend(database, "owner-a", old_outbox)
    with pytest.raises(StorageError, match="fenced"):
        fenced._drain()
    assert database.requested_locks == [None]
    assert any("WITH (HOLDLOCK)" in query for query in database.executed_queries)
    assert old_outbox.status()["pending_writes"] == 1
    assert database.record("audit", "old-write") is None

    poison_outbox = DurableOutbox(tmp_path / "successor-home.sqlite3", max_bytes=4096)
    poison_outbox.append("job", "job-conflict", {"job_id": "job-conflict", "state": "failed"}, "owner-a")
    poison_outbox.append("audit", "same-batch", {"event": "job.finish"}, "owner-a")
    database.connection.execute(
        "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "job",
            "job-conflict",
            json.dumps({"job_id": "job-conflict", "state": "running"}),
            "owner-c",
            1,
            1,
            None,
            None,
            None,
        ),
    )
    database.connection.commit()
    successor = _fake_backend(database, "owner-b", poison_outbox)
    with pytest.raises(StorageError, match="51001"):
        successor._drain()
    assert successor.flush(timeout=0.1) is False
    assert successor.status()["error"] == "SQL job ownership conflict (51001); journal retained for replay"
    assert poison_outbox.status()["pending_writes"] == 2
    assert database.record("audit", "same-batch") is None

    old_outbox.close()
    poison_outbox.close()
    database.close()
