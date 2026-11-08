"""Azure SQL coordination and ordered, restart-replayable local write-behind.

SQL is the authority. The SQLite outbox is a local delivery journal, not a
leasing fallback. Acknowledgments follow SQL commit and replay is idempotent.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import socket
import sqlite3
import struct
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from privy.hub.settings import SqlSettings

log = logging.getLogger("privy.hub")


def _profile_sql_enabled() -> bool:
    return os.environ.get("PRIVY_SQL_PROFILE") == "1"


class _ProfileCursor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor
        self._query = ""

    def execute(self, query, *params):
        self._query = query
        started = time.perf_counter()
        try:
            return self._cursor.execute(query, *params)
        except Exception as exc:
            log.exception(
                "sql_profile execute failed thread=%s error_type=%s error=%s query=%r",
                threading.get_ident(),
                type(exc).__name__,
                exc,
                query,
            )
            raise
        finally:
            log.info(
                "sql_profile execute thread=%s duration_ms=%.1f query=%r",
                threading.get_ident(),
                (time.perf_counter() - started) * 1000,
                query,
            )

    def fetchall(self):
        started = time.perf_counter()
        try:
            rows = self._cursor.fetchall()
        except Exception as exc:
            log.exception(
                "sql_profile fetchall failed thread=%s error_type=%s error=%s query=%r",
                threading.get_ident(),
                type(exc).__name__,
                exc,
                self._query,
            )
            raise
        log.info(
            "sql_profile fetchall thread=%s duration_ms=%.1f rows=%d query=%r",
            threading.get_ident(),
            (time.perf_counter() - started) * 1000,
            len(rows),
            self._query,
        )
        return rows

    def fetchone(self):
        started = time.perf_counter()
        try:
            row = self._cursor.fetchone()
        except Exception as exc:
            log.exception(
                "sql_profile fetchone failed thread=%s error_type=%s error=%s query=%r",
                threading.get_ident(),
                type(exc).__name__,
                exc,
                self._query,
            )
            raise
        log.info(
            "sql_profile fetchone thread=%s duration_ms=%.1f rows=%d query=%r",
            threading.get_ident(),
            (time.perf_counter() - started) * 1000,
            int(row is not None),
            self._query,
        )
        return row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class StorageError(RuntimeError):
    """An authoritative operation or durable delivery could not complete."""


class _HomeLock:
    """Prevent two processes from using the same home identity and local outbox."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0, os.SEEK_END)
                if self._stream.tell() == 0:
                    self._stream.write(b"\0")
                    self._stream.flush()
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                self._unlock = lambda: msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._unlock = lambda: fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        except (OSError, BlockingIOError) as exc:
            self._stream.close()
            raise StorageError("another Azure SQL hub process is already using this home") from exc

    def close(self) -> None:
        if self._stream.closed:
            return
        try:
            self._unlock()
        finally:
            self._stream.close()


