"""Persistent, cooperative reservations on regex-defined groups of hub targets."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import logging
import math
import re
import string
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from privy.hub.home import _write_private
from privy.hub.jobs import AuditLog, utc_iso
from privy.hub.purpose import normalize_purpose
from privy.hub.registry import ONLINE, TargetLookupError, TargetRegistry

DEFAULT_CONFIG = """\
version: 1
defaults:
  ttl: 30m
  max_ttl: 4h
  manual_max_ttl: 365d
  cooldown: 15m
  max_leases_per_client: 4
  client_idle_expiry: 24h
groups:
  - name: devbox-stack
    match: '^(?P<relay>[^/]+)/(?P<group>.+)_(?P<layer>windows|wsl|devcontainer)$'
    key: '{relay}/{group}'
    layer: '{layer}'
    require_online: [windows]
"""

log = logging.getLogger("privy.hub")


class LeaseError(Exception):
    def __init__(
        self, message: str, *, code: str = "invalid_argument", status: int = 400, details: Any = None
    ):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


def duration(value: Any) -> float:
    if type(value) in (int, float):
        seconds = float(value)
        if not math.isfinite(seconds) or not 0 < seconds <= 365 * 86400:
            raise ValueError("duration seconds must be positive and at most 365 days")
        return seconds
    if not isinstance(value, str):
        raise ValueError("duration must be a positive duration string, e.g. 30m or 4h")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h|d)?", value)
    if match is None:
        raise ValueError("duration must be a positive duration string, e.g. 30m or 4h")
    seconds = float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2] or "s"]
    if not 0 < seconds <= 365 * 86400:
        raise ValueError("duration must be positive and at most 365 days")
    return seconds


def effective_lease_config(config: Any) -> dict[str, Any]:
    """Normalize lease policy representations to their effective values."""
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
        raise ValueError("expected version: 1")
    defaults = config.get("defaults")
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be a mapping")
    groups = config.get("groups")
    if not isinstance(groups, list):
        raise ValueError("groups must be a list")
    ttl = duration(defaults["ttl"])
    max_ttl = duration(defaults["max_ttl"])
    manual_max_ttl = duration(defaults.get("manual_max_ttl", "365d"))
    cooldown = duration(defaults["cooldown"])
    client_idle_expiry = duration(defaults["client_idle_expiry"])
    client_limit = defaults["max_leases_per_client"]
    if type(client_limit) is not int or client_limit < 1:
        raise ValueError("max_leases_per_client must be a positive integer")
    if ttl > max_ttl or max_ttl > 4 * 3600:
        raise ValueError("ttl must not exceed max_ttl and max_ttl must not exceed 4h")
    if manual_max_ttl < ttl:
        raise ValueError("manual_max_ttl must not be less than the default ttl")
    return {
        "version": 1,
        "defaults": {
            "ttl_s": ttl,
            "max_ttl_s": max_ttl,
            "manual_max_ttl_s": manual_max_ttl,
            "cooldown_s": cooldown,
            "max_leases_per_client": client_limit,
            "client_idle_expiry_s": client_idle_expiry,
        },
        "groups": copy.deepcopy(groups),
    }


def _labels(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) for k, v in value.items()
    ):
        raise LeaseError("labels must be an object of non-empty string keys and string values")
    if len(value) > 64 or any(len(k) > 240 or len(v) > 1024 for k, v in value.items()):
        raise LeaseError("labels support at most 64 entries, 240-character keys and 1024-character values")
    return dict(value)


class LeaseStore:
    """One lock covers admission, expiry, usage and disk replacement.

    Never calls JobManager or takes a job condition: job callbacks may call this
    store while holding those locks.
    """

    def __init__(
        self,
        registry: TargetRegistry,
        *,
        home: Path | None = None,
        audit: AuditLog | None = None,
        clock: Callable[[], float] = time.time,
        sql_backend: Any = None,
    ) -> None:
        self.registry, self._clock = registry, clock
        self.audit = audit or AuditLog(None)
        self.config_path = home / "leases.yaml" if home else None
        self.state_path = home / "leases-state.json" if home else None
        self._lock = threading.RLock()
        self._clients: dict[str, dict[str, Any]] = {}
        self._leases: dict[str, dict[str, Any]] = {}
        self._uses: dict[str, dict[str, Any]] = {}
        self._rules: list[tuple[dict[str, Any], re.Pattern[str]]] = []
        self.sql_backend = sql_backend
        stored_config = sql_backend.get_config("leases") if sql_backend is not None else None
        if stored_config is not None:
            stored_config = copy.deepcopy(stored_config)
            defaults = stored_config["defaults"]
            for name in ("ttl", "max_ttl", "manual_max_ttl", "cooldown", "client_idle_expiry"):
                if name + "_s" in defaults:
                    defaults[name] = defaults.pop(name + "_s")
        if self.config_path is not None and not self.config_path.exists():
            _write_private(
                self.config_path,
                yaml.safe_dump(stored_config, sort_keys=False) if stored_config else DEFAULT_CONFIG,
            )
        try:
            text = (
                (self.config_path.read_text(encoding="utf-8-sig") if self.config_path else DEFAULT_CONFIG)
                if stored_config is None
                else ""
            )
            self.config = stored_config if stored_config is not None else yaml.safe_load(text)
            self._validate_config()
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError, re.error) as exc:
            raise ValueError(f"Invalid leases.yaml ({self.config_path or 'defaults'}): {exc}") from exc
        if self.config_path is not None:
            try:
                self._membership()
            except LeaseError as exc:
                raise ValueError(f"Invalid leases.yaml ({self.config_path}): {exc}") from exc
        if sql_backend is not None:
            from privy.hub.sql_store import SqlLeaseLock

            import_path = self.state_path
            self.state_path = None
            sql_backend.check_config("leases", self.effective_config)
            sql_backend.check_config(
                "targets",
                {
                    "targets": [
                        {"id": t.id, "namespace": t.namespace, "path": t.path}
                        for t in self.registry.snapshot().targets
                    ]
                },
            )
            self._lock = SqlLeaseLock(self, sql_backend)
            self._clock = self._lock.now
            if import_path is not None and import_path.exists():
                self._import_sql_state(import_path)
            with self._lock:
                self._validate_state()
                self._sweep()
            return
        if self.state_path is not None and self.state_path.exists():
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or state.get("version") != 1:
                    raise ValueError("expected version: 1")
                for key in ("clients", "leases", "last_use"):
                    if not isinstance(state.get(key), dict):
                        raise ValueError(f"{key} must be an object")
                self._clients, self._leases, self._uses = state["clients"], state["leases"], state["last_use"]
                self._validate_state()
                cleared_pins = False
                for lease in self._leases.values():
                    # Hub jobs are not restored; stale pins must not reserve a group forever.
                    if lease["running_jobs"]:
                        cleared_pins = True
                        lease["running_jobs"] = []
                        if lease["state"] == "active":
                            lease["expires_ts"] = max(lease["expires_ts"], self._clock() + lease["ttl_s"])
                self._sweep()
                if cleared_pins:
                    self._save()
            except (OSError, ValueError, TypeError, KeyError, LeaseError) as exc:
                raise ValueError(f"Invalid leases-state.json ({self.state_path}): {exc}") from exc
        elif self.state_path is not None:
            self._restore_recent_use()
            self._save()

    def _restore_recent_use(self) -> None:
        path = self.audit.path
        if path is None or not path.exists():
            return
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - 8 * 1024 * 1024))
                raw = stream.read()
        except OSError as exc:
            log.warning("Cannot restore lease cooldowns from %s: %s", path, exc)
            raise ValueError(f"Cannot restore lease cooldowns from {path}: {exc}") from exc
        if size > 8 * 1024 * 1024:
            raw = raw.split(b"\n", 1)[-1]
        _, membership = self._membership()
        for line in raw.splitlines():
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("event") not in {
                    "job.start",
                    "job.finish",
                    "terminal.open",
                    "terminal.close",
                    "terminal.finish",
                }:
                    continue
                moment = dt.datetime.fromisoformat(record["ts"].replace("Z", "+00:00")).timestamp()
                key = membership.get(record.get("target"), (None, None))[0]
                if key is None or moment < self._clock() - self.cooldown:
                    continue
                previous = self._uses.get(key, {}).get("at", 0)
                if moment > previous:
                    self._uses[key] = {"at": moment, "by": {"origin": record.get("origin") or "ui"}}
            except (ValueError, TypeError, KeyError, AttributeError):
                log.warning("Ignoring a malformed audit record while restoring lease cooldowns from %s", path)

    def _import_sql_state(self, path: Path) -> None:
        """Merge a new home's file state once, rejecting conflicting allocations."""
        from privy.hub.sql_store import StorageError

        raw = path.read_bytes()
        try:
            state = json.loads(raw)
            if not isinstance(state, dict) or state.get("version") != 1:
                raise ValueError("expected version: 1")
            self._clients = state["clients"]
            self._leases = state["leases"]
            self._uses = state["last_use"]
            self._validate_state()
            imported = copy.deepcopy((self._clients, self._leases, self._uses))
        except (ValueError, KeyError, TypeError, LeaseError) as exc:
            raise ValueError(f"Invalid SQL import state ({path}): {exc}") from exc
        digest = hashlib.sha256(raw).hexdigest()
        with self._lock:
            cursor = self._lock._local.cursor
            cursor.execute("SELECT payload FROM dbo.privy_coordination WHERE kind='import' AND id=?", digest)
            if cursor.fetchone():
                self._retire_import(path, digest)
                return
            aliases = {}
            for key, payload in imported[0].items():
                active_imports = any(
                    lease_id not in self._leases
                    and lease["client_id"] == key
                    and (lease["state"] == "active" or lease["running_jobs"])
                    for lease_id, lease in imported[1].items()
                )
                if (
                    key in self._clients
                    and self._clients[key].get("session_id") != payload.get("session_id")
                    and active_imports
                ):
                    raise StorageError("Local active lease client identity differs from shared SQL identity")
                matches = [
                    client_id
                    for client_id, client in self._clients.items()
                    if payload.get("session_id") and client.get("session_id") == payload["session_id"]
                ]
                if len(matches) > 1:
                    raise StorageError("Shared SQL contains ambiguous duplicate client sessions")
                if matches and matches[0] != key:
                    if active_imports:
                        raise StorageError(
                            "Local active lease has a different shared session owner; resolve ownership before importing"
                        )
                    aliases[key] = matches[0]
            new_leases = []
            for current, source in zip((self._clients, self._leases, self._uses), imported):
                for key, payload in source.items():
                    if current is self._clients and key in aliases:
                        continue
                    if current is self._leases and key not in current:
                        payload["client_id"] = aliases.get(payload["client_id"], payload["client_id"])
                        new_leases.append(payload)
                    if key in current:
                        if current is self._uses and payload["at"] > current[key]["at"]:
                            current[key] = payload
                        continue
                    current[key] = payload
            self._validate_state()
            # File-owned pins are unresolved, not evidence that remote execution stopped.
            for lease in new_leases:
                if lease["running_jobs"]:
                    if lease["state"] != "active":
                        raise StorageError(
                            "File import has released/expired pinned jobs; verify remote completion before importing"
                        )
                    lease.setdefault("job_owners", {}).update(
                        (job, "unresolved-file-import") for job in lease["running_jobs"]
                    )
            cursor.execute(
                "INSERT dbo.privy_coordination(kind,id,payload) VALUES('import',?,?)",
                digest,
                json.dumps({"version": 1}),
            )
        self._retire_import(path, digest)

    @staticmethod
    def _retire_import(path: Path, digest: str) -> None:
        from privy.hub.sql_store import StorageError

        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise StorageError("Legacy lease state changed before retirement; preserve it and retry startup")
        snapshot = path.with_name(path.name + ".imported-snapshot")
        if snapshot.exists():
            snapshot = path.with_name(path.name + f".{digest}.imported-snapshot")
        path.replace(snapshot)

    def _validate_config(self) -> None:
        config = self.config
        if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
            raise ValueError("expected version: 1")
        self.effective_config = effective_lease_config(config)
        defaults = self.effective_config["defaults"]
        self.default_ttl = defaults["ttl_s"]
        self.max_ttl = defaults["max_ttl_s"]
        self.manual_max_ttl = defaults["manual_max_ttl_s"]
        self.cooldown = defaults["cooldown_s"]
        self.client_idle_expiry = defaults["client_idle_expiry_s"]
        self.client_limit = defaults["max_leases_per_client"]
        names: set[str] = set()
        for rule in config["groups"]:
            if not isinstance(rule, dict) or not isinstance(rule.get("name"), str) or not rule["name"]:
                raise ValueError("every group rule needs a non-empty name")
            if rule["name"] in names:
                raise ValueError(f"duplicate rule name {rule['name']}")
            names.add(rule["name"])
            regex = rule["match"]
            if not isinstance(regex, str) or not regex.startswith("^") or not regex.endswith("$"):
                raise ValueError(f"{rule['name']}: match must be anchored with ^ and $")
            compiled = re.compile(regex)
            for field in ("key", "layer"):
                template = rule[field]
                if not isinstance(template, str) or not template:
                    raise ValueError(f"{rule['name']}: {field} must be a non-empty format string")
                for _, name, spec, conversion in string.Formatter().parse(template):
                    if name is not None and (name not in compiled.groupindex or spec or conversion):
                        raise ValueError(f"{rule['name']}: {field} must reference only named regex groups")
            required = rule["require_online"]
            if not isinstance(required, list) or not all(
                isinstance(layer, str) and layer for layer in required
            ):
                raise ValueError("require_online must be a list of non-empty layer names")
            self._rules.append((rule, compiled))

    def _validate_state(self) -> None:
        def timestamp(value: Any) -> bool:
            return type(value) in (float, int) and math.isfinite(value) and value >= 0

        for client_id, client in self._clients.items():
            if not isinstance(client, dict) or client.get("client_id") != client_id:
                raise ValueError("invalid client record")
            if not all(timestamp(client.get(k)) for k in ("registered_ts", "last_seen_ts")):
                raise ValueError("invalid client timestamps")
            if not isinstance(client.get("name"), str) or not client["name"]:
                raise ValueError("invalid client name")
            _labels(client.get("labels"))
        active_groups: set[str] = set()
        for lease_id, lease in self._leases.items():
            if not isinstance(lease, dict) or lease.get("lease_id") != lease_id:
                raise ValueError("invalid lease record")
            if lease.get("state") not in ("active", "expired", "released"):
                raise ValueError("invalid lease state")
            if not isinstance(lease.get("group"), str) or not isinstance(lease.get("client_id"), str):
                raise ValueError("invalid lease identity")
            if lease["state"] == "active":
                if lease["group"] in active_groups or lease["client_id"] not in self._clients:
                    raise ValueError("duplicate active group or missing active client")
                active_groups.add(lease["group"])
            if not all(timestamp(lease.get(k)) for k in ("acquired_ts", "renewed_ts", "expires_ts")):
                raise ValueError("invalid lease timestamps")
            if type(lease.get("manual", False)) is not bool:
                raise ValueError("invalid manual lease marker")
            maximum = self.manual_max_ttl if lease.get("manual") else self.max_ttl
            if not timestamp(lease.get("ttl_s")) or not 0 < lease["ttl_s"] <= maximum:
                raise ValueError("invalid lease ttl")
            if not isinstance(lease.get("members"), dict) or not isinstance(lease.get("events"), list):
                raise ValueError("invalid lease members or events")
            if not isinstance(lease.get("running_jobs"), list):
                raise ValueError("invalid lease pins")
            _labels(lease.get("labels"))
        for use in self._uses.values():
            if (
                not isinstance(use, dict)
                or not timestamp(use.get("at"))
                or not isinstance(use.get("by"), dict)
            ):
                raise ValueError("invalid last-use record")

    def _save(self) -> None:
        if self.state_path is None:
            return
        temporary = self.state_path.with_suffix(".tmp")
        _write_private(
            temporary,
            json.dumps(
                {
                    "version": 1,
                    "clients": self._clients,
                    "leases": self._leases,
                    "last_use": self._uses,
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
        )
        temporary.replace(self.state_path)

    def _event(self, lease: dict[str, Any], event: str, **details: Any) -> None:
        entry = {"at": utc_iso(self._clock()), "event": event, **details}
        lease["events"].append(entry)
        del lease["events"][:-1000]
        self.audit.record(
            f"lease.{event}",
            lease_id=lease["lease_id"],
            client_id=lease["client_id"],
            group=lease["group"],
            **details,
        )

    def _sweep(self) -> None:
        now, changed = self._clock(), False
        for lease in self._leases.values():
            if lease["state"] == "active" and not lease["running_jobs"] and lease["expires_ts"] <= now:
                lease["state"] = "expired"
                lease["expired_ts"] = lease["expires_ts"]
                self._event(lease, "expired", expires_at=utc_iso(lease["expires_ts"]))
                changed = True
        active = {item["client_id"] for item in self._leases.values() if item["state"] == "active"}
        for client_id, client in list(self._clients.items()):
            if client_id not in active and now - client["last_seen_ts"] >= self.client_idle_expiry:
                del self._clients[client_id]
                self.audit.record("client.expired", client_id=client_id)
                changed = True
        if changed:
            self._save()

    def _client(self, client_id: str) -> dict[str, Any]:
        client = self._clients.get(client_id)
        if client is None:
            raise LeaseError(f"unknown client {client_id!r}", status=404, code="unknown_client")
        return client

    def _lease(self, lease_id: str) -> dict[str, Any]:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise LeaseError(f"unknown lease {lease_id!r}", status=404, code="unknown_lease")
        return lease

    def _client_view(
        self,
        client: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        leases = snapshot["leases"] if snapshot is not None else self._leases
        now = snapshot["now"] if snapshot is not None else None
        return {k: copy.deepcopy(v) for k, v in client.items() if not k.endswith("_ts")} | {
            "registered_at": utc_iso(client["registered_ts"]),
            "last_seen_at": utc_iso(client["last_seen_ts"]),
            "leases": [
                item["lease_id"]
                for item in leases.values()
                if item["client_id"] == client["client_id"]
                and item["state"] == "active"
                and (snapshot is None or item["running_jobs"] or item["expires_ts"] > now)
            ],
        }

    def _view(
        self,
        lease: dict[str, Any],
        *,
        events: bool = True,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        view = {
            k: copy.deepcopy(v)
            for k, v in lease.items()
            if not k.endswith("_ts") and k != "client_record" and (events or k != "events")
        }
        uses = snapshot["uses"] if snapshot is not None else self._uses
        use = uses.get(lease["group"], {})
        last_used = lease.get("last_used_ts", 0)
        if use.get("by", {}).get("lease_id") == lease["lease_id"]:
            last_used = max(last_used, use.get("at", 0))
        if "last_used_ts" not in lease:
            for event in lease.get("events", []):
                if event["event"] in ("pinned", "unpinned"):
                    last_used = max(
                        last_used, dt.datetime.fromisoformat(event["at"].replace("Z", "+00:00")).timestamp()
                    )
        view.update(
            last_use_known="last_used_ts" in lease or last_used > 0,
            last_used_at=utc_iso(last_used) if last_used >= lease["acquired_ts"] else None,
            acquired_at=utc_iso(lease["acquired_ts"]),
            expires_at=utc_iso(lease["expires_ts"]),
            renewed_at=utc_iso(lease["renewed_ts"]),
            jobs_url=f"/v1/jobs?lease_id={lease['lease_id']}",
        )
        clients = snapshot["clients"] if snapshot is not None else self._clients
        client = clients.get(lease["client_id"])
        view["holder"] = (
            self._client_view(client, snapshot=snapshot)
            if client
            else {"client_id": lease["client_id"], "name": lease["holder_name"]}
        )
        return view

    def register_client(
        self,
        name: str,
        kind: str | None = None,
        host: str | None = None,
        session_id: str | None = None,
        labels: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise LeaseError("name must be a non-empty string")
        labels = _labels(labels)
        with self._lock:
            self._sweep()
            now = self._clock()
            existing = next(
                (c for c in self._clients.values() if session_id and c["session_id"] == session_id), None
            )
            if existing:
                existing["last_seen_ts"] = now
                self._save()
                return {**self._client_view(existing), "created": False}
            client_id = "cl-" + uuid.uuid4().hex
            client = {
                "client_id": client_id,
                "name": name.strip(),
                "kind": kind or "script",
                "host": host,
                "session_id": session_id,
                "labels": labels,
                "registered_ts": now,
                "last_seen_ts": now,
            }
            self._clients[client_id] = client
            self.audit.record("client.registered", client_id=client_id, name=name, kind=client["kind"])
            self._save()
            return {**self._client_view(client), "created": True}

    def list_clients(self, active: bool = False) -> list[dict[str, Any]]:
        if getattr(self.sql_backend, "read_model", None) is not None:
            with self._lock.read_snapshot() as snapshot:
                views = [self._client_view(c, snapshot=snapshot) for c in snapshot["clients"].values()]
                return [v for v in views if v["leases"]] if active else views
        with self._lock:
            self._sweep()
            views = [self._client_view(c) for c in self._clients.values()]
            return [v for v in views if v["leases"]] if active else views

    def get_client(self, client_id: str) -> dict[str, Any]:
        if getattr(self.sql_backend, "read_model", None) is not None:
            with self._lock.read_snapshot() as snapshot:
                client = snapshot["clients"].get(client_id)
                if client is None:
                    raise LeaseError(f"unknown client {client_id!r}", status=404, code="unknown_client")
                return self._client_view(client, snapshot=snapshot)
        with self._lock:
            self._sweep()
            return self._client_view(self._client(client_id))

    def delete_client(self, client_id: str) -> dict[str, Any]:
        with self._lock:
            self._sweep()
            self._client(client_id)
            for lease in list(self._leases.values()):
                if lease["client_id"] == client_id and lease["state"] != "released":
                    self.release(lease["lease_id"], client_id)
            del self._clients[client_id]
            self.audit.record("client.unregistered", client_id=client_id)
            self._save()
            return {"client_id": client_id, "unregistered": True}

    def _membership(self) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, str]]]:
        groups: dict[str, dict[str, Any]] = {}
        targets: dict[str, tuple[str, str]] = {}
        for target in self.registry.snapshot().targets:
            key, layer, required = target.id, "target", ["target"]
            for rule, regex in self._rules:
                match = regex.fullmatch(target.id)
                if match:
                    values = match.groupdict()
                    key, layer = rule["key"].format(**values), rule["layer"].format(**values)
                    required = rule["require_online"]
                    break
            group = groups.setdefault(key, {"group": key, "members": {}, "require_online": required})
            if not key or not layer or layer in group["members"] or group["require_online"] != required:
                raise LeaseError(
                    f"conflicting group rules for {target.id}", code="invalid_lease_config", status=500
                )
            group["members"][layer] = target.id
            targets[target.id] = key, layer
        return groups, targets

    def _active(self, group: str) -> dict[str, Any] | None:
        return next(
            (item for item in self._leases.values() if item["group"] == group and item["state"] == "active"),
            None,
        )

    def _latest(self, group: str) -> dict[str, Any] | None:
        return next((item for item in reversed(self._leases.values()) if item["group"] == group), None)

    def _group_view(
        self,
        group: dict[str, Any],
        *,
        probe: bool = False,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = group["group"]
        leases = snapshot["leases"] if snapshot is not None else self._leases
        uses = snapshot["uses"] if snapshot is not None else self._uses
        now = snapshot["now"] if snapshot is not None else self._clock()
        lease = next(
            (
                item
                for item in leases.values()
                if item["group"] == key
                and item["state"] == "active"
                and (item["running_jobs"] or item["expires_ts"] > now)
            ),
            None,
        )
        use = uses.get(key, {})
        allocation = next(
            (item for item in reversed(leases.values()) if item["group"] == key),
            None,
        )
        expired_at = 0
        if allocation:
            if allocation["state"] == "expired":
                expired_at = allocation.get("expired_ts", 0)
            elif (
                allocation["state"] == "active"
                and not allocation["running_jobs"]
                and allocation["expires_ts"] <= now
            ):
                expired_at = allocation["expires_ts"]
        released = allocation.get("released_ts", 0) if allocation and allocation["state"] == "released" else 0
        last = max(use.get("at", 0), expired_at)
        leasable = last + self.cooldown if last > released else 0
        online = True
        for layer in group["require_online"]:
            target_id = group["members"].get(layer)
            if target_id is None:
                online = False
                break
            target = self.registry.resolve(target_id)
            status = self.registry.probe(target) if probe else self.registry.cached(target)
            if status is None or status.status != ONLINE:
                online = False
                break
        state = (
            "leased" if lease else "unavailable" if not online else "cooling" if leasable > now else "free"
        )
        if self.sql_backend is not None and use.get("running_jobs") and not lease:
            state = "unavailable"
        return {
            **copy.deepcopy(group),
            "state": state,
            "lease": self._view(lease, events=False, snapshot=snapshot) if lease else None,
            "leasable_at": utc_iso(lease["expires_ts"] if lease else leasable)
            if (lease or leasable)
            else None,
            "last_used_at": utc_iso(use.get("at")),
            "last_used_by": use.get("by"),
            "retry_after_s": max(0, int((lease["expires_ts"] if lease else leasable) - now) + 1),
        }

    def groups(self, *, probe: bool = False) -> list[dict[str, Any]]:
        read_snapshot = getattr(self._lock, "read_snapshot", None)
        if callable(read_snapshot):
            with read_snapshot() as snapshot:
                groups, _ = self._membership()
                return [self._group_view(g, probe=probe, snapshot=snapshot) for g in groups.values()]
        with self._lock:
            self._sweep()
            groups, _ = self._membership()
            return [self._group_view(g, probe=probe) for g in groups.values()]

    def target_summary(self, target_id: str) -> dict[str, Any]:
        with self._lock:
            self._sweep()
            groups, targets = self._membership()
            key, layer = targets[target_id]
            view = self._group_view(groups[key])
            return {
                "group": key,
                "layer": layer,
                "lease": view["lease"],
                "lease_state": view["state"],
                "leasable_at": view["leasable_at"],
            }

    def target_summaries(self, target_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        requested = list(dict.fromkeys(target_ids))
        if not requested:
            return {}

        def summarize(snapshot: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
            groups, targets = self._membership()
            group_views: dict[str, dict[str, Any]] = {}
            summaries: dict[str, dict[str, Any]] = {}
            for target_id in requested:
                key, layer = targets[target_id]
                if key not in group_views:
                    group_views[key] = self._group_view(groups[key], snapshot=snapshot)
                view = group_views[key]
                summaries[target_id] = {
                    "group": key,
                    "layer": layer,
                    "lease": copy.deepcopy(view["lease"]),
                    "lease_state": view["state"],
                    "leasable_at": view["leasable_at"],
                }
            return summaries

        read_snapshot = getattr(self._lock, "read_snapshot", None)
        if callable(read_snapshot):
            with read_snapshot() as snapshot:
                return summarize(snapshot)
        with self._lock:
            self._sweep()
            return summarize()

    def _ttl(self, ttl: str | None, *, manual: bool = False) -> tuple[str, float]:
        ttl = self.config["defaults"]["ttl"] if ttl is None else ttl
        try:
            seconds = duration(ttl)
        except ValueError as exc:
            raise LeaseError(str(exc)) from exc
        maximum = self.manual_max_ttl if manual else self.max_ttl
        if seconds > maximum:
            raise LeaseError(f"ttl must not exceed {maximum:g} seconds")
        return ttl, seconds

    def acquire(
        self,
        client_id: str,
        target: str | None = None,
        labels: Any = None,
        purpose: str | None = None,
        ttl: str | None = None,
        *,
        manual: bool = False,
    ) -> dict[str, Any]:
        if type(manual) is not bool:
            raise LeaseError("manual must be a boolean")
        ttl, seconds = self._ttl(ttl, manual=manual)
        labels = _labels(labels)
        try:
            purpose = normalize_purpose(purpose)
        except ValueError as exc:
            raise LeaseError(str(exc)) from exc
        if self.sql_backend is not None:
            groups, _ = self._membership()
            required = {
                group["members"][layer]
                for group in groups.values()
                for layer in group["require_online"]
                if layer in group["members"]
            }
            self.registry.probe_many([self.registry.resolve(target_id) for target_id in required])
        with self._lock:
            self._sweep()
            client = self._client(client_id)
            groups, targets = self._membership()
            if target is not None:
                if target in groups:
                    selected = groups[target]
                else:
                    short = [g for k, g in groups.items() if k.rsplit("/", 1)[-1] == target]
                    if len(short) == 1:
                        selected = short[0]
                    else:
                        try:
                            resolved = self.registry.resolve(target)
                            selected = groups[targets[resolved.id][0]]
                        except TargetLookupError as exc:
                            raise LeaseError(str(exc), status=404, code="unknown_target") from exc
                current = self._active(selected["group"])
                if current and current["client_id"] == client_id:
                    self._renew(current)
                    return {**self._view(current), "created": False}
            else:
                selected = None
            if (
                sum(
                    item["client_id"] == client_id and item["state"] == "active"
                    for item in self._leases.values()
                )
                >= self.client_limit
            ):
                raise LeaseError(
                    "client holds the maximum number of leases", status=409, code="client_lease_limit"
                )
            candidates = [selected] if selected else list(groups.values())
            views = []
            for candidate in candidates:
                view = self._group_view(candidate, probe=self.sql_backend is None)
                views.append(view)
                previous = self._latest(candidate["group"])
                reclaim = previous and previous["state"] == "expired" and previous["client_id"] == client_id
                if view["state"] == "free" or (view["state"] == "cooling" and reclaim):
                    now = self._clock()
                    lease_id = "ls-" + uuid.uuid4().hex
                    lease = {
                        "lease_id": lease_id,
                        "client_id": client_id,
                        "holder_name": client["name"],
                        "client_record": copy.deepcopy(client),
                        "group": candidate["group"],
                        "members": copy.deepcopy(candidate["members"]),
                        "labels": labels,
                        "purpose": purpose,
                        "state": "active",
                        "ttl": ttl,
                        "ttl_s": seconds,
                        "manual": manual,
                        "acquired_ts": now,
                        "last_used_ts": 0,
                        "renewed_ts": now,
                        "expires_ts": now + seconds,
                        "running_jobs": [],
                        "events": [],
                    }
                    self._leases[lease_id] = lease
                    client["last_seen_ts"] = now
                    self._event(lease, "acquired")
                    self._save()
                    return {**self._view(lease), "created": True}
            if target is None:
                retry = min(
                    (v["retry_after_s"] for v in views if v["state"] in ("leased", "cooling")), default=15
                )
                raise LeaseError(
                    "no lease group is available",
                    status=409,
                    code="none_available",
                    details={"groups": views, "retry_after_s": retry},
                )
            view = views[0]
            if view["state"] == "leased":
                holder = view["lease"]
                details = {
                    "holder": holder["holder"],
                    "lease_id": holder["lease_id"],
                    "expires_at": holder["expires_at"],
                    "retry_after_s": view["retry_after_s"],
                }
                code = "group_leased"
            elif view["state"] == "cooling":
                details, code = view, "cooling_down"
            else:
                details, code = view, "unavailable"
            raise LeaseError(
                f"group {view['group']} is {view['state']}", status=409, code=code, details=details
            )

    def list_leases(
        self,
        client_id: str | None = None,
        group: str | None = None,
        label: Sequence[str] | str | None = None,
        state: str = "active",
    ) -> list[dict[str, Any]]:
        if state not in ("active", "expired", "released", "all"):
            raise LeaseError("state must be active, expired, released or all")
        filters = [label] if isinstance(label, str) else list(label or [])
        if any("=" not in item or not item.split("=", 1)[0] for item in filters):
            raise LeaseError("label filters must be key=value")
        if getattr(self.sql_backend, "read_model", None) is not None:
            with self._lock.read_snapshot() as snapshot:
                self._expire_read_snapshot(snapshot)
                return [
                    self._view(item, snapshot=snapshot)
                    for item in snapshot["leases"].values()
                    if (not client_id or item["client_id"] == client_id)
                    and (not group or item["group"] == group)
                    and (state == "all" or item["state"] == state)
                    and all(item["labels"].get(k) == v for k, v in (f.split("=", 1) for f in filters))
                ]
        with self._lock:
            self._sweep()
            return [
                self._view(item)
                for item in self._leases.values()
                if (not client_id or item["client_id"] == client_id)
                and (not group or item["group"] == group)
                and (state == "all" or item["state"] == state)
                and all(item["labels"].get(k) == v for k, v in (f.split("=", 1) for f in filters))
            ]

    def get_lease(self, lease_id: str) -> dict[str, Any]:
        if getattr(self.sql_backend, "read_model", None) is not None:
            with self._lock.read_snapshot() as snapshot:
                self._expire_read_snapshot(snapshot)
                lease = snapshot["leases"].get(lease_id)
                if lease is None:
                    raise LeaseError(f"unknown lease {lease_id!r}", status=404, code="unknown_lease")
                return self._view(lease, snapshot=snapshot)
        with self._lock:
            self._sweep()
            return self._view(self._lease(lease_id))

    @staticmethod
    def _expire_read_snapshot(snapshot):
        for lease in snapshot["leases"].values():
            if (
                lease["state"] == "active"
                and not lease["running_jobs"]
                and lease["expires_ts"] <= snapshot["now"]
            ):
                lease["state"] = "expired"
                lease["expired_ts"] = lease["expires_ts"]

    def _renew(self, lease: dict[str, Any]) -> None:
        if lease["state"] == "released":
            raise LeaseError("lease was released", status=409, code="lease_released")
        current = self._latest(lease["group"])
        if current is not None and current["lease_id"] != lease["lease_id"]:
            raise LeaseError(
                "this lease was superseded by a newer group allocation",
                status=409,
                code="lease_lost",
                details={"lease_id": current["lease_id"], "holder": self._view(current)["holder"]},
            )
        revived = lease["state"] == "expired"
        if (
            revived
            and sum(
                item["client_id"] == lease["client_id"] and item["state"] == "active"
                for item in self._leases.values()
            )
            >= self.client_limit
        ):
            raise LeaseError(
                "client holds the maximum number of leases", status=409, code="client_lease_limit"
            )
        lease["state"] = "active"
        lease["renewed_ts"] = self._clock()
        lease["expires_ts"] = self._clock() + lease["ttl_s"]
        client = self._clients.get(lease["client_id"])
        if client is None:
            client = copy.deepcopy(lease["client_record"])
            self._clients[lease["client_id"]] = client
        if client:
            client["last_seen_ts"] = self._clock()
        self._event(lease, "revived" if revived else "renewed")
        self._save()

    def renew(
        self, lease_id: str, client_id: str, ttl: str | None = None, *, manual: bool = False
    ) -> dict[str, Any]:
        with self._lock:
            self._sweep()
            lease = self._lease(lease_id)
            if lease["client_id"] != client_id:
                raise LeaseError("only the holder may renew this lease", status=403, code="not_lease_holder")
            parsed = self._ttl(ttl, manual=manual and lease.get("manual", False)) if ttl is not None else None
            if parsed:
                lease["ttl"], lease["ttl_s"] = parsed
            self._renew(lease)
            return self._view(lease)

    def release(
        self,
        lease_id: str,
        client_id: str | None = None,
        force: bool = False,
        *,
        require_unpinned: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._sweep()
            lease = self._lease(lease_id)
            if not force and lease["client_id"] != client_id:
                raise LeaseError(
                    "only the holder may release this lease", status=403, code="not_lease_holder"
                )
            if require_unpinned:
                running_jobs = lease.get("running_jobs")
                job_owners = lease.get("job_owners", {})
                if (
                    not isinstance(running_jobs, list)
                    or not isinstance(job_owners, dict)
                    or running_jobs
                    or job_owners
                ):
                    raise LeaseError(
                        "lease has running or unresolved job pins; refresh and retry after they finish",
                        status=409,
                        code="lease_pinned",
                        details={
                            "running_jobs": running_jobs if isinstance(running_jobs, list) else None,
                            "job_owners": sorted(job_owners) if isinstance(job_owners, dict) else None,
                        },
                    )
            if self.sql_backend is not None and lease["running_jobs"]:
                if not force:
                    raise LeaseError(
                        "running or unresolved pinned jobs must finish before SQL lease release",
                        status=409,
                        code="lease_pinned",
                        details={"running_jobs": lease["running_jobs"]},
                    )
                owners = lease.get("job_owners", {})
                live = [
                    job
                    for job in lease["running_jobs"]
                    if not self.sql_backend.owner_is_stale(owners.get(job, "unresolved-file-import"))
                ]
                if live:
                    raise LeaseError(
                        "cannot reconcile pinned jobs owned by an active or unknown hub; confirm remote jobs have stopped",
                        status=409,
                        code="lease_pinned",
                        details={"running_jobs": live},
                    )
                reconciled = list(lease["running_jobs"])
                lease["running_jobs"] = []
                lease["job_owners"] = {}
                self._event(
                    lease,
                    "pins_reconciled",
                    job_ids=reconciled,
                    warning="Remote work may still be running; verify before force release",
                )
            if lease["state"] != "released":
                lease["state"], lease["released_ts"] = "released", self._clock()
                self._event(lease, "force_released" if force else "released")
                client = self._clients.get(lease["client_id"])
                if client:
                    client["last_seen_ts"] = self._clock()
                self._save()
            return self._view(lease)

    def validate_job_reconciliation(
        self, *, lease_id: str, client_id: str, target: str, job_id: str, owner: str
    ) -> None:
        """Validate a pinned job against its current SQL lease without changing it."""
        if self.sql_backend is None:
            raise LeaseError(
                "job reconciliation requires Azure SQL shared storage",
                status=409,
                code="sql_reconciliation_required",
            )
        with self._lock:
            self._sweep()
            lease = self._lease(lease_id)
            self._client(client_id)
            if lease["state"] != "active" or lease["client_id"] != client_id:
                raise LeaseError(
                    "the supplied client does not hold this active lease",
                    status=409,
                    code="job_identity_mismatch",
                )
            members = lease.get("members")
            running_jobs = lease.get("running_jobs")
            job_owners = lease.get("job_owners")
            if (
                not isinstance(members, dict)
                or target not in members.values()
                or not isinstance(running_jobs, list)
                or not isinstance(job_owners, dict)
                or job_id not in running_jobs
                or job_owners.get(job_id) != owner
            ):
                raise LeaseError(
                    "the active lease does not contain this exact job pin and owner",
                    status=409,
                    code="job_identity_mismatch",
                )

    def validate_targets(self, lease_id: str, targets: Sequence[str]) -> None:
        with self._lock:
            self._sweep()
            lease = self._lease(lease_id)
            groups, membership = self._membership()
            del groups
            if any(membership.get(t, (None, None))[0] != lease["group"] for t in targets):
                raise LeaseError(
                    "target is outside this lease's group", status=409, code="target_not_in_lease"
                )
            self._renew(lease)

    def touch(self, lease_id: str) -> dict[str, Any]:
        with self._lock:
            self._sweep()
            lease = self._lease(lease_id)
            self._renew(lease)
            return self._view(lease)

    def use(self, target: str, lease_id: str | None = None, *, origin: str = "rest") -> dict[str, Any]:
        with self._lock:
            self._sweep()
            _, membership = self._membership()
            key, _ = membership[target]
            lease = None
            if lease_id:
                self.validate_targets(lease_id, [target])
                lease = self._lease(lease_id)
                lease["last_used_ts"] = self._clock()
            self._uses[key] = {
                **self._uses.get(key, {}),
                "at": self._clock(),
                "by": {"lease_id": lease_id} if lease_id else {"origin": origin},
            }
            active = self._active(key)
            result: dict[str, Any] = {"purpose": lease["purpose"] if lease else None}
            if active and not lease_id:
                view = self._view(active)
                result["lease_warning"] = {k: view[k] for k in ("lease_id", "group", "holder", "expires_at")}
                self._event(active, "warning", target=target, origin=origin)
            self._save()
            return result

    def read_use(self, target: str, lease_id: str | None = None, *, origin: str = "rest") -> dict[str, Any]:
        """Renew lease-backed reads authoritatively; defer only advisory unleased accounting."""
        model = getattr(self.sql_backend, "read_model", None)
        if model is None or lease_id is not None:
            return self.use(target, lease_id, origin=origin)
        with self._lock.read_snapshot() as snapshot:
            _, membership = self._membership()
            key, _ = membership[target]
            lease = self._read_lease(snapshot, lease_id) if lease_id else None
            if lease is not None:
                if lease["group"] != key or target not in lease["members"].values():
                    raise LeaseError(
                        "target is outside this lease's group", status=409, code="target_not_in_lease"
                    )
            result = {"purpose": lease["purpose"] if lease else None}
            active = next(
                (
                    item
                    for item in snapshot["leases"].values()
                    if item["group"] == key
                    and item["state"] == "active"
                    and (item["running_jobs"] or item["expires_ts"] > snapshot["now"])
                ),
                None,
            )
            if active and not lease_id:
                view = self._view(active, snapshot=snapshot)
                result["lease_warning"] = {k: view[k] for k in ("lease_id", "group", "holder", "expires_at")}
        model.defer_use(self, target, lease_id, origin)
        return result

    def read_touch(self, lease_id: str) -> None:
        # Renewal is lease authority, not advisory read accounting.
        self.touch(lease_id)

    def _read_lease(self, snapshot: dict[str, Any], lease_id: str) -> dict[str, Any]:
        lease = snapshot["leases"].get(lease_id)
        if lease is None:
            raise LeaseError(f"unknown lease {lease_id!r}", status=404, code="unknown_lease")
        if lease["state"] == "released":
            raise LeaseError("lease was released", status=409, code="lease_released")
        latest = next(
            (item for item in reversed(snapshot["leases"].values()) if item["group"] == lease["group"]), None
        )
        if latest is not None and latest["lease_id"] != lease_id:
            raise LeaseError(
                "this lease was superseded by a newer group allocation",
                status=409,
                code="lease_lost",
                details={"lease_id": lease_id, "holder": self._view(latest, snapshot=snapshot)["holder"]},
            )
        return lease

    def job_started(self, job: Any) -> None:
        with self._lock:
            usage = self.use(job.target.id, job.lease_id, origin=job.origin)
            job.lease_warning = usage.get("lease_warning")
            if job.lease_id:
                lease = self._lease(job.lease_id)
                job.client_id = lease["client_id"]
                if job.purpose is None:
                    job.purpose = lease["purpose"]
                lease["running_jobs"].append(job.id)
                if self.sql_backend is not None:
                    lease.setdefault("job_owners", {})[job.id] = self.sql_backend.owner
                self._event(lease, "pinned", job_id=job.id, target=job.target.id)
                self._save()
            elif self.sql_backend is not None:
                _, membership = self._membership()
                key = membership[job.target.id][0]
                use = self._uses[key]
                use.setdefault("running_jobs", {})[job.id] = self.sql_backend.owner

    def job_finished(self, job: Any) -> None:
        with self._lock:
            _, membership = self._membership()
            key = membership.get(job.target.id, (None, None))[0]
            if key is not None:
                self._uses[key] = {
                    **self._uses.get(key, {}),
                    "at": self._clock(),
                    "by": {"lease_id": job.lease_id} if job.lease_id else {"origin": job.origin},
                }
            if job.lease_id:
                lease = self._lease(job.lease_id)
                if (
                    self.sql_backend is not None
                    and lease.get("job_owners", {}).get(job.id) != self.sql_backend.owner
                ):
                    raise LeaseError(
                        "only the executing hub may acknowledge a job pin",
                        code="job_owner_mismatch",
                        status=409,
                    )
                lease["last_used_ts"] = self._clock()
                if job.id in lease["running_jobs"]:
                    lease["running_jobs"].remove(job.id)
                    if self.sql_backend is not None:
                        lease["job_owners"].pop(job.id, None)
                    self._event(lease, "unpinned", job_id=job.id, state=job.state)
                    if lease["state"] == "active" and not lease["running_jobs"]:
                        lease["renewed_ts"] = self._clock()
                        lease["expires_ts"] = self._clock() + lease["ttl_s"]
            elif self.sql_backend is not None and key is not None:
                running = self._uses[key].get("running_jobs", {})
                if running.get(job.id) != self.sql_backend.owner:
                    raise LeaseError(
                        "only the executing hub may acknowledge an unleased job",
                        code="job_owner_mismatch",
                        status=409,
                    )
                running.pop(job.id)
            self._save()
