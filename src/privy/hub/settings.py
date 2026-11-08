"""Strict, local-only hub backing-store settings."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


class SettingsError(ValueError):
    """Present settings must be valid; never implicitly select file leasing."""


@dataclass(frozen=True)
class SqlSettings:
    server: str
    database: str
    driver: str = "ODBC Driver 18 for SQL Server"
    retention_days: int = 7
    outbox_max_bytes: int = 256 * 1024 * 1024


def load_settings(home: Path, explicit: Path | None = None) -> SqlSettings | None:
    supplied = explicit or os.environ.get("PRIVY_HUB_SETTINGS")
    candidates = (
        [Path(supplied).expanduser()]
        if supplied
        else [home / "appsettings.yaml", Path.cwd() / "appsettings.yaml"]
    )
    path = next((p for p in candidates if p.exists()), candidates[0] if supplied else None)
    if path is None:
        return None
    try:
        with path.open(encoding="utf-8-sig") as stream:
            document = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsError(f"cannot read hub settings at {path}") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "storage"}
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        raise SettingsError("settings require version: 1 and storage, with no unknown fields")
    storage = document["storage"]
    if not isinstance(storage, dict):
        raise SettingsError("storage must be a mapping")
    if storage.get("mode") == "file" and set(storage) == {"mode"}:
        return None
    allowed = {"mode", "server", "database", "driver", "retention_days", "outbox_max_bytes"}
    if storage.get("mode") != "azure_sql" or set(storage) - allowed:
        raise SettingsError("storage.mode must be file or azure_sql, with no unknown fields")
    server, database = storage.get("server"), storage.get("database")
    if not isinstance(server, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", server):
        raise SettingsError("storage.server must be a SQL server DNS name")
    if not isinstance(database, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", database):
        raise SettingsError("storage.database must be a database name (letters, digits, '_' and '-')")
    driver = storage.get("driver", "ODBC Driver 18 for SQL Server")
    if driver != "ODBC Driver 18 for SQL Server":
        raise SettingsError("storage.driver must be ODBC Driver 18 for SQL Server")
    retention = storage.get("retention_days", 7)
    if type(retention) is not int or retention != 7:
        raise SettingsError("storage.retention_days must be 7")
    bound = storage.get("outbox_max_bytes", 256 * 1024 * 1024)
    if type(bound) is not int or not 1024 * 1024 <= bound <= 4 * 1024**3:
        raise SettingsError("storage.outbox_max_bytes must be between 1 MiB and 4 GiB")
    return SqlSettings(server, database, driver, retention, bound)
