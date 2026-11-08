import json
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, Thread

import pytest
import yaml

from privy.hub.leases import DEFAULT_CONFIG, effective_lease_config
from privy.hub.settings import SettingsError, load_settings
from privy.hub.sql_store import _SCHEMA, SqlBackend, StorageError, _home_identity, _HomeLock, _ProfileCursor


def test_absent_settings_preserve_file_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRIVY_HUB_SETTINGS", raising=False)
    assert load_settings(tmp_path) is None


def test_explicit_missing_settings_fail(tmp_path):
    with pytest.raises(SettingsError, match="cannot read"):
        load_settings(tmp_path, tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "version: 1\nstorage: {mode: sql}",
        "version: 1\nstorage: {mode: azure_sql, server: 'bad;Password=x', database: privy}",
        "version: 1\nstorage: {mode: azure_sql, server: a.database.windows.net, database: privy, retention_days: 0}",
        "version: 1\nstorage: {mode: azure_sql, server: a.database.windows.net, database: privy, typo: true}",
    ],
)
def test_present_invalid_settings_fail(tmp_path, text):
    path = tmp_path / "appsettings.yaml"
    path.write_text(text)
    with pytest.raises(SettingsError):
        load_settings(tmp_path, path)


def test_sql_settings_and_resolution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRIVY_HUB_SETTINGS", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "appsettings.yaml").write_text("version: 1\nstorage: {mode: file}")
    path = home / "appsettings.yaml"
    path.write_text(
        "version: 1\nstorage: {mode: azure_sql, server: sqltest.database.windows.net, database: isolated_test}"
    )
    settings = load_settings(home)
    assert settings.database == "isolated_test"
    assert settings.retention_days == 7
    assert settings.driver == "ODBC Driver 18 for SQL Server"
    assert load_settings(home, Path("appsettings.yaml")) is None


def test_sql_identity_file_is_created_atomically_and_ignores_orphan_temp_file(tmp_path):
    (tmp_path / ".sql-hub-id.orphan.tmp").write_text("partial", encoding="ascii")
    identity = _home_identity(tmp_path)
    assert identity == _home_identity(tmp_path)
    assert len(identity) == 32


def test_same_home_successor_fences_previous_incarnation_without_releasing_pins():
    previous_payload = json.dumps(
        {"owner": "previous", "hub_id": "home-id", "home_scope": "host:/home", "active": True}
    )
    cursor = FakeCursor([("previous", previous_payload, 0)])

    @contextmanager
    def transaction(_lock=None):
        yield cursor

    backend = object.__new__(SqlBackend)
    backend.owner = "current"
    backend.hub_id = "home-id"
    backend.home_scope = "host:/home"
    backend.transaction = transaction
    backend._register_incarnation()

    update = next(args for query, args in cursor.statements if query.startswith("UPDATE"))
    insert = next(args for query, args in cursor.statements if query.startswith("INSERT"))
    assert json.loads(update[0])["active"] is False
    assert json.loads(insert[1])["active"] is True
    assert json.loads(insert[1])["home_scope"] == "host:/home"


def test_sql_job_pages_apply_state_filters_and_aggregate_counts():
    class Cursor:
        def __init__(self):
            self.responses = [
                [(json.dumps({"id": "job-1"}),)],
                [("target-a", 3), ("target-b", 2)],
                [("lease-a", 4)],
                [(3,)],
            ]
            self.statements = []

        def execute(self, query, *args):
            self.statements.append((query, args))

        def fetchall(self):
            return self.responses.pop(0)

        def fetchone(self):
            return self.responses.pop(0)[0]

    cursor = Cursor()

    @contextmanager
    def transaction(_lock=None):
        yield cursor

    backend = object.__new__(SqlBackend)
    backend.transaction = transaction

    assert backend.list(
        "job", limit=2, target="target-a", lease_id="lease-a", states=frozenset({"failed", "error"})
    ) == [{"id": "job-1"}]
    assert backend.count_jobs_by_target(lease_id="lease-a") == {"target-a": 3, "target-b": 2}
    assert backend.count_jobs_by_lease() == {"lease-a": 4}
    assert (
        backend.count_job_records(
            lease_id="lease-a", states=frozenset({"failed", "error"}), after=10, before=20
        )
        == 3
    )

    page_query, page_args = cursor.statements[0]
    assert "JSON_VALUE(payload,'$.state') IN (?,?)" in page_query
    assert page_args == (2, "job", "target-a", "lease-a", "error", "failed")
    assert "GROUP BY target" in cursor.statements[1][0]
    assert cursor.statements[1][1] == ("lease-a",)
    assert "GROUP BY lease_id" in cursor.statements[2][0]
    assert "lease_id<>''" in cursor.statements[2][0]
    count_query, count_args = cursor.statements[3]
    assert "at>=?" in count_query and "at<?" in count_query
    assert "JSON_VALUE(payload,'$.state') IN (?,?)" in count_query
    assert count_args == ("lease-a", 10, 20, "error", "failed")


