import json
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


def test_cursor_reads_are_batched_read_only_but_consumption_stays_atomic():
    class Cursor:
        def __init__(self):
            self.queries = []
            self.rows = []

        def execute(self, query, *params):
            self.queries.append((query, params))
            if query.startswith("SELECT stream,offset"):
                self.rows = [("stdout", 7)]
            elif query.startswith("SELECT offset"):
                self.rows = [(7,)]
            else:
                self.rows = []

        def fetchall(self):
            rows, self.rows = self.rows, []
            return rows

        def fetchone(self):
            rows, self.rows = self.rows, []
            return rows[0] if rows else None

    cursor = Cursor()

    @contextmanager
    def transaction():
        yield cursor

    backend = SimpleNamespace(transaction=transaction)
    assert SqlBackend.read_cursors(backend, "job-1", ("stdout", "stderr")) == {"stdout": 7, "stderr": 0}
    select = cursor.queries[-1][0]
    assert "stream IN (?,?)" in select and "UPDLOCK" not in select

    cursor.queries.clear()
    assert SqlBackend.advance_cursor(backend, "job-1", "stdout", 0) == 7
    assert len(cursor.queries) == 1 and "UPDLOCK" not in cursor.queries[0][0]
    assert not any(query.startswith("UPDATE") for query, _ in cursor.queries)

    cursor.queries.clear()
    assert SqlBackend.advance_cursor(backend, "job-1", "stdout", 12) == 7
    assert "UPDLOCK,HOLDLOCK" in cursor.queries[0][0]
    assert any(query.startswith("UPDATE") for query, _ in cursor.queries)


def test_collect_skips_lease_lock_when_no_retention_candidates():
    class Cursor:
        rowcount = 0

        def __init__(self):
            self.queries = []

        def execute(self, query, *params):
            self.queries.append((query, params))

        def fetchone(self):
            return (0,)

    cursor = Cursor()
    transactions = []

    @contextmanager
    def transaction(resource):
        transactions.append(resource)
        yield cursor

    backend = SimpleNamespace(transaction=transaction)
    assert SqlBackend.collect(backend) == {"output": 0, "jobs": 0, "history": 0, "leases": 0}
    assert transactions == ["privy.gc"]
    assert len(cursor.queries) == 1
    query = cursor.queries[0][0]
    assert "kind='job' AND at<@cutoff" in query
    assert "kind<>'job' AND at<@cutoff" in query
    assert "kind IN ('lease','hub')" in query and "DATEADD(day,-7,@now)" in query


def test_collect_keeps_lease_lock_and_running_job_pin_guard_when_candidates_exist():
    class Cursor:
        rowcount = 0

        def __init__(self):
            self.queries = []
            self.results = [(1,), (0,)]

        def execute(self, query, *params):
            self.queries.append((query, params))

        def fetchone(self):
            return self.results.pop(0)

    cursor = Cursor()
    transactions = []

    @contextmanager
    def transaction(resource):
        transactions.append(resource)
        yield cursor

    backend = SimpleNamespace(transaction=transaction)
    assert SqlBackend.collect(backend) == {"output": 0, "jobs": 0, "history": 0, "leases": 0}
    assert transactions == ["privy.gc"]
    queries = [query for query, _ in cursor.queries]
    assert any("@Resource='privy.leases'" in query for query in queries)
    assert any("OPENJSON(c.payload,'$.running_jobs')" in query for query in queries)


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


def test_lease_lock_loads_snapshot_with_one_batched_read():
    target = SimpleNamespace(id="dev/box_windows", namespace="dev", path="/tmp/box")
    target_config = {"targets": [{"id": target.id, "namespace": target.namespace, "path": target.path}]}
    client = {"client_id": "client-1"}
    lease = {"lease_id": "lease-1", "group": "dev/box", "state": "active"}
    use = {"at": 1.0, "by": "client-1"}

    class Cursor:
        def __init__(self):
            self.result_sets = [
                [(1,)],
                [(1_800_000_000.25,)],
                [(json.dumps(target_config),)],
                [
                    ("client", "client-1", json.dumps(client)),
                    ("lease", "lease-1", json.dumps(lease)),
                    ("use", "dev/box", json.dumps(use)),
                ],
            ]
            self.result_set_index = 0
            self.queries = []

        def execute(self, query, *params):
            self.queries.append((query, params))

        def fetchone(self):
            rows = self.result_sets[self.result_set_index]
            return rows[0] if rows else None

        def fetchall(self):
            return self.result_sets[self.result_set_index]

        def nextset(self):
            self.result_set_index += 1
            return self.result_set_index < len(self.result_sets)

    cursor = Cursor()

    @contextmanager
    def transaction(resource):
        assert resource == "privy.leases"
        yield cursor

    store = SimpleNamespace(
        registry=SimpleNamespace(snapshot=lambda: SimpleNamespace(targets=[target])),
        _clients={},
        _leases={},
        _uses={},
    )
    backend = SimpleNamespace(
        transaction=transaction,
        assert_owner_active=lambda _cursor: None,
        _assert_owner_current=lambda: None,
        owner="hub-1",
    )
    lock = SqlLeaseLock(store, backend)

    with lock:
        assert lock.now() == 1_800_000_000.25

    assert len(cursor.queries) == 1
    query, params = cursor.queries[0]
    assert params == ("hub-1",)
    assert "kind='hub'" in query
    assert query.count(";") == 3
    assert store._clients == {"client-1": client}
    assert store._leases == {"lease-1": lease}
    assert store._uses == {"dev/box": use}


