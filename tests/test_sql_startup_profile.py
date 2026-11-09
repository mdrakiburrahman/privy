import logging
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from privy.hub.app import HubApp, _Warmup, build_hub
from privy.hub.metrics import RANGES
from privy.hub.read_model import SqlReadModel
from privy.hub.settings import SqlSettings
from privy.hub.sql_profile import StartupProfile
from privy.hub.sql_store import AzureSqlConnections, SqlBackend, StorageError


@pytest.fixture(autouse=True)
def capture_hub_logs(caplog):
    caplog.set_level(logging.INFO, logger="privy.hub")


def test_profile_disabled_does_not_read_clock_or_log(monkeypatch, caplog):
    monkeypatch.delenv("PRIVY_SQL_PROFILE", raising=False)

    def forbidden_clock():
        raise AssertionError("disabled instrumentation must not read the clock")

    monkeypatch.setattr("privy.hub.sql_profile.time.perf_counter", forbidden_clock)
    with caplog.at_level(logging.INFO), StartupProfile("test") as profile:
        with profile.stage("read"):
            profile.count("rows", 20)
    assert not caplog.records


def test_profile_accounts_repeated_stages_and_counts(monkeypatch, caplog):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    clock = iter((10.0, 11.0, 11.25, 12.0, 12.75, 14.0))
    monkeypatch.setattr("privy.hub.sql_profile.time.perf_counter", lambda: next(clock))
    with caplog.at_level(logging.INFO), StartupProfile("hydration") as profile:
        with profile.stage("page"):
            profile.count("pages")
            profile.count("rows", 2000)
        with profile.stage("page"):
            profile.count("pages")
            profile.count("rows", 7)
    assert profile.counts == {"pages": 2, "rows": 2007}
    assert profile.timings == {"page": [2, 1000.0, 750.0]}
    assert "mono_s=10.000000" in caplog.text
    assert "duration_ms=4000.000" in caplog.text
    assert "outcome=ok" in caplog.text
    assert "rows=2007" in caplog.text
    assert "page_max_ms=750.000" in caplog.text


def test_profile_failure_is_redacted_and_propagates(monkeypatch, caplog):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    error = RuntimeError("secret-token; Server=private; user code and row payload")
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError) as caught:
        with StartupProfile("hydration") as profile, profile.stage("decode"):
            raise error
    assert caught.value is error
    assert "outcome=error" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert str(error) not in caplog.text


@pytest.mark.parametrize("value", ["true", "0", ""])
def test_profile_requires_exact_opt_in(monkeypatch, caplog, value):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", value)
    with caplog.at_level(logging.INFO), StartupProfile("test"):
        pass
    assert not caplog.records


def test_cold_hydration_profiles_all_pages_before_ready(monkeypatch, caplog):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    records = [(("job", "one"), {"state": "done"}), (("audit", "two"), {"event": "finish"})]
    calls = []
    backend = SimpleNamespace(
        owner="test-owner",
        outbox=SimpleNamespace(pending_keys=lambda: set()),
        read_record_keys=lambda: {identity for identity, _ in records},
    )
    model = SqlReadModel(backend, start=False)

    def read_changes(version):
        assert not model._ready.is_set()
        index = len(calls)
        calls.append(version)
        (kind, key), payload = records[index]
        return bytes([index + 1]), [(kind, key, payload)], index == 1

    backend.read_changes = read_changes
    try:
        with caplog.at_level(logging.INFO):
            model.refresh_once()
        assert model._ready.is_set()
        assert model._records == dict(records)
        assert model._version == b"\x02"
        assert "phase=hydration" in caplog.text
        assert "pages=2" in caplog.text
        assert "rows=2" in caplog.text
        assert "stage=keys" in caplog.text
        assert "stage=apply" in caplog.text
        assert "stage=publish_ready" in caplog.text
        assert "ready=1" in caplog.text
        caplog.clear()
        backend.read_changes = lambda version: (version, [], True)
        with caplog.at_level(logging.INFO):
            model.refresh_once()
        assert "phase=hydration" not in caplog.text
    finally:
        model.close()