def test_sql_history_scan_uses_larger_bounded_keyset_pages():
    class Cursor:
        def __init__(self):
            self.statement = None

        def execute(self, query, *args):
            self.statement = (query, args)

        def fetchall(self):
            return [(100.0, "record-1", json.dumps({"created_at": 100.0}))]

    cursor = Cursor()

    @contextmanager
    def transaction(_lock=None):
        yield cursor

    backend = object.__new__(SqlBackend)
    backend.transaction = transaction

    assert list(backend.scan("probe", after=0)) == [{"created_at": 100.0}]
    query, args = cursor.statement
    assert "SELECT TOP (5000)" in query
    assert "ORDER BY at DESC,id DESC" in query
    assert args == ("probe", 0)


def test_sql_probe_history_projects_samples_and_loads_only_latest_payloads():
    class Cursor:
        def __init__(self):
            self.statements = []
            self.result = []

        def execute(self, query, *args):
            self.statements.append((query, args))
            if "probe_status,probe_latency_ms" in query:
                self.result = [
                    ("target-a", 1.0, "probe-a", "online", 10),
                    ("target-a", 2.0, "probe-b", "offline", None),
                    ("target-b", 3.0, "probe-c", "online", 50),
                ]
            else:
                self.result = [
                    ("probe-b", json.dumps({"info": {"hostname": "a"}, "error": "offline"})),
                    ("probe-c", json.dumps({"info": {"hostname": "b"}, "legacy": False})),
                ]

        def fetchall(self):
            return self.result

    cursor = Cursor()

    @contextmanager
    def transaction():
        yield cursor

    backend = object.__new__(SqlBackend)
    backend.transaction = transaction

    rows = backend.probe_history(after=0, before=4)

    assert [row["status"] for row in rows] == ["online", "offline", "online"]
    assert rows[0]["latency_ms"] == 10
    assert rows[1]["error"] == "offline"
    assert rows[2]["info"] == {"hostname": "b"}
    sample_query, sample_args = cursor.statements[0]
    assert "probe_status,probe_latency_ms" in sample_query
    assert "JSON_VALUE" not in sample_query
    assert "ORDER BY" not in sample_query
    assert "at>=?" in sample_query and "at<?" in sample_query
    assert sample_args == (0, 4)
    payload_query, payload_args = cursor.statements[1]
    assert "OPENJSON(?)" in payload_query
    assert payload_args == (json.dumps(["probe-b", "probe-c"]),)


def test_schema_bootstrap_adds_compatible_covering_probe_projection():
    assert "probe_status AS (CONVERT(nvarchar(32), JSON_VALUE(payload,'$.status')))" in _SCHEMA
    assert "probe_latency_ms AS (TRY_CONVERT(int, JSON_VALUE(payload,'$.latency_ms')))" in _SCHEMA
    assert "IF COL_LENGTH('dbo.privy_records','probe_status') IS NULL" in _SCHEMA
    assert "IX_privy_probe_history" in _SCHEMA
    assert "CREATE INDEX IX_privy_probe_history ON dbo.privy_records(at DESC)" in _SCHEMA
    assert "INCLUDE(target,probe_status,probe_latency_ms) WHERE kind='probe'" in _SCHEMA