def test_lease_read_snapshot_is_serializable_without_application_lock():
    target = SimpleNamespace(id="dev/box_windows", namespace="dev", path="/tmp/box")
    target_config = {"targets": [{"id": target.id, "namespace": target.namespace, "path": target.path}]}

    class Cursor:
        def __init__(self):
            self.result_sets = [[(1,)], [(1_800_000_000.25,)], [(json.dumps(target_config),)], []]
            self.result_set_index = 0
            self.queries = []

        def execute(self, query, *params):
            self.queries.append((query, params))

        def fetchone(self):
            rows = self.result_sets[self.result_set_index]
            return rows[0] if rows else None

        def fetchall(self):
            return self.result_sets[self.result_set_index]

        def nextset(self):
            self.result_set_index += 1
            return self.result_set_index < len(self.result_sets)

    cursor = Cursor()
    transactions = []

    @contextmanager
    def transaction(lock=None, *, isolation_level=None):
        transactions.append((lock, isolation_level))
        yield cursor

    backend = SimpleNamespace(
        transaction=transaction,
        assert_owner_active=lambda _cursor: None,
        _assert_owner_current=lambda: None,
        owner="hub-1",
    )
    store = SimpleNamespace(
        registry=SimpleNamespace(snapshot=lambda: SimpleNamespace(targets=[target])),
        _clients={},
        _leases={},
        _uses={},
    )
    lock = SqlLeaseLock(store, backend)

    with lock.read_snapshot() as snapshot:
        assert snapshot["now"] == 1_800_000_000.25

    assert transactions == [(None, "SERIALIZABLE")]
    assert len(cursor.queries) == 1
    assert "sp_getapplock" not in cursor.queries[0][0]
    assert store._clients == {} and store._leases == {} and store._uses == {}


def test_lease_lock_rejects_snapshot_for_fenced_owner():
    class Cursor:
        def __init__(self):
            self.queries = []

        def execute(self, query, *params):
            self.queries.append((query, params))

        def fetchone(self):
            return (0,)

    cursor = Cursor()

    @contextmanager
    def transaction(_resource):
        yield cursor

    def assert_active(_cursor):
        raise StorageError("this Azure SQL hub incarnation was fenced; restart the hub")

    backend = SimpleNamespace(
        transaction=transaction,
        assert_owner_active=assert_active,
        _assert_owner_current=lambda: None,
        owner="fenced-hub",
    )
    store = SimpleNamespace(_clients={}, _leases={}, _uses={})
    lock = SqlLeaseLock(store, backend)

    with pytest.raises(StorageError, match="was fenced"):
        with lock:
            pass

    assert len(cursor.queries) == 1
    assert cursor.queries[0][1] == ("fenced-hub",)


def test_transaction_closes_cursor_before_returning_connection():
    events = []
    queries = []

    class Cursor:
        def execute(self, query, *_args):
            events.append("execute")
            queries.append(query)

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
    assert (
        queries[-1]
        == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED; SET NOCOUNT ON; SET XACT_ABORT ON; SET LOCK_TIMEOUT 15000;"
    )

    events.clear()
    queries.clear()
    with backend.transaction(isolation_level="SERIALIZABLE"):
        pass
    assert events == ["execute", "cursor.close", "commit", "connection.close"]
    assert queries[0].startswith("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;")

    events.clear()
    with pytest.raises(ValueError, match="test failure"):
        with backend.transaction():
            raise ValueError("test failure")
    assert events == ["execute", "cursor.close", "rollback", "connection.close"]