def _home_identity(home: Path) -> str:
    path = home / ".sql-hub-id"
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        value = uuid.uuid4().hex
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(value + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        raise StorageError(f"invalid Azure SQL hub identity file: {path}")
    return value


class DurableOutbox:
    def __init__(self, path: Path, *, max_bytes: int) -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS pending (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT, payload TEXT, owner TEXT, size INTEGER)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS usage(value INTEGER NOT NULL, rejected INTEGER NOT NULL DEFAULT 0)"
        )
        if "rejected" not in {r[1] for r in self._db.execute("PRAGMA table_info(usage)")}:
            self._db.execute("ALTER TABLE usage ADD COLUMN rejected INTEGER NOT NULL DEFAULT 0")
        self._db.execute(
            "INSERT INTO usage(value) SELECT COALESCE(SUM(size),0) FROM pending HAVING NOT EXISTS(SELECT 1 FROM usage)"
        )
        self._db.execute(
            "CREATE TRIGGER IF NOT EXISTS usage_insert AFTER INSERT ON pending BEGIN UPDATE usage SET value=value+NEW.size; END"
        )
        self._db.execute(
            "CREATE TRIGGER IF NOT EXISTS usage_delete AFTER DELETE ON pending BEGIN UPDATE usage SET value=value-OLD.size; END"
        )
        self._db.commit()
        self.max_bytes = max_bytes

    def append(self, kind: str, key: str, payload: dict, owner: str) -> int:
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        size = len(raw.encode()) + len(key.encode()) + len(kind.encode()) + len(owner.encode())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            used = self._db.execute("SELECT value FROM usage").fetchone()[0]
            if used + size > self.max_bytes:
                self._db.execute("UPDATE usage SET rejected=rejected+1")
                self._db.commit()
                log.error("SQL delivery outbox is full; rejecting write without dropping pending data")
                raise StorageError("SQL delivery outbox is full; new writes rejected")
            try:
                cursor = self._db.execute(
                    "INSERT INTO pending(kind,key,payload,owner,size) VALUES(?,?,?,?,?)",
                    (kind, key, raw, owner, size),
                )
                self._db.commit()
                return cursor.lastrowid
            except sqlite3.Error as exc:
                self._db.rollback()
                log.error("Cannot durably append SQL delivery journal; rejecting write")
                raise StorageError("Cannot durably append SQL delivery journal") from exc

    def pending(self, limit: int = 100) -> list:
        with self._lock:
            return self._db.execute(
                "SELECT seq,kind,key,payload,owner FROM pending ORDER BY seq LIMIT ?", (limit,)
            ).fetchall()

    def ack(self, seq: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pending WHERE seq=?", (seq,))
            self._db.commit()

    def ack_batch(self, sequences: list[int]) -> None:
        with self._lock:
            self._db.executemany("DELETE FROM pending WHERE seq=?", ((seq,) for seq in sequences))
            self._db.commit()

    def status(self) -> dict:
        with self._lock:
            count, size = self._db.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM pending").fetchone()
            return {
                "pending_writes": count,
                "pending_bytes": size,
                "max_bytes": self.max_bytes,
                "rejected_writes": self._db.execute("SELECT rejected FROM usage").fetchone()[0],
            }

    def close(self) -> None:
        with self._lock:
            self._db.close()


class AzureSqlConnections:
    """Each transaction gets its own encrypted connection and a fresh-enough CLI token."""

    def __init__(self, settings: SqlSettings) -> None:
        try:
            import pyodbc
            from azure.identity import AzureCliCredential
        except ImportError as exc:
            raise StorageError("Azure SQL mode requires privy[sql] and Microsoft ODBC Driver 18") from exc
        if settings.driver not in pyodbc.drivers():
            raise StorageError("Install Microsoft ODBC Driver 18 for SQL Server to use Azure SQL mode")
        self._odbc = pyodbc
        self._credential = AzureCliCredential(process_timeout=30)
        self._token = None
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(8)
        self._idle: list[tuple[Any, float]] = []
        self._closed = False
        self.settings = settings

    def __call__(self):
        if not self._slots.acquire(timeout=15):
            raise StorageError("Azure SQL connection pool is busy; retry the request")
        try:
            return self._checkout()
        except BaseException:
            self._slots.release()
            raise

    def _checkout(self):
        with self._lock:
            if self._closed:
                raise StorageError("Azure SQL connection pool has closed")
            while self._idle:
                connection, expiry = self._idle.pop()
                if expiry > time.time() + 300:
                    return _PooledConnection(self, connection, expiry)
                connection.close()
            if self._token is None or self._token.expires_on <= time.time() + 300:
                try:
                    self._token = self._credential.get_token("https://database.windows.net/.default")
                except Exception as exc:
                    raise StorageError(
                        "AzureCliCredential could not obtain an Azure SQL token; sign in with Azure CLI"
                    ) from exc
            raw = self._token.token.encode("utf-16-le")
            token = struct.pack("<I", len(raw)) + raw
            expiry = self._token.expires_on
        s = self.settings
        try:
            connection = self._odbc.connect(
                f"Driver={{{s.driver}}};Server=tcp:{s.server},1433;Database={s.database};"
                "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=10;",
                attrs_before={1256: token},
                autocommit=False,
            )
            connection.timeout = 15
            return _PooledConnection(self, connection, expiry)
        except Exception as exc:
            raise StorageError("Cannot connect to authoritative Azure SQL backing store") from exc

    def _release(self, connection, expiry: float, broken: bool) -> None:
        try:
            with self._lock:
                if broken or self._closed or expiry <= time.time() + 300:
                    connection.close()
                else:
                    self._idle.append((connection, expiry))
        finally:
            self._slots.release()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for connection, _ in self._idle:
                connection.close()
            self._idle.clear()


class _PooledConnection:
    def __init__(self, pool: AzureSqlConnections, connection, expiry: float) -> None:
        self._pool, self._connection, self._expiry = pool, connection, expiry
        self._broken = False
        self._returned = False

    def cursor(self):
        return self._connection.cursor()

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        try:
            self._connection.rollback()
        except Exception:
            self._broken = True
            raise

    def invalidate(self) -> None:
        self._broken = True

    def close(self) -> None:
        if not self._returned:
            self._returned = True
            self._pool._release(self._connection, self._expiry, self._broken)


_SCHEMA = """
IF OBJECT_ID('dbo.privy_schema') IS NULL
    CREATE TABLE dbo.privy_schema(version int NOT NULL);
IF NOT EXISTS(SELECT 1 FROM dbo.privy_schema)
    INSERT dbo.privy_schema VALUES(1);
IF OBJECT_ID('dbo.privy_records') IS NULL
BEGIN
    CREATE TABLE dbo.privy_records(
        kind nvarchar(32) NOT NULL, id nvarchar(400) NOT NULL,
        payload nvarchar(max) NOT NULL, owner nvarchar(64) NOT NULL,
        revision bigint NOT NULL, at float NOT NULL,
        target nvarchar(300) NULL, lease_id nvarchar(80) NULL, client_id nvarchar(80) NULL,
        updated datetime2 NOT NULL DEFAULT SYSUTCDATETIME(),
        probe_status AS (CONVERT(nvarchar(32), JSON_VALUE(payload,'$.status'))),
        probe_latency_ms AS (TRY_CONVERT(int, JSON_VALUE(payload,'$.latency_ms'))),
        CONSTRAINT PK_privy_records PRIMARY KEY(kind,id));
    CREATE INDEX IX_privy_records_at ON dbo.privy_records(kind,at DESC);
    CREATE INDEX IX_privy_records_target ON dbo.privy_records(kind,target,at DESC);
    CREATE INDEX IX_privy_records_lease ON dbo.privy_records(kind,lease_id,at DESC);
    CREATE INDEX IX_privy_records_client ON dbo.privy_records(kind,client_id,at DESC);
END;
IF COL_LENGTH('dbo.privy_records','probe_status') IS NULL
    ALTER TABLE dbo.privy_records ADD probe_status AS (CONVERT(nvarchar(32), JSON_VALUE(payload,'$.status')));
IF COL_LENGTH('dbo.privy_records','probe_latency_ms') IS NULL
    ALTER TABLE dbo.privy_records ADD probe_latency_ms AS (TRY_CONVERT(int, JSON_VALUE(payload,'$.latency_ms')));
IF NOT EXISTS(SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.privy_records') AND name='IX_privy_probe_history')
    CREATE INDEX IX_privy_probe_history ON dbo.privy_records(at DESC)
        INCLUDE(target,probe_status,probe_latency_ms) WHERE kind='probe';
IF NOT EXISTS(SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.privy_records') AND name='IX_privy_probe_target_history')
    CREATE INDEX IX_privy_probe_target_history ON dbo.privy_records(target,at,id)
        INCLUDE(probe_status,probe_latency_ms) WHERE kind='probe';
IF NOT EXISTS(SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.privy_records') AND name='IX_privy_probe_target_latency')
    CREATE INDEX IX_privy_probe_target_latency ON dbo.privy_records(target,probe_latency_ms,at,id)
        WHERE kind='probe';
IF NOT EXISTS(SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.privy_records') AND name='IX_privy_probe_latency')
    CREATE INDEX IX_privy_probe_latency ON dbo.privy_records(probe_latency_ms,target,at,id)
        INCLUDE(probe_status) WHERE kind='probe';
IF OBJECT_ID('dbo.privy_coordination') IS NULL
BEGIN
    CREATE TABLE dbo.privy_coordination(
        kind nvarchar(32) NOT NULL, id nvarchar(400) NOT NULL, payload nvarchar(max) NOT NULL,
        ordinal bigint IDENTITY NOT NULL, updated datetime2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_privy_coordination PRIMARY KEY(kind,id));
    CREATE INDEX IX_privy_coordination_gc ON dbo.privy_coordination(kind,updated);
END;
IF OBJECT_ID('dbo.privy_cancels') IS NULL
    CREATE TABLE dbo.privy_cancels(
        job_id nvarchar(80) PRIMARY KEY, owner nvarchar(64) NOT NULL,
        requested datetime2 NOT NULL DEFAULT SYSUTCDATETIME(), acknowledged datetime2 NULL);
IF OBJECT_ID('dbo.privy_cursors') IS NULL
    CREATE TABLE dbo.privy_cursors(job_id nvarchar(80), stream nvarchar(8), offset bigint NOT NULL,
        updated datetime2 NOT NULL DEFAULT SYSUTCDATETIME(), PRIMARY KEY(job_id,stream));
IF OBJECT_ID('dbo.privy_output') IS NULL
BEGIN
    CREATE TABLE dbo.privy_output(job_id nvarchar(80), chunk_index bigint, stream nvarchar(8),
        start_offset bigint NOT NULL, end_offset bigint NOT NULL, payload nvarchar(max) NOT NULL,
        PRIMARY KEY(job_id,chunk_index));
    CREATE INDEX IX_privy_output_read ON dbo.privy_output(job_id,stream,start_offset) INCLUDE(end_offset);
END;
"""

_FLUSH_BATCH = """
DECLARE @rows TABLE(kind nvarchar(32), id nvarchar(400), payload nvarchar(max),
    owner nvarchar(64), revision bigint, at float, target nvarchar(300),
    lease_id nvarchar(80), client_id nvarchar(80));
INSERT @rows SELECT * FROM OPENJSON(?)
WITH(kind nvarchar(32), id nvarchar(400), payload nvarchar(max) AS JSON,
     owner nvarchar(64), revision bigint, at float, target nvarchar(300),
     lease_id nvarchar(80), client_id nvarchar(80));
DECLARE @latest TABLE(kind nvarchar(32), id nvarchar(400), payload nvarchar(max),
    owner nvarchar(64), revision bigint, at float, target nvarchar(300),
    lease_id nvarchar(80), client_id nvarchar(80), PRIMARY KEY(kind,id));
INSERT @latest SELECT r.* FROM @rows r WHERE revision=(
    SELECT MAX(revision) FROM @rows p WHERE p.kind=r.kind AND p.id=r.id);
IF EXISTS(SELECT 1 FROM @latest n JOIN dbo.privy_records p WITH(UPDLOCK,HOLDLOCK)
    ON p.kind=n.kind AND p.id=n.id WHERE n.kind='job' AND p.owner<>n.owner)
    THROW 51001, 'Privy job ownership conflict', 1;
UPDATE p SET payload=n.payload,owner=n.owner,revision=n.revision,at=n.at,
    target=n.target,lease_id=n.lease_id,client_id=n.client_id,updated=SYSUTCDATETIME()
FROM dbo.privy_records p WITH(UPDLOCK,HOLDLOCK)
JOIN @latest n ON p.kind=n.kind AND p.id=n.id
WHERE n.kind<>'output' AND (
    (p.owner=n.owner AND p.revision<n.revision) OR
    (n.kind<>'job' AND p.owner<>n.owner AND p.at<n.at));
INSERT dbo.privy_records(kind,id,payload,owner,revision,at,target,lease_id,client_id)
SELECT n.kind,n.id,n.payload,n.owner,n.revision,n.at,n.target,n.lease_id,n.client_id
FROM @latest n WHERE n.kind<>'output' AND NOT EXISTS(
    SELECT 1 FROM dbo.privy_records p WITH(UPDLOCK,HOLDLOCK) WHERE p.kind=n.kind AND p.id=n.id);
INSERT dbo.privy_output(job_id,chunk_index,stream,start_offset,end_offset,payload)
SELECT JSON_VALUE(n.payload,'$.job_id'),CAST(JSON_VALUE(n.payload,'$.index') AS bigint),
    JSON_VALUE(n.payload,'$.stream'),CAST(JSON_VALUE(n.payload,'$.start') AS bigint),
    CAST(JSON_VALUE(n.payload,'$.end') AS bigint),n.payload
FROM @latest n WHERE n.kind='output' AND NOT EXISTS(
    SELECT 1 FROM dbo.privy_output p WITH(UPDLOCK,HOLDLOCK)
    WHERE p.job_id=JSON_VALUE(n.payload,'$.job_id')
      AND p.chunk_index=CAST(JSON_VALUE(n.payload,'$.index') AS bigint));
"""


class SqlBackend:
    def __init__(self, settings: SqlSettings, home: Path, *, connect=None, start_worker: bool = True) -> None:
        self.settings = settings
        self._home_lock = _HomeLock(home / "sql-home.lock")
        try:
            self.hub_id = _home_identity(home)
            self.home_scope = f"{socket.gethostname().casefold()}:{str(home.resolve()).casefold()}"
            self.owner = uuid.uuid4().hex
            self._fenced = False
            self._closed = False
            self._migration_status: dict[str, Any] | None = None
            self._connect = connect or AzureSqlConnections(settings)
        except BaseException:
            self._home_lock.close()
            raise
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._flush_lock = threading.Lock()
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None
        self.outbox = None
        try:
            self.bootstrap()
            self.outbox = DurableOutbox(home / "sql-outbox.sqlite3", max_bytes=settings.outbox_max_bytes)
            self._register_incarnation()
        except BaseException:
            if self.outbox is not None:
                self.outbox.close()
            if hasattr(self._connect, "close"):
                self._connect.close()
            self._home_lock.close()
            raise
        log.info(
            "Storage: azure_sql (%s/%s); hub %s; owner incarnation %s; no file fallback",
            settings.server,
            settings.database,
            self.hub_id,
            self.owner,
        )
        if start_worker:
            self._thread = threading.Thread(target=self._work, name="privy-sql-delivery", daemon=True)
            self._thread.start()

    @contextmanager
    def transaction(self, lock: str | None = None):
        profile = _profile_sql_enabled()
        connection_started = time.perf_counter()
        try:
            connection = self._connect()
        except Exception as exc:
            if profile:
                log.exception(
                    "sql_profile connection_acquire failed thread=%s error_type=%s error=%s",
                    threading.get_ident(),
                    type(exc).__name__,
                    exc,
                )
            raise
        if profile:
            log.info(
                "sql_profile connection_acquire thread=%s duration_ms=%.1f",
                threading.get_ident(),
                (time.perf_counter() - connection_started) * 1000,
            )
        cursor = None

        def close_cursor() -> None:
            nonlocal cursor
            if cursor is None:
                return
            current, cursor = cursor, None
            current.close()

        cursor_close_error = None
        try:
            raw_cursor = connection.cursor()
            cursor = _ProfileCursor(raw_cursor) if profile else raw_cursor
            cursor.execute("SET NOCOUNT ON; SET XACT_ABORT ON; SET LOCK_TIMEOUT 15000;")
            if lock:
                cursor.execute(
                    "DECLARE @r int; EXEC @r=sys.sp_getapplock @Resource=?, @LockMode='Exclusive', "
                    "@LockOwner='Transaction', @LockTimeout=15000; SELECT @r;",
                    lock,
                )
                if cursor.fetchone()[0] < 0:
                    raise StorageError("SQL coordination lock timed out")
            yield cursor
            try:
                close_cursor()
            except Exception as close_error:
                cursor_close_error = close_error
                raise
            connection.commit()
        except BaseException as exc:
            if type(exc).__module__ == "pyodbc" and hasattr(connection, "invalidate"):
                connection.invalidate()
            if cursor_close_error is exc and hasattr(connection, "invalidate"):
                connection.invalidate()
            try:
                close_cursor()
            except Exception:
                if hasattr(connection, "invalidate"):
                    connection.invalidate()
                log.exception("Azure SQL cursor close failed; connection discarded")
            try:
                connection.rollback()
            except Exception as rollback_error:
                if hasattr(connection, "invalidate"):
                    connection.invalidate()
                log.exception("Azure SQL rollback failed; connection discarded, no file fallback")
                raise StorageError("Authoritative SQL transaction rollback failed") from rollback_error
            if cursor_close_error is exc:
                raise StorageError("Authoritative SQL cursor cleanup failed; no file fallback") from exc
            if type(exc).__module__ == "pyodbc":
                raise StorageError("Authoritative Azure SQL transaction failed; no file fallback") from exc
            raise
        finally:
            connection.close()

    def bootstrap(self) -> None:
        with self.transaction("privy.schema.v1") as cursor:
            cursor.execute(_SCHEMA)
            cursor.execute("SELECT version FROM dbo.privy_schema")
            versions = [row[0] for row in cursor.fetchall()]
            if versions != [1]:
                raise StorageError("Unsupported Privy SQL schema version")
            cursor.execute("SELECT payload FROM dbo.privy_coordination WHERE kind='config' AND id='storage'")
            row = cursor.fetchone()
            config = {"retention_days": self.settings.retention_days}
            if row and json.loads(row[0]) != config:
                raise StorageError("SQL storage configuration differs between hubs")
            if not row:
                cursor.execute(
                    "INSERT dbo.privy_coordination(kind,id,payload) VALUES('config','storage',?)",
                    json.dumps(config),
                )

    def _register_incarnation(self) -> None:
        with self.transaction("privy.leases") as cursor:
            cursor.execute(
                "SELECT id, payload, CASE WHEN updated < DATEADD(second,-90,SYSUTCDATETIME()) "
                "THEN 1 ELSE 0 END FROM dbo.privy_coordination "
                "WHERE kind='hub' AND id<>? AND JSON_VALUE(payload,'$.hub_id')=?",
                self.owner,
                self.hub_id,
            )
            previous = cursor.fetchall()
            for owner, payload, stale in previous:
                heartbeat = json.loads(payload)
                if (
                    heartbeat.get("active", True)
                    and not stale
                    and heartbeat.get("home_scope") != self.home_scope
                ):
                    raise StorageError("another active Azure SQL process is using this hub home identity")
                cursor.execute(
                    "UPDATE dbo.privy_coordination SET payload=?,updated=SYSUTCDATETIME() "
                    "WHERE kind='hub' AND id=?",
                    json.dumps({**heartbeat, "active": False}),
                    owner,
                )
            cursor.execute(
                "INSERT dbo.privy_coordination(kind,id,payload) VALUES('hub',?,?)",
                self.owner,
                json.dumps(
                    {
                        "owner": self.owner,
                        "hub_id": self.hub_id,
                        "home_scope": self.home_scope,
                        "active": True,
                    }
                ),
            )

    def assert_owner_active(self, cursor) -> None:
        if self._fenced or self._closed:
            raise StorageError("this Azure SQL hub incarnation is no longer active")
        cursor.execute(
            "SELECT 1 FROM dbo.privy_coordination WHERE kind='hub' AND id=? "
            "AND JSON_VALUE(payload,'$.active')='true'",
            self.owner,
        )
        if cursor.fetchone() is None:
            self._fenced = True
            self._stop.set()
            raise StorageError("this Azure SQL hub incarnation was fenced; restart the hub")

    def set_migration_status(self, status: dict[str, Any]) -> None:
        self._migration_status = dict(status)

    def check_config(self, name: str, payload: dict) -> None:
        with self.transaction("privy.leases") as cursor:
            cursor.execute("SELECT payload FROM dbo.privy_coordination WHERE kind='config' AND id=?", name)
            row = cursor.fetchone()
            if row:
                stored = json.loads(row[0])
                if stored != payload:
                    compatible = False
                    if name == "leases":
                        from privy.hub.leases import effective_lease_config

                        try:
                            compatible = effective_lease_config(stored) == payload
                        except (KeyError, TypeError, ValueError):
                            compatible = False
                    if not compatible:
                        raise StorageError(
                            f"Shared hub {name} configuration differs; refusing independent coordination"
                        )
                    log.info("Accepted equivalent legacy shared leases configuration without rewriting it")
            else:
                cursor.execute(
                    "INSERT dbo.privy_coordination(kind,id,payload) VALUES('config',?,?)",
                    name,
                    json.dumps(payload, sort_keys=True),
                )

    def enqueue(self, kind: str, key: str, payload: dict) -> None:
        self._enqueue(kind, key, payload)

    def _enqueue(self, kind: str, key: str, payload: dict) -> int:
        if not kind or len(kind) > 32 or not key or len(key) > 400:
            raise StorageError("SQL record identity is invalid")
        result = self.outbox.append(kind, key, payload, self.owner)
        self._wake.set()
        return result

    def put(self, kind: str, key: str, payload: dict) -> None:
        seq = self._enqueue(kind, key, payload)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self._drain()
            pending = self.outbox.pending(1)
            if not pending or pending[0][0] > seq:
                return
        raise StorageError("SQL synchronous write did not complete; retained in delivery journal")

    def _drain(self) -> None:
        with self._flush_lock:
            rows = self.outbox.pending(500)
            if not rows:
                return
            batch = []
            for seq, kind, key, raw, owner in rows:
                payload = json.loads(raw)
                at = payload.get("created_at", payload.get("at", time.time()))
                if not isinstance(at, (int, float)):
                    at = time.time()
                batch.append(
                    {
                        "kind": kind,
                        "id": key,
                        "payload": payload,
                        "owner": owner,
                        "revision": seq,
                        "at": at,
                        "target": payload.get("target"),
                        "lease_id": payload.get("lease_id"),
                        "client_id": payload.get("client_id"),
                    }
                )
            with self.transaction("privy.leases") as cursor:
                self.assert_owner_active(cursor)
                cursor.execute(_FLUSH_BATCH, json.dumps(batch, ensure_ascii=True))
            self.outbox.ack_batch([row[0] for row in rows])
            self._last_error = None

    def get(self, kind: str, key: str) -> dict | None:
        with self.transaction() as cursor:
            cursor.execute("SELECT payload FROM dbo.privy_records WHERE kind=? AND id=?", kind, key)
            row = cursor.fetchone()
            return json.loads(row[0]) if row else None

    def get_many(self, kind: str, keys: list[str]) -> dict[str, dict]:
        unique_keys = list(dict.fromkeys(keys))
        if not unique_keys:
            return {}
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT id,payload FROM dbo.privy_records WHERE kind=? "
                "AND id IN (SELECT value FROM OPENJSON(?))",
                kind,
                json.dumps(unique_keys, ensure_ascii=True),
            )
            return {str(key): json.loads(payload) for key, payload in cursor.fetchall()}

    def list(
        self,
        kind: str,
        *,
        limit: int = 200,
        before: float | None = None,
        after: float | None = None,
        target: str | None = None,
        lease_id: str | None = None,
        client_id: str | None = None,
        states: set[str] | frozenset[str] | None = None,
    ) -> list[dict]:
        if not 1 <= limit <= 100_000:
            raise StorageError("SQL page size must be between 1 and 100000")
        if states is not None and not states:
            return []
        clauses, params = ["kind=?"], [kind]
        for column, value in (("target", target), ("lease_id", lease_id), ("client_id", client_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if before is not None:
            clauses.append("at<?")
            params.append(before)
        if after is not None:
            clauses.append("at>=?")
            params.append(after)
        if states is not None:
            state_values = tuple(sorted(states))
            clauses.append("JSON_VALUE(payload,'$.state') IN (" + ",".join("?" for _ in state_values) + ")")
            params.extend(state_values)
        with self.transaction() as cursor:
            cursor.execute(
                f"SELECT TOP (?) payload FROM dbo.privy_records WHERE {' AND '.join(clauses)} ORDER BY at DESC,id DESC",
                limit,
                *params,
            )
            return [json.loads(row[0]) for row in cursor.fetchall()]

    def count_jobs_by_target(self, *, lease_id: str | None = None) -> dict[str, int]:
        clauses = ["kind='job'", "target IS NOT NULL"]
        params = []
        if lease_id is not None:
            clauses.append("lease_id=?")
            params.append(lease_id)
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT target,COUNT_BIG(*) FROM dbo.privy_records WHERE "
                + " AND ".join(clauses)
                + " GROUP BY target",
                *params,
            )
            return {str(row[0]): int(row[1]) for row in cursor.fetchall() if row[0] is not None}

    def count_jobs_by_lease(self) -> dict[str, int]:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT lease_id,COUNT_BIG(*) FROM dbo.privy_records "
                "WHERE kind='job' AND lease_id IS NOT NULL AND lease_id<>'' GROUP BY lease_id"
            )
            return {str(row[0]): int(row[1]) for row in cursor.fetchall() if row[0] is not None}

    def count_job_records(
        self,
        *,
        lease_id: str | None = None,
        states: set[str] | frozenset[str] | None = None,
        after: float | None = None,
        before: float | None = None,
    ) -> int:
        if states is not None and not states:
            return 0
        clauses, params = ["kind='job'"], []
        if lease_id is not None:
            clauses.append("lease_id=?")
            params.append(lease_id)
        if after is not None:
            clauses.append("at>=?")
            params.append(after)
        if before is not None:
            clauses.append("at<?")
            params.append(before)
        if states is not None:
            state_values = tuple(sorted(states))
            clauses.append("JSON_VALUE(payload,'$.state') IN (" + ",".join("?" for _ in state_values) + ")")
            params.extend(state_values)
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT COUNT_BIG(*) FROM dbo.privy_records WHERE " + " AND ".join(clauses),
                *params,
            )
            return int(cursor.fetchone()[0])

    def count_jobs(self) -> dict:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT JSON_VALUE(payload,'$.state'),COUNT_BIG(*) FROM dbo.privy_records WHERE kind='job' GROUP BY JSON_VALUE(payload,'$.state')"
            )
            counts = {row[0]: int(row[1]) for row in cursor.fetchall()}
            return {
                "queued": counts.get("queued", 0),
                "running": counts.get("running", 0),
                "retained": sum(counts.values()),
            }

    def probe_history(self, *, after=None, before=None):
        clauses = ["kind='probe'"]
        params = []
        if after is not None:
            clauses.append("at>=?")
            params.append(after)
        if before is not None:
            clauses.append("at<?")
            params.append(before)
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT target,at,id,probe_status,probe_latency_ms "
                "FROM dbo.privy_records WHERE " + " AND ".join(clauses),
                *params,
            )
            rows = cursor.fetchall()
            latest = {}
            for target, at, key, *_ in rows:
                candidate = (float(at), str(key))
                if target not in latest or candidate > latest[target]:
                    latest[target] = candidate
            latest_ids = [key for _, key in latest.values()]
            payloads = {}
            if latest_ids:
                cursor.execute(
                    "SELECT id,payload FROM dbo.privy_records WHERE kind='probe' "
                    "AND id IN (SELECT value FROM OPENJSON(?))",
                    json.dumps(latest_ids),
                )
                payload_rows = cursor.fetchall()
                decode_started = time.perf_counter()
                payloads = {str(key): json.loads(payload) for key, payload in payload_rows}
                if _profile_sql_enabled():
                    log.info(
                        "sql_profile record_decode thread=%s kind=probe_latest rows=%d duration_ms=%.1f",
                        threading.get_ident(),
                        len(payload_rows),
                        (time.perf_counter() - decode_started) * 1000,
                    )

        decode_started = time.perf_counter()
        result = []
        for target, at, key, status, latency_ms in rows:
            row = {
                "target": target,
                "at": float(at),
                "status": status,
                "latency_ms": latency_ms,
            }
            if latest.get(target) == (float(at), str(key)) and str(key) in payloads:
                row.update(payloads[str(key)])
            result.append(row)
        if _profile_sql_enabled():
            log.info(
                "sql_profile record_decode thread=%s kind=probe rows=%d latest_payloads=%d duration_ms=%.1f",
                threading.get_ident(),
                len(rows),
                len(payloads),
                (time.perf_counter() - decode_started) * 1000,
            )
        return result

    def report_history(self, *, after=None, before=None):
        """Read all dashboard history on one fresh connection and transaction."""
        started = time.perf_counter()

        def query(kind):
            if kind == "probe":
                clauses, params = ["kind='probe'"], []
            else:
                clauses, params = ["kind=?"], [kind]
            if after is not None:
                clauses.append("at>=?")
                params.append(after)
            if before is not None:
                clauses.append("at<?")
                params.append(before)
            return " AND ".join(clauses), params

        decoded = {}
        with self.transaction() as cursor:

            def execute_batch(statements, params):
                cursor.execute(";\n".join(statements), *params)
                result_sets = []
                for index in range(len(statements)):
                    result_sets.append(cursor.fetchall())
                    has_next = bool(cursor.nextset())
                    if has_next != (index + 1 < len(statements)):
                        raise StorageError("Azure SQL report history returned an unexpected result-set count")
                return result_sets

            job_where, job_params = query("job")
            purpose_where, purpose_params = query("purpose")
            latest_before_sql = (
                "WITH ranked AS (SELECT payload,ROW_NUMBER() OVER(PARTITION BY kind,target "
                "ORDER BY at DESC,id DESC) AS n FROM dbo.privy_records "
                "WHERE kind IN('job','purpose') AND at<?) SELECT payload FROM ranked WHERE n=1"
            )
            latest_before_params = [after] if after is not None else []
            if after is None:
                latest_before_sql = "SELECT payload FROM dbo.privy_records WHERE 1=0"
            where, params = query("probe")
            probe_sql = f"""
                WITH summary AS (
                    SELECT target,COUNT_BIG(*) AS sample_count,
                        SUM(CONVERT(bigint,CASE WHEN probe_status='online' THEN 1 ELSE 0 END)) AS online_count,
                        MAX(CASE WHEN probe_status='online' THEN at END) AS last_online,
                        MIN(at) AS earliest_at
                    FROM dbo.privy_records WHERE {where}
                    GROUP BY target
                )
                SELECT summary.target,summary.sample_count,summary.online_count,
                    summary.last_online,summary.earliest_at,latest.at,latest.payload,
                    latest.probe_status,latest.probe_latency_ms
                FROM summary
                CROSS APPLY (
                    SELECT TOP(1) p.at,p.payload,p.probe_status,p.probe_latency_ms
                    FROM dbo.privy_records p
                    WHERE {where} AND p.target=summary.target
                    ORDER BY p.at DESC,p.id DESC
                ) latest
                """
            status_where, status_params = query("probe")
            status_sql = f"""
                WITH ordered AS (
                    SELECT target,at,id,probe_status,
                        ROW_NUMBER() OVER(PARTITION BY target ORDER BY at,id) AS sample_rank,
                        LAG(probe_status) OVER(PARTITION BY target ORDER BY at,id) AS previous_status
                    FROM dbo.privy_records WHERE {status_where}
                ), changed AS (
                    SELECT *,CASE WHEN sample_rank=1
                        OR (previous_status IS NULL AND probe_status IS NOT NULL)
                        OR (previous_status IS NOT NULL AND probe_status IS NULL)
                        OR previous_status<>probe_status THEN 1 ELSE 0 END AS status_changed
                    FROM ordered
                )
                SELECT target,MAX(at),MAX(CASE WHEN sample_rank=1 THEN 1 ELSE 0 END)
                FROM changed WHERE status_changed=1 GROUP BY target
            """
            target_where, target_params = query("probe")
            target_where += " AND probe_status='online' AND probe_latency_ms IS NOT NULL"
            target_p50_sql = f"""
                WITH counts AS (
                    SELECT target,COUNT_BIG(*) AS sample_count
                    FROM dbo.privy_records WHERE {target_where}
                    GROUP BY target
                ), ranked AS (
                    SELECT p.target,p.probe_latency_ms,
                        ROW_NUMBER() OVER(PARTITION BY p.target ORDER BY p.probe_latency_ms,p.at,p.id) AS sample_rank,
                        counts.sample_count
                    FROM dbo.privy_records p
                    JOIN counts ON counts.target=p.target
                    WHERE {target_where}
                )
                SELECT target,MAX(probe_latency_ms)
                FROM ranked
                WHERE sample_rank=(sample_count-1)/2 +
                    CASE WHEN (sample_count-1)%2=1
                        AND ((sample_count-1)/2)%2=1 THEN 1 ELSE 0 END + 1
                GROUP BY target
            """
            overall_where, overall_params = query("probe")
            overall_where += " AND probe_status='online' AND probe_latency_ms IS NOT NULL"
            overall_sql = f"""
                WITH counts AS (
                    SELECT COUNT_BIG(*) AS sample_count
                    FROM dbo.privy_records WHERE {overall_where}
                    HAVING COUNT_BIG(*) > 0
                )
                SELECT p50.probe_latency_ms,p95.probe_latency_ms
                FROM counts
                CROSS APPLY (
                    SELECT probe_latency_ms FROM dbo.privy_records
                    WHERE {overall_where}
                        AND probe_status='online' AND probe_latency_ms IS NOT NULL
                    ORDER BY probe_latency_ms,target,at,id
                    OFFSET (counts.sample_count-1)/2 +
                        CASE WHEN (counts.sample_count-1)%2=1
                            AND ((counts.sample_count-1)/2)%2=1 THEN 1 ELSE 0 END ROWS
                    FETCH NEXT 1 ROWS ONLY
                ) p50
                CROSS APPLY (
                    SELECT probe_latency_ms FROM dbo.privy_records
                    WHERE {overall_where}
                        AND probe_status='online' AND probe_latency_ms IS NOT NULL
                    ORDER BY probe_latency_ms,target,at,id
                    OFFSET ((counts.sample_count-1)*19)/20 +
                        CASE WHEN ((counts.sample_count-1)*19)%20>10
                            OR (((counts.sample_count-1)*19)%20=10
                                AND (((counts.sample_count-1)*19)/20)%2=1)
                            THEN 1 ELSE 0 END ROWS
                    FETCH NEXT 1 ROWS ONLY
                ) p95
            """
            call_where, call_params = query("call")
            calls_sql = (
                "SELECT JSON_VALUE(payload,'$.surface'),COUNT_BIG(*),"
                "SUM(CASE WHEN TRY_CONVERT(int,JSON_VALUE(payload,'$.status'))>=400 THEN 1 ELSE 0 END),"
                f"MIN(at) FROM dbo.privy_records WHERE {call_where} "
                "GROUP BY JSON_VALUE(payload,'$.surface')"
            )
            job_counts_sql = (
                "SELECT JSON_VALUE(payload,'$.state'),COUNT_BIG(*) "
                "FROM dbo.privy_records WHERE kind='job' GROUP BY JSON_VALUE(payload,'$.state')"
            )
            (
                job_rows,
                purpose_rows,
                latest_before_rows,
                probe_rows,
                status_result,
                target_p50_result,
                overall_result,
                call_rows,
                job_count_rows,
            ) = execute_batch(
                [
                    f"SELECT payload FROM dbo.privy_records WHERE {job_where}",
                    f"SELECT payload FROM dbo.privy_records WHERE {purpose_where}",
                    latest_before_sql,
                    probe_sql,
                    status_sql,
                    target_p50_sql,
                    overall_sql,
                    calls_sql,
                    job_counts_sql,
                ],
                [
                    *job_params,
                    *purpose_params,
                    *latest_before_params,
                    *params,
                    *params,
                    *status_params,
                    *target_params,
                    *target_params,
                    *overall_params,
                    *overall_params,
                    *overall_params,
                    *call_params,
                ],
            )
            for name, kind, rows in (
                ("jobs", "job", job_rows),
                ("purposes", "purpose", purpose_rows),
            ):
                decode_started = time.perf_counter()
                decoded[name] = [json.loads(row[0]) for row in rows]
                if _profile_sql_enabled():
                    log.info(
                        "sql_profile record_decode thread=%s kind=%s rows=%d duration_ms=%.1f",
                        threading.get_ident(),
                        kind,
                        len(rows),
                        (time.perf_counter() - decode_started) * 1000,
                    )
            decoded["latest_before"] = [json.loads(row[0]) for row in latest_before_rows]
            status_rows = {str(target): (float(since), bool(first)) for target, since, first in status_result}
            latency_by_target = {
                str(target): float(p50) if p50 is not None else None for target, p50 in target_p50_result
            }
            overall_row = overall_result[0] if overall_result else None
            overall_p50, overall_p95 = overall_row if overall_row is not None else (None, None)
            overall_p50 = float(overall_p50) if overall_p50 is not None else None
            overall_p95 = float(overall_p95) if overall_p95 is not None else None
            counts = {row[0]: int(row[1]) for row in job_count_rows}

            by_target = {}
            decoded["probes"] = []
            sample_count = 0
            for row in probe_rows:
                target = str(row[0])
                stats = {
                    "samples": int(row[1]),
                    "online": int(row[2]),
                    "latency_p50_ms": latency_by_target.get(target),
                    "last_online": float(row[3]) if row[3] is not None else None,
                    "earliest_at": float(row[4]),
                    "status_since": status_rows.get(target, (float(row[4]), True))[0],
                    "status_since_first_check": status_rows.get(target, (float(row[4]), True))[1],
                }
                by_target[target] = stats
                sample_count += stats["samples"]
                latest = json.loads(row[6])
                latest.update(target=target, at=float(row[5]), status=row[7], latency_ms=row[8])
                decoded["probes"].append(latest)

            decoded["probe_stats"] = {
                "by_target": by_target,
                "samples": sample_count,
                "latency_p50_ms": overall_p50,
                "latency_p95_ms": overall_p95,
            }
            call_stats = {"total": 0, "by_surface": {}, "errors": 0, "earliest_at": None}
            for surface, count, errors, earliest_at in call_rows:
                count, errors = int(count), int(errors or 0)
                call_stats["total"] += count
                call_stats["by_surface"][surface] = count
                call_stats["errors"] += errors
                if earliest_at is not None:
                    first = float(earliest_at)
                    if call_stats["earliest_at"] is None or first < call_stats["earliest_at"]:
                        call_stats["earliest_at"] = first
            decoded["call_stats"] = call_stats
            decoded["calls"] = []

            decoded["job_counts"] = {
                "queued": counts.get("queued", 0),
                "running": counts.get("running", 0),
                "retained": sum(counts.values()),
            }

        if _profile_sql_enabled():
            log.info(
                "sql_profile report_history thread=%s duration_ms=%.1f jobs=%d purposes=%d "
                "probe_samples=%d probe_targets=%d calls=%d latest_before=%d",
                threading.get_ident(),
                (time.perf_counter() - started) * 1000,
                len(decoded["jobs"]),
                len(decoded["purposes"]),
                decoded["probe_stats"]["samples"],
                len(decoded["probes"]),
                decoded["call_stats"]["total"],
                len(decoded["latest_before"]),
            )
        return decoded

    def scan(self, kind: str, *, before=None, after=None, target=None, lease_id=None, client_id=None):
        page_size = 5000
        clauses, params = ["kind=?"], [kind]
        for column, value in (("target", target), ("lease_id", lease_id), ("client_id", client_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if before is not None:
            clauses.append("at<?")
            params.append(before)
        if after is not None:
            clauses.append("at>=?")
            params.append(after)
        boundary = None
        while True:
            page_clauses, page_params = list(clauses), list(params)
            if boundary is not None:
                at, key = boundary
                page_clauses.append("(at<? OR (at=? AND id<?))")
                page_params.extend((at, at, key))
            with self.transaction() as cursor:
                cursor.execute(
                    f"SELECT TOP ({page_size}) at,id,payload FROM dbo.privy_records "
                    f"WHERE {' AND '.join(page_clauses)} ORDER BY at DESC,id DESC",
                    *page_params,
                )
                page = cursor.fetchall()
            decode_started = time.perf_counter()
            decoded = [json.loads(row[2]) for row in page]
            if _profile_sql_enabled():
                log.info(
                    "sql_profile record_decode thread=%s kind=%s rows=%d duration_ms=%.1f",
                    threading.get_ident(),
                    kind,
                    len(decoded),
                    (time.perf_counter() - decode_started) * 1000,
                )
            yield from decoded
            if len(page) < page_size:
                return
            boundary = page[-1][0], page[-1][1]

    def latest_before(self, before: float) -> list[dict]:
        with self.transaction() as cursor:
            cursor.execute(
                "WITH ranked AS (SELECT payload,ROW_NUMBER() OVER(PARTITION BY kind,target ORDER BY at DESC,id DESC) AS n "
                "FROM dbo.privy_records WHERE kind IN('job','purpose') AND at<?) SELECT payload FROM ranked WHERE n=1",
                before,
            )
            return [json.loads(row[0]) for row in cursor.fetchall()]

    def health(self) -> dict:
        with self.transaction() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone()[0] != 1:
                raise StorageError("Azure SQL health probe failed")
        return self.status()

    def delete(self, kind: str, key: str) -> None:
        with self.transaction() as cursor:
            cursor.execute("DELETE dbo.privy_records WHERE kind=? AND id=?", kind, key)

    def output(self, job_id: str, stream: str, offset: int, limit: int) -> list[dict]:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT payload FROM dbo.privy_output WHERE job_id=? AND stream=? AND end_offset>? AND start_offset<? ORDER BY start_offset",
                job_id,
                stream,
                offset,
                offset + limit,
            )
            return [json.loads(row[0]) for row in cursor.fetchall()]

    def pieces(self, job_id: str, index: int, limit: int = 200) -> list[dict]:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT TOP (?) payload FROM dbo.privy_output WHERE job_id=? AND chunk_index>=? ORDER BY chunk_index",
                limit,
                job_id,
                index,
            )
            return [json.loads(row[0]) for row in cursor.fetchall()]

    def advance_cursor(self, job_id: str, stream: str, end: int) -> int:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT offset FROM dbo.privy_cursors WITH(UPDLOCK,HOLDLOCK) WHERE job_id=? AND stream=?",
                job_id,
                stream,
            )
            row = cursor.fetchone()
            previous = row[0] if row else 0
            if row:
                cursor.execute(
                    "UPDATE dbo.privy_cursors SET offset=?,updated=SYSUTCDATETIME() WHERE job_id=? AND stream=?",
                    max(previous, end),
                    job_id,
                    stream,
                )
            else:
                cursor.execute(
                    "INSERT dbo.privy_cursors(job_id,stream,offset) VALUES(?,?,?)", job_id, stream, end
                )
            return previous

    def request_cancel(self, job_id: str) -> dict:
        with self.transaction() as cursor:
            cursor.execute("SELECT owner FROM dbo.privy_records WHERE kind='job' AND id=?", job_id)
            row = cursor.fetchone()
            if row is None:
                raise StorageError("Cannot cancel an unknown shared job")
            cursor.execute(
                "IF NOT EXISTS(SELECT 1 FROM dbo.privy_cancels WITH(UPDLOCK,HOLDLOCK) WHERE job_id=?) INSERT dbo.privy_cancels(job_id,owner) VALUES(?,?)",
                job_id,
                job_id,
                row[0],
            )
            cursor.execute("SELECT acknowledged FROM dbo.privy_cancels WHERE job_id=?", job_id)
            acknowledged = cursor.fetchone()[0] is not None
            return {"job_id": job_id, "requested": True, "acknowledged": acknowledged}

    def pending_cancels(self) -> list[str]:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT TOP(100) job_id FROM dbo.privy_cancels WHERE owner=? AND acknowledged IS NULL ORDER BY requested",
                self.owner,
            )
            return [row[0] for row in cursor.fetchall()]

    def ack_cancel(self, job_id: str) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE dbo.privy_cancels SET acknowledged=SYSUTCDATETIME() WHERE job_id=? AND owner=?",
                job_id,
                self.owner,
            )

    def heartbeat(self) -> None:
        with self.transaction("privy.leases") as cursor:
            cursor.execute(
                "UPDATE dbo.privy_coordination SET payload=?,updated=SYSUTCDATETIME() "
                "WHERE kind='hub' AND id=? AND JSON_VALUE(payload,'$.active')='true'",
                json.dumps(
                    {
                        "owner": self.owner,
                        "hub_id": self.hub_id,
                        "home_scope": self.home_scope,
                        "active": True,
                    }
                ),
                self.owner,
            )
            cursor.execute(
                "SELECT 1 FROM dbo.privy_coordination WHERE kind='hub' AND id=? "
                "AND JSON_VALUE(payload,'$.active')='true'",
                self.owner,
            )
            if cursor.fetchone() is None:
                self._fenced = True
                self._stop.set()
                raise StorageError("this Azure SQL hub incarnation was fenced; restart the hub")

    def mark_stopped(self) -> None:
        if self._closed or self._fenced:
            return
        try:
            with self.transaction("privy.leases") as cursor:
                cursor.execute(
                    "UPDATE dbo.privy_coordination SET payload=?,updated=SYSUTCDATETIME() "
                    "WHERE kind='hub' AND id=? AND JSON_VALUE(payload,'$.active')='true'",
                    json.dumps(
                        {
                            "owner": self.owner,
                            "hub_id": self.hub_id,
                            "home_scope": self.home_scope,
                            "active": False,
                        }
                    ),
                    self.owner,
                )
        except Exception:
            log.warning("Could not mark Azure SQL hub incarnation stopped; heartbeat will expire")

    def owner_is_stale(self, owner: str) -> bool:
        if owner == "unresolved-file-import":
            return True
        if not owner or len(owner) > 64:
            return False
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT CASE WHEN JSON_VALUE(payload,'$.active')='false' OR "
                "updated < DATEADD(second,-90,SYSUTCDATETIME()) THEN 1 ELSE 0 END "
                "FROM dbo.privy_coordination WHERE kind='hub' AND id=?",
                owner,
            )
            row = cursor.fetchone()
            return bool(row and row[0])

    def existing(self, kind: str, keys: list[str], *, batch_size: int = 1000) -> set[str]:
        existing = set()
        for start in range(0, len(keys), batch_size):
            batch = keys[start : start + batch_size]
            with self.transaction() as cursor:
                cursor.execute(
                    "SELECT id FROM dbo.privy_records WHERE kind=? AND id IN (SELECT value FROM OPENJSON(?))",
                    kind,
                    json.dumps(batch),
                )
                existing.update(row[0] for row in cursor.fetchall())
        return existing

    def collect(self, batch: int = 500) -> dict:
        if not 1 <= batch <= 1000:
            raise StorageError("GC batch must be between 1 and 1000")
        counts = {}
        with self.transaction("privy.gc") as cursor:
            cursor.execute(
                "DECLARE @r int; EXEC @r=sys.sp_getapplock @Resource='privy.leases', @LockMode='Exclusive', "
                "@LockOwner='Transaction', @LockTimeout=15000; SELECT @r;"
            )
            if cursor.fetchone()[0] < 0:
                raise StorageError("SQL lease cleanup coordination lock timed out")
            # Never collect a running/queued job, its output, or its control records.
            cursor.execute("DROP TABLE IF EXISTS #expired; CREATE TABLE #expired(id nvarchar(400) NOT NULL)")
            cursor.execute(
                "INSERT #expired SELECT TOP (?) r.id FROM dbo.privy_records r WHERE r.kind='job' "
                "AND r.at < DATEDIFF_BIG(second,'1970-01-01',SYSUTCDATETIME())-604800 "
                "AND JSON_VALUE(r.payload,'$.state') IN ('succeeded','failed','timed_out','cancelled','error') "
                "AND NOT EXISTS(SELECT 1 FROM dbo.privy_coordination c CROSS APPLY OPENJSON(c.payload,'$.running_jobs') pin "
                "WHERE (c.kind='lease' AND pin.value=r.id) OR (c.kind='use' AND pin.[key] COLLATE DATABASE_DEFAULT=r.id)) ORDER BY r.at",
                batch,
            )
            cursor.execute(
                "DELETE TOP (?) FROM dbo.privy_output WHERE job_id IN(SELECT id FROM #expired)", batch
            )
            counts["output"] = cursor.rowcount
            cursor.execute("DELETE FROM #expired WHERE id IN(SELECT job_id FROM dbo.privy_output)")
            cursor.execute("DELETE FROM dbo.privy_cursors WHERE job_id IN(SELECT id FROM #expired)")
            cursor.execute("DELETE FROM dbo.privy_cancels WHERE job_id IN(SELECT id FROM #expired)")
            cursor.execute(
                "DELETE FROM dbo.privy_records WHERE kind='job' AND id IN(SELECT id FROM #expired)"
            )
            counts["jobs"] = cursor.rowcount
            cursor.execute(
                "DELETE TOP (?) r FROM dbo.privy_records r WHERE r.kind<>'job' "
                "AND r.at<DATEDIFF_BIG(second,'1970-01-01',SYSUTCDATETIME())-604800 "
                "AND NOT EXISTS(SELECT 1 FROM dbo.privy_records j WHERE j.kind='job' AND j.id=JSON_VALUE(r.payload,'$.job_id')) "
                "AND NOT EXISTS(SELECT 1 FROM OPENJSON(r.payload,'$.job_ids') child JOIN dbo.privy_records j ON j.kind='job' AND j.id=child.value)",
                batch,
            )
            counts["history"] = cursor.rowcount
            cursor.execute(
                "DELETE TOP (?) FROM dbo.privy_coordination WHERE kind='lease' AND updated<DATEADD(day,-7,SYSUTCDATETIME()) "
                "AND JSON_VALUE(payload,'$.state')<>'active' AND JSON_QUERY(payload,'$.running_jobs')='[]'",
                batch,
            )
            counts["leases"] = cursor.rowcount
            cursor.execute(
                "DELETE TOP (?) FROM dbo.privy_coordination WHERE kind='hub' AND updated<DATEADD(day,-7,SYSUTCDATETIME())",
                batch,
            )
            cursor.execute("DROP TABLE #expired")
        return counts

    def flush(self, timeout: float = 10) -> bool:
        deadline = time.monotonic() + timeout
        while self.outbox.pending(1):
            if time.monotonic() >= deadline:
                return False
            try:
                self._drain()
            except Exception:
                self._last_error = "SQL delivery unavailable; journal retained for replay"
                self._wake.set()
                return False
        return True

    def status(self) -> dict:
        result = {
            "mode": "azure_sql",
            "hub_id": self.hub_id,
            "owner": self.owner,
            "error": self._last_error,
            **self.outbox.status(),
        }
        if self._migration_status is not None:
            result["audit_import"] = dict(self._migration_status)
        return result

    def _work(self) -> None:
        maintenance = 0.0
        while not self._stop.is_set():
            try:
                self._drain()
                if time.monotonic() >= maintenance:
                    self.heartbeat()
                    self.collect()
                    maintenance = time.monotonic() + 30
                    self._last_error = None
            except Exception:
                if self._last_error is None:
                    log.error("SQL delivery/maintenance failed; journal retained, no file leasing fallback")
                self._last_error = "SQL delivery unavailable; journal retained for replay"
                self._stop.wait(1)
            self._wake.wait(0.25)
            self._wake.clear()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=35)
            if self._thread.is_alive():
                raise StorageError("SQL delivery thread did not stop; journal remains open")
        if not self.flush():
            log.error("SQL journal has pending writes on shutdown; next start will replay them")
        self.mark_stopped()
        self._closed = True
        self.outbox.close()
        if hasattr(self._connect, "close"):
            self._connect.close()
        self._home_lock.close()