def test_sql_profile_records_failed_statement_without_bound_parameters(caplog):
    class Cursor:
        def execute(self, query, *params):
            raise RuntimeError("database failure")

    with caplog.at_level("ERROR", logger="privy.hub"):
        with pytest.raises(RuntimeError, match="database failure"):
            _ProfileCursor(Cursor()).execute("SELECT payload WHERE id=?", "private-value")

    assert "SELECT payload WHERE id=?" in caplog.text
    assert "database failure" in caplog.text
    assert "private-value" not in caplog.text


def test_active_incarnation_from_a_different_home_scope_blocks_startup():
    previous_payload = json.dumps(
        {"owner": "previous", "hub_id": "copied-home-id", "home_scope": "other-host:/home", "active": True}
    )
    cursor = FakeCursor([("previous", previous_payload, 0)])

    @contextmanager
    def transaction(_lock=None):
        yield cursor

    backend = object.__new__(SqlBackend)
    backend.owner = "current"
    backend.hub_id = "copied-home-id"
    backend.home_scope = "host:/home"
    backend.transaction = transaction
    with pytest.raises(StorageError, match="another active Azure SQL process"):
        backend._register_incarnation()
    assert len(cursor.statements) == 1
    assert cursor.statements[0][0].startswith("SELECT")


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def execute(self, query, *args):
        self.statements.append((query, args))

    def fetchall(self):
        return self.rows


def test_sql_home_identity_is_stable_and_single_process_exclusive(tmp_path):
    assert _home_identity(tmp_path) == _home_identity(tmp_path)
    lock = _HomeLock(tmp_path / "sql-home.lock")
    try:
        with pytest.raises(StorageError, match="already using this home"):
            _HomeLock(tmp_path / "sql-home.lock")
    finally:
        lock.close()


class ConfigCursor:
    def __init__(self, payload):
        self.row = (json.dumps(payload),)
        self.statements = []
        self.lock = Lock()
        self.protected_rows = {
            "clients": {"client-1": {"id": "client-1"}},
            "leases": {"lease-1": {"client_id": "client-1", "running_jobs": ["job-1"]}},
            "pins": {"job-1": {"owner": "hub-b"}},
        }

    def execute(self, query, *args):
        self.statements.append((query, args))

    def fetchone(self):
        return self.row


def make_config_backend(cursor):
    @contextmanager
    def transaction(lock_name=None):
        assert lock_name == "privy.leases"
        with cursor.lock:
            yield cursor

    backend = object.__new__(SqlBackend)
    backend.transaction = transaction
    return backend


def test_concurrent_sql_starts_accept_equivalent_legacy_config_without_mutating_shared_state():
    legacy = yaml.safe_load(DEFAULT_CONFIG)
    current = yaml.safe_load(DEFAULT_CONFIG.replace("  manual_max_ttl: 365d\n", ""))
    expected = effective_lease_config(current)
    cursor = ConfigCursor(legacy)
    protected_before = json.loads(json.dumps(cursor.protected_rows))
    backend = make_config_backend(cursor)
    errors = []

    def start_hub():
        try:
            backend.check_config("leases", expected)
        except Exception as exc:
            errors.append(exc)

    starters = [Thread(target=start_hub) for _ in range(2)]
    for starter in starters:
        starter.start()
    for starter in starters:
        starter.join()

    assert errors == []
    assert len(cursor.statements) == 2
    assert all(statement[0].startswith("SELECT") for statement in cursor.statements)
    assert cursor.protected_rows == protected_before
    assert json.loads(cursor.row[0]) == legacy


def test_sql_lease_config_rejects_genuine_semantic_mismatch():
    current = yaml.safe_load(DEFAULT_CONFIG)
    expected = effective_lease_config(current)
    changed = yaml.safe_load(DEFAULT_CONFIG)
    changed["defaults"]["ttl"] = "45m"
    cursor = ConfigCursor(changed)
    backend = make_config_backend(cursor)

    with pytest.raises(StorageError, match="configuration differs"):
        backend.check_config("leases", expected)
    assert len(cursor.statements) == 1
    assert cursor.statements[0][0].startswith("SELECT")