def test_pooled_transaction_resets_isolation_after_commit_and_rollback(connections):
    pool, _, created, _ = connections
    pooled = pool()
    connection = pooled._connection
    state = {"isolation": "READ COMMITTED", "queries": [], "outcome": None}

    class Cursor:
        def execute(self, query, *_args):
            state["queries"].append(query)
            if query.startswith("SET TRANSACTION ISOLATION LEVEL "):
                state["isolation"] = query.split("LEVEL ", 1)[1].split(";", 1)[0]

        def close(self):
            pass

    connection.cursor = lambda: Cursor()
    connection.commit = lambda: state.update(outcome="commit")
    connection.rollback = lambda: state.update(outcome="rollback")
    pooled.close()

    backend = object.__new__(SqlBackend)
    backend._connect = pool

    with backend.transaction(isolation_level="SERIALIZABLE"):
        assert state["isolation"] == "SERIALIZABLE"
    assert state["isolation"] == "SERIALIZABLE"

    with pytest.raises(RuntimeError, match="rollback test"):
        with backend.transaction(isolation_level="SERIALIZABLE"):
            assert state["isolation"] == "SERIALIZABLE"
            raise RuntimeError("rollback test")
    assert state["outcome"] == "rollback"
    assert state["isolation"] == "SERIALIZABLE"

    with backend.transaction():
        assert state["isolation"] == "READ COMMITTED"

    assert len(created) == 1
    assert [query.split(";", 1)[0] for query in state["queries"]] == [
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
    ]


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
        [
            [(json.dumps(job),)],
            [(json.dumps(purpose),)],
            [(json.dumps(previous),)],
            [("box", 4, 3, 3.0, 1.0, 3.0, json.dumps(probe), "online", 4)],
            [("box", 3.0, 1)],
            [("box", 4.0)],
            [(4.0, 4.0)],
            [("ui", 1, 0, 3.0)],
            [("queued", 1), ("running", 2)],
        ]
    ]

    class Cursor:
        def __init__(self):
            self.executed = []
            self.exec_index = -1
            self.result_index = 0
            self.nextset_calls = []

        def execute(self, query, *params):
            self.executed.append((query, params))
            self.exec_index += 1
            self.result_index = 0
            return self

        def fetchall(self):
            return results[self.exec_index][self.result_index]

        def fetchone(self):
            rows = results[self.exec_index][self.result_index]
            return rows[0] if rows else None

        def nextset(self):
            self.nextset_calls.append(self.exec_index)
            self.result_index += 1
            return self.result_index < len(results[self.exec_index])

    cursor = Cursor()
    transactions = []

    @contextmanager
    def transaction():
        transactions.append(True)
        yield cursor

    backend.transaction = transaction
    history = backend.report_history(after=2, before=4)

    assert transactions == [True]
    assert len(cursor.executed) == 1
    queries = cursor.executed[0][0].split(";\n")
    assert len(queries) == 9
    assert "kind=?" in queries[0]
    assert "kind=?" in queries[1]
    assert "kind IN('job','purpose')" in queries[2]
    probe_query = queries[3]
    assert "GROUP BY target" in probe_query
    assert "CROSS APPLY" in probe_query
    assert "COUNT_BIG(*) OVER" not in probe_query
    assert "kind='probe'" in probe_query
    assert "latest.payload" in probe_query
    assert "OPENJSON(?)" not in probe_query
    assert "LAG(probe_status)" in queries[4]
    target_p50_query = queries[5]
    assert (
        "ROW_NUMBER() OVER(PARTITION BY p.target ORDER BY p.probe_latency_ms,p.at,p.id)" in target_p50_query
    )
    assert "GROUP BY target" in target_p50_query
    assert "COUNT_BIG(*) OVER" not in target_p50_query
    assert "THEN 1 ELSE 0 END + 1" in target_p50_query
    assert "ORDER BY probe_latency_ms,target,at,id" in queries[6]
    assert "GROUP BY JSON_VALUE(payload,'$.surface')" in queries[7]
    assert "GROUP BY JSON_VALUE(payload,'$.state')" in queries[8]
    assert cursor.executed[0][1] == (
        "job",
        2,
        4,
        "purpose",
        2,
        4,
        2,
        2,
        4,
        2,
        4,
        2,
        4,
        2,
        4,
        2,
        4,
        2,
        4,
        2,
        4,
        2,
        4,
        "call",
        2,
        4,
    )
    assert cursor.nextset_calls == [0] * 9
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