class SqlLeaseLock:
    """Refresh only inside a database lock, then commit changed keyed records."""

    def __init__(self, store, backend: SqlBackend) -> None:
        self.store, self.backend = store, backend
        self._lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self):
        self._lock.acquire()
        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        if depth:
            return self
        try:
            self._local.transaction = None
            transaction = self.backend.transaction("privy.leases")
            cursor = transaction.__enter__()
            self._local.transaction = transaction
            self._local.cursor = cursor
            assert_active = getattr(self.backend, "assert_owner_active", None)
            if assert_active is not None:
                assert_active(cursor)
            cursor.execute(
                "SELECT DATEDIFF_BIG(millisecond,'1970-01-01',SYSUTCDATETIME())/1000.0;"
                "SELECT payload FROM dbo.privy_coordination WHERE kind='config' AND id='targets';"
                "SELECT kind,id,payload FROM dbo.privy_coordination "
                "WHERE kind IN('client','lease','use') ORDER BY ordinal"
            )
            now_row = cursor.fetchone()
            if now_row is None or not cursor.nextset():
                raise StorageError("Azure SQL lease snapshot returned an unexpected result-set count")
            self._local.now = float(now_row[0])
            row = cursor.fetchone()
            targets = {
                "targets": [
                    {"id": t.id, "namespace": t.namespace, "path": t.path}
                    for t in self.store.registry.snapshot().targets
                ]
            }
            if row is None or json.loads(row[0]) != targets:
                raise StorageError(
                    "Local target configuration changed or differs from shared SQL configuration"
                )
            if not cursor.nextset():
                raise StorageError("Azure SQL lease snapshot returned an unexpected result-set count")
            state = {"client": {}, "lease": {}, "use": {}}
            for kind, key, raw in cursor.fetchall():
                state[kind][key] = json.loads(raw)
            if cursor.nextset():
                raise StorageError("Azure SQL lease snapshot returned an unexpected result-set count")
            self.store._clients, self.store._leases, self.store._uses = (
                state["client"],
                state["lease"],
                state["use"],
            )
            self._local.before = copy.deepcopy(state)
            return self
        except BaseException as exc:
            try:
                if getattr(self._local, "transaction", None) is not None:
                    self._local.transaction.__exit__(type(exc), exc, exc.__traceback__)
            finally:
                self._local.transaction = None
                self._local.depth = 0
                self._lock.release()
            raise

    def now(self) -> float:
        return self._local.now

    def __exit__(self, typ, value, trace):
        self._local.depth -= 1
        try:
            if self._local.depth:
                return False
            if typ is None:
                cursor = self._local.cursor
                current = {
                    "client": self.store._clients,
                    "lease": self.store._leases,
                    "use": self.store._uses,
                }
                for kind, records in current.items():
                    previous = self._local.before[kind]
                    for key in previous.keys() - records.keys():
                        cursor.execute("DELETE dbo.privy_coordination WHERE kind=? AND id=?", kind, key)
                    for key, payload in records.items():
                        if key not in previous:
                            cursor.execute(
                                "INSERT dbo.privy_coordination(kind,id,payload) VALUES(?,?,?)",
                                kind,
                                key,
                                json.dumps(payload),
                            )
                        elif payload != previous[key]:
                            cursor.execute(
                                "UPDATE dbo.privy_coordination SET payload=?,updated=SYSUTCDATETIME() WHERE kind=? AND id=?",
                                json.dumps(payload),
                                kind,
                                key,
                            )
            return self._local.transaction.__exit__(typ, value, trace)
        except BaseException as exc:
            if typ is None:
                self._local.transaction.__exit__(type(exc), exc, exc.__traceback__)
            raise
        finally:
            self._lock.release()