def test_cold_hydration_failure_preserves_unready_and_watermark(monkeypatch, caplog):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    failure = RuntimeError("private database payload")

    def read_changes(version):
        raise failure

    backend = SimpleNamespace(
        owner="test-owner",
        outbox=SimpleNamespace(pending_keys=lambda: set()),
        read_changes=read_changes,
    )
    model = SqlReadModel(backend, start=False)
    try:
        with caplog.at_level(logging.INFO), pytest.raises(RuntimeError) as caught:
            model.refresh_once()
        assert caught.value is failure
        assert not model._ready.is_set()
        assert model._version == bytes(8)
        assert "phase=hydration" in caplog.text
        assert "outcome=error" in caplog.text
        assert str(failure) not in caplog.text
    finally:
        model.close()


def test_feed_profiles_fetch_decode_without_logging_payload_or_sql(monkeypatch, caplog):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    statements = []
    payload = '{"data":"private payload"}'
    cursor = SimpleNamespace(
        execute=lambda query, *args: statements.append(query),
        fetchone=lambda: (b"\x00" * 7 + b"\x03",),
        fetchall=lambda: [(b"\x00" * 7 + b"\x02", "job", "private-id", payload)],
    )

    @contextmanager
    def transaction():
        yield cursor

    backend = SimpleNamespace(transaction=transaction)
    with caplog.at_level(logging.INFO):
        version, rows, complete = SqlBackend.read_changes(backend, bytes(8))
    assert version == b"\x00" * 7 + b"\x02"
    assert rows == [("job", "private-id", {"data": "private payload"})]
    assert complete
    for name in ("watermark", "execute", "fetch", "decode"):
        assert f"stage={name}" in caplog.text
    assert "rows=1" in caplog.text
    assert "private" not in caplog.text
    assert "SELECT" not in caplog.text
    assert len(statements) == 2


def test_health_separates_sql_and_python_status_and_preserves_response(monkeypatch, caplog):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    calls = []
    cursor = SimpleNamespace(execute=lambda query: calls.append(query), fetchone=lambda: (1,))

    @contextmanager
    def transaction():
        yield cursor

    backend = SimpleNamespace(
        transaction=transaction,
        status=lambda: {"pending_writes": 0, "mode": "azure_sql"},
        read_model=SimpleNamespace(health=lambda: {"ready": False}),
    )
    backend.health = lambda: SqlBackend.health(backend)
    app = HubApp(SimpleNamespace(jobs=SimpleNamespace(backend=backend), version="test"), port=18748)
    app.warmup = SimpleNamespace(status=lambda: {"warm": False})
    with caplog.at_level(logging.INFO):
        response = app.handle("GET", "/healthz", {"Host": "127.0.0.1:18748"}, b"")
    assert response.status == 200
    assert response.payload["storage"]["read_model"] == {"ready": False}
    assert response.payload["storage"]["warmup"] == {"warm": False}
    assert calls == ["SELECT 1"]
    for phase, stage in (("health", "sql"), ("health", "status"), ("health_sql", "query")):
        assert f"phase={phase} stage={stage}" in caplog.text
    assert "SELECT" not in caplog.text


