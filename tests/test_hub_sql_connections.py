from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from privy.hub.settings import SqlSettings
from privy.hub.sql_store import AzureSqlConnections, SqlBackend, SqlLeaseLock, StorageError


@pytest.fixture
def connections(monkeypatch):
    pyodbc = pytest.importorskip("pyodbc")
    pytest.importorskip("azure.identity")
    now = [1_800_000_000]
    monkeypatch.setattr("privy.hub.sql_store.time.time", lambda: now[0])
    monkeypatch.setattr(pyodbc, "drivers", lambda: ["ODBC Driver 18 for SQL Server"])
    created, tokens = [], []

    def create(*args, **kwargs):
        assert "Encrypt=yes;TrustServerCertificate=no" in args[0]
        assert "Password" not in args[0]
        assert kwargs["attrs_before"][1256][4:].decode("utf-16-le") == "test-token"
        connection = SimpleNamespace(closed=False, cursor=lambda: None)
        connection.close = lambda: setattr(connection, "closed", True)
        created.append(connection)
        return connection

    def token(scope):
        tokens.append(scope)
        return SimpleNamespace(token="test-token", expires_on=now[0] + 1000)

    monkeypatch.setattr(pyodbc, "connect", create)
    pool = AzureSqlConnections(SqlSettings("sqltest.database.windows.net", "isolated_test"))
    pool._credential = SimpleNamespace(get_token=token)
    yield pool, now, created, tokens
    pool.close()


def test_cli_token_and_connections_are_reused(connections):
    pool, _, created, tokens = connections
    first = pool()
    first.close()
    second = pool()
    second.close()
    assert len(created) == 1
    assert tokens == ["https://database.windows.net/.default"]


def test_near_expiry_refreshes_cli_token_and_connection(connections):
    pool, now, created, tokens = connections
    pool().close()
    now[0] += 701
    pool().close()
    assert len(tokens) == 2 and len(created) == 2
    assert created[0].closed


def test_invalidated_connection_is_never_reused(connections):
    pool, _, created, tokens = connections
    connection = pool()
    connection.invalidate()
    connection.close()
    pool().close()
    assert len(created) == 2 and created[0].closed
    assert len(tokens) == 1


def test_failed_lease_entry_and_rollback_does_not_strand_process_lock():
    @contextmanager
    def failed_transaction(resource):
        def fail(*args):
            raise StorageError("query unavailable")

        try:
            yield SimpleNamespace(execute=fail)
        finally:
            raise StorageError("rollback unavailable")

    lock = SqlLeaseLock(SimpleNamespace(), SimpleNamespace(transaction=failed_transaction))
    with pytest.raises(StorageError, match="rollback unavailable"):
        with lock:
            pass
    assert lock._local.depth == 0

    def another_thread():
        acquired = lock._lock.acquire(timeout=1)
        if acquired:
            lock._lock.release()
        return acquired

    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(another_thread).result(timeout=2)


def test_transaction_closes_cursor_before_returning_connection():
    events = []

    class Cursor:
        def execute(self, *_args):
            events.append("execute")

        def close(self):
            events.append("cursor.close")

    class Connection:
        def cursor(self):
            return cursor

        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("connection.close")

    cursor = Cursor()
    connection = Connection()
    backend = object.__new__(SqlBackend)
    backend._connect = lambda: connection

    with backend.transaction():
        pass
    assert events == ["execute", "cursor.close", "commit", "connection.close"]

    events.clear()
    with pytest.raises(ValueError, match="test failure"):
        with backend.transaction():
            raise ValueError("test failure")
    assert events == ["execute", "cursor.close", "rollback", "connection.close"]


def test_cursor_close_failure_rolls_back_and_discards_connection():
    events = []

    class Cursor:
        def execute(self, *_args):
            events.append("execute")

        def close(self):
            events.append("cursor.close")
            raise RuntimeError("cursor still has pending results")

    class Connection:
        invalidated = False

        def cursor(self):
            return Cursor()

        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

        def invalidate(self):
            self.invalidated = True

        def close(self):
            events.append("connection.close")

    connection = Connection()
    backend = object.__new__(SqlBackend)
    backend._connect = lambda: connection

    with pytest.raises(StorageError, match="cursor cleanup failed"):
        with backend.transaction():
            pass

    assert connection.invalidated
    assert events == ["execute", "cursor.close", "rollback", "connection.close"]


