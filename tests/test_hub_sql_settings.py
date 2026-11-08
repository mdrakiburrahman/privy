from pathlib import Path

import pytest

from privy.hub.settings import SettingsError, load_settings
from privy.hub.sql_store import StorageError, _HomeLock, _home_identity


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


def test_sql_home_identity_is_stable_and_single_process_exclusive(tmp_path):
    assert _home_identity(tmp_path) == _home_identity(tmp_path)
    lock = _HomeLock(tmp_path / "sql-home.lock")
    try:
        with pytest.raises(StorageError, match="already using this home"):
            _HomeLock(tmp_path / "sql-home.lock")
    finally:
        lock.close()