def test_warmup_profiles_hydration_wait_separately_and_keeps_gate(monkeypatch, caplog):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    ready = threading.Event()
    calls = []
    registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(targets=[]),
        probe_many=lambda targets: calls.append("probe"),
        cached=lambda target: None,
        probe_ttl_s=30,
    )
    service = SimpleNamespace(
        registry=registry,
        listener_facts_many=lambda *args: None,
        leases=SimpleNamespace(
            target_summaries=lambda targets: None,
            groups=lambda: None,
            list_leases=lambda **kwargs: None,
            list_clients=lambda **kwargs: None,
        ),
        metrics=SimpleNamespace(
            count_jobs_by_lease=lambda: None,
            count_jobs_by_target=lambda: None,
            entries=lambda **kwargs: None,
            report=lambda *args, **kwargs: calls.append(kwargs["selection"].range_name),
        ),
        jobs=SimpleNamespace(list=lambda **kwargs: None, max_running=3),
    )
    model = SimpleNamespace(
        _ready=ready,
        reads=SimpleNamespace(core_error=lambda: None),
        health=lambda: {"last_error": None},
    )
    monkeypatch.setattr("privy.hub.dashboard.build_shell_report", lambda *args: None)
    warmup = _Warmup(SimpleNamespace(service=service), model)
    with caplog.at_level(logging.INFO):
        try:
            warmup.start()
            assert not warmup.status()["warm"]
            assert not calls
            ready.set()
            deadline = time.monotonic() + 3
            while not warmup.status()["warm"] and time.monotonic() < deadline:
                time.sleep(0.01)
            assert warmup.status()["warm"]
            assert calls == ["probe", *RANGES, "all"]
            for stage in ("hydration_wait", "probes", "facts", "leases", "lists", "shell"):
                assert f"stage={stage}" in caplog.text
            for name in (*RANGES, "all"):
                assert f"stage=report_{name}" in caplog.text
        finally:
            warmup.close()


def test_connection_profiles_auth_connect_and_reuse_without_credentials(monkeypatch, caplog):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    native = SimpleNamespace(autocommit=True, rollback=lambda: None, close=lambda: None)
    token = SimpleNamespace(token="private-token", expires_on=time.time() + 10000)
    credential = SimpleNamespace(get_token=lambda scope: token)
    monkeypatch.setitem(
        sys.modules,
        "pyodbc",
        SimpleNamespace(drivers=lambda: ["ODBC Driver 18 for SQL Server"], connect=lambda *a, **k: native),
    )
    monkeypatch.setitem(
        sys.modules, "azure.identity", SimpleNamespace(AzureCliCredential=lambda **kwargs: credential)
    )
    pool = AzureSqlConnections(SqlSettings("private.database.windows.net", "private_database"))
    try:
        with caplog.at_level(logging.INFO):
            pool().close()
            pool().close()
        for stage in ("pool_wait", "checkout", "auth", "connect"):
            assert f"stage={stage}" in caplog.text
        assert "reused=1" in caplog.text
        assert "private" not in caplog.text
        assert "Server=" not in caplog.text
    finally:
        pool.close()