def test_pooled_transactions_close_cursors_under_concurrent_reads_and_flushes():
    import threading

    pool_slot = threading.BoundedSemaphore(1)
    state_lock = threading.Lock()
    cursor_open = False

    class Cursor:
        def execute(self, *_args):
            return self

        def fetchone(self):
            return (0,)

        def close(self):
            nonlocal cursor_open
            with state_lock:
                assert cursor_open
                cursor_open = False

    class Connection:
        def cursor(self):
            nonlocal cursor_open
            with state_lock:
                if cursor_open:
                    raise RuntimeError("HY000: connection is busy with results")
                cursor_open = True
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pool_slot.release()

    def connect():
        assert pool_slot.acquire(timeout=2)
        return Connection()

    backend = object.__new__(SqlBackend)
    backend._connect = connect

    def run_transaction(index):
        with backend.transaction("privy.concurrent-test") as cursor:
            statement = "SELECT payload FROM dbo.privy_records" if index % 2 else "MERGE dbo.privy_records"
            cursor.execute(statement)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(run_transaction, range(64)))

    assert not cursor_open


def test_report_history_aggregates_probe_and_call_data_in_one_transaction():
    import json

    backend = object.__new__(SqlBackend)
    job = {"job_id": "j1", "target": "box", "created_at": 3, "state": "queued"}
    purpose = {"id": "p1", "target": "box", "created_at": 3, "purpose": "current"}
    previous = {"job_id": "old", "target": "box", "created_at": 1, "purpose": "earlier"}
    probe = {"target": "box", "at": 3, "status": "online", "latency_ms": 4, "info": {"os": "test"}}
    results = [
        [(json.dumps(job),)],
        [(json.dumps(purpose),)],
        [(json.dumps(previous),)],
        [("box", 4, 3, 3.0, 1.0, 3.0, "probe-1", "online", 4)],
        [("probe-1", json.dumps(probe))],
        [("box", 3.0, 1)],
        [("box", 4.0)],
        [(4.0, 4.0)],
        [("ui", 1, 0, 3.0)],
        [("queued", 1), ("running", 2)],
    ]

    class Cursor:
        def __init__(self):
            self.executed = []
            self.index = -1

        def execute(self, query, *params):
            self.executed.append((query, params))
            self.index += 1
            return self

        def fetchall(self):
            return results[self.index]

        def fetchone(self):
            rows = results[self.index]
            return rows[0] if rows else None

    cursor = Cursor()
    transactions = []

    @contextmanager
    def transaction():
        transactions.append(True)
        yield cursor

    backend.transaction = transaction
    history = backend.report_history(after=2, before=4)

    assert transactions == [True]
    assert len(cursor.executed) == 10
    assert "ROW_NUMBER() OVER(PARTITION BY target" in cursor.executed[3][0]
    assert "JOIN dbo.privy_records payloads" not in cursor.executed[3][0]
    assert "kind='probe'" in cursor.executed[3][0]
    assert cursor.executed[3][1] == (2, 4)
    assert "OPENJSON(?)" in cursor.executed[4][0]
    assert cursor.executed[4][1] == ('["probe-1"]',)
    assert "LAG(probe_status)" in cursor.executed[5][0]
    assert "ROW_NUMBER() OVER(PARTITION BY target ORDER BY probe_latency_ms,at,id)" in cursor.executed[6][0]
    assert "ORDER BY probe_latency_ms,target,at,id" in cursor.executed[7][0]
    assert cursor.executed[6][1] == (2, 4)
    assert cursor.executed[7][1] == (2, 4, 2, 4, 2, 4)
    assert "GROUP BY JSON_VALUE(payload,'$.surface')" in cursor.executed[8][0]
    assert history["jobs"] == [job]
    assert history["purposes"] == [purpose]
    assert history["latest_before"] == [previous]
    assert history["probes"] == [{**probe, "latency_ms": 4}]
    assert history["probe_stats"] == {
        "by_target": {
            "box": {
                "samples": 4,
                "online": 3,
                "latency_p50_ms": 4.0,
                "last_online": 3.0,
                "earliest_at": 1.0,
                "status_since": 3.0,
                "status_since_first_check": True,
            }
        },
        "samples": 4,
        "latency_p50_ms": 4.0,
        "latency_p95_ms": 4.0,
    }
    assert history["calls"] == []
    assert history["call_stats"] == {
        "total": 1,
        "by_surface": {"ui": 1},
        "errors": 0,
        "earliest_at": 3.0,
    }
    assert history["job_counts"] == {"queued": 1, "running": 2, "retained": 3}
