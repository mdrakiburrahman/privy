from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from privy.hub.settings import SqlSettings
from privy.hub.sql_store import AzureSqlConnections, SqlLeaseLock, StorageError


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
