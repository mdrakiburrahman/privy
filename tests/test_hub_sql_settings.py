import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from privy.hub.settings import SettingsError, load_settings
from privy.hub.sql_store import SqlBackend, StorageError, _home_identity, _HomeLock


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