def test_construction_profiles_config_and_bind_without_binding_or_sql(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    snapshot = SimpleNamespace(targets=[])
    registry = SimpleNamespace(snapshot=lambda: snapshot)
    jobs = SimpleNamespace(audit=None, max_running=3, close=lambda **kwargs: True)
    backend = SimpleNamespace(close=lambda: None)
    events = []
    monkeypatch.setattr("privy.hub.app.ensure_home", lambda home: None)
    monkeypatch.setattr("privy.hub.app.RelaysStore", lambda path: SimpleNamespace(snapshot=lambda: snapshot))
    monkeypatch.setattr("privy.hub.app.TargetRegistry", lambda *args, **kwargs: registry)
    monkeypatch.setattr("privy.hub.settings.load_settings", lambda *args: object())
    monkeypatch.setattr("privy.hub.sql_store.SqlBackend", lambda *args, **kwargs: backend)
    monkeypatch.setattr("privy.hub.shared_jobs.SharedJobManager", lambda *args, **kwargs: jobs)
    monkeypatch.setattr("privy.hub.app.LeaseStore", lambda *args, **kwargs: object())
    monkeypatch.setattr("privy.hub.app.ListenerInfoStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "privy.hub.app.HubService",
        lambda **kwargs: SimpleNamespace(**kwargs, started_at=0),
    )
    monkeypatch.setattr("privy.hub.shared_metrics.SharedHubMetrics", lambda *args, **kwargs: object())

    def server(address, app):
        events.append("bind")
        return SimpleNamespace(server_address=address, server_close=lambda: None)

    monkeypatch.setattr("privy.hub.app.HubHTTPServer", server)
    monkeypatch.setattr("privy.hub.app.write_state", lambda *args: events.append("state"))
    with caplog.at_level(logging.INFO):
        runtime = build_hub(home=tmp_path, port=18748, version="test")
    assert runtime.storage is backend
    assert runtime.service.jobs is jobs
    assert events == ["bind", "state"]
    for stage in ("local_config", "backend", "lease_config", "bind", "state_file"):
        assert f"phase=construction stage={stage}" in caplog.text
    assert str(tmp_path) not in caplog.text


def test_health_storage_failure_remains_503_and_python_status_errors_propagate(monkeypatch, caplog):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")

    def unavailable():
        raise StorageError("private connection error")

    backend = SimpleNamespace(health=unavailable)
    app = HubApp(SimpleNamespace(jobs=SimpleNamespace(backend=backend), version="test"), port=18748)
    with caplog.at_level(logging.INFO):
        response = app.handle("GET", "/healthz", {"Host": "127.0.0.1:18748"}, b"")
    assert response.status == 503
    assert "private" not in caplog.text
    backend.health = lambda: {}
    backend.read_model = SimpleNamespace(health=unavailable)
    with pytest.raises(StorageError, match="private"):
        app.handle("GET", "/healthz", {"Host": "127.0.0.1:18748"}, b"")


def test_backend_profiles_initialization_order_and_preserves_cleanup(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    events = []
    monkeypatch.setattr(
        "privy.hub.sql_store._HomeLock",
        lambda path: SimpleNamespace(close=lambda: events.append("home_close")),
    )
    monkeypatch.setattr("privy.hub.sql_store._home_identity", lambda home: "test-hub")
    monkeypatch.setattr(SqlBackend, "bootstrap", lambda self: events.append("schema"))
    outbox = SimpleNamespace(close=lambda: events.append("journal_close"))
    monkeypatch.setattr("privy.hub.sql_store.DurableOutbox", lambda *args, **kwargs: outbox)

    def register(self):
        events.append("incarnation")
        raise StorageError("private registration payload")

    monkeypatch.setattr(SqlBackend, "_register_incarnation", register)
    connection = SimpleNamespace(close=lambda: events.append("pool_close"))
    with caplog.at_level(logging.INFO), pytest.raises(StorageError, match="private"):
        SqlBackend(
            SqlSettings("sqltest.database.windows.net", "isolated_test"),
            tmp_path,
            connect=connection,
            start_worker=False,
        )
    assert events == ["schema", "incarnation", "journal_close", "pool_close", "home_close"]
    for stage in ("schema", "journal", "incarnation"):
        assert f"phase=backend stage={stage}" in caplog.text
    assert "outcome=error" in caplog.text
    assert "private" not in caplog.text


def test_stopped_cold_hydration_does_not_publish_partial_history(monkeypatch, caplog):
    monkeypatch.setenv("PRIVY_SQL_PROFILE", "1")
    backend = SimpleNamespace(owner="test", outbox=SimpleNamespace(pending_keys=lambda: set()))
    model = SqlReadModel(backend, start=False)

    def first_page(version):
        model._stop.set()
        return b"\x01", [("job", "one", {"state": "done"})], False

    backend.read_changes = first_page
    try:
        with caplog.at_level(logging.INFO):
            model.refresh_once()
        assert not model._ready.is_set()
        assert model._records == {}
        assert model._version == bytes(8)
        assert "stopped=1" in caplog.text
        assert "ready=1" not in caplog.text
    finally:
        model.close()
