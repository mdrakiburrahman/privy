"""Hydrated Relay credentials for the hub: the ``relays.json`` file.

The hub never calls Azure Resource Manager. An operator hydrates this file once
(see ``docs/SCHEDULED_TASKS.md``) and the hub re-reads it whenever it changes::

    {
      "version": 1,
      "relays": [
        {
          "name": "dev",
          "namespace": "contoso-relay",
          "hybrid_connections": [
            {"path": "devbox01_windows", "keyrule": "all", "key": "<base64 key>"},
            {"path": "devbox02_windows", "keyrule": "all", "key": "<base64 key>"}
          ]
        },
        {
          "namespace": "contoso-lab-relay",
          "keyrule": "privy-send",
          "key": "<namespace-level key>",
          "hybrid_connections": ["gpu01_linux", "gpu02_linux"]
        }
      ]
    }

A hybrid connection uses its own ``keyrule``/``key`` (or a pre-minted ``token``)
and otherwise inherits the relay-level credential. Each listener becomes a
target named ``<relay name>/<hybrid connection>``; the relay name defaults to the
namespace's short name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("privy.hub")

RELAYS_FILENAME = "relays.json"
RELAYS_FILE_VERSION = 1
MAX_RELAYS_FILE_BYTES = 16 * 1024 * 1024

_RELAY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,259}$")
_KEYRULE_RE = re.compile(r"^[A-Za-z0-9._-]{1,256}$")
_METADATA_FIELDS = ("subscription_id", "resource_group", "hydrated_at")
_MAX_DESCRIPTION_CHARS = 1024


class RelaysFileError(ValueError):
    """``relays.json`` is unreadable or does not match the expected shape."""


@dataclass(frozen=True)
class Target:
    """One listener reachable through a hybrid connection."""

    id: str
    relay: str
    namespace: str
    path: str
    description: str | None = None
    keyrule: str | None = field(default=None, repr=False)
    key: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)

    def relay_client_kwargs(self, *, ttl_seconds: int) -> dict[str, Any]:
        """Constructor arguments for :class:`privy.RelayClient`."""
        if self.token is not None:
            return {"namespace": self.namespace, "path": self.path, "token": self.token}
        return {
            "namespace": self.namespace,
            "path": self.path,
            "keyrule": self.keyrule,
            "key": self.key,
            "ttl_seconds": ttl_seconds,
        }

    @property
    def credential_fingerprint(self) -> str:
        """Opaque digest that changes when the credential is re-hydrated."""
        material = "\0".join((self.keyrule or "", self.key or "", self.token or ""))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Relay:
    """One Azure Relay namespace and the listeners hydrated for it."""

    name: str
    namespace: str
    targets: tuple[Target, ...]
    description: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


def _fail(where: str, message: str) -> RelaysFileError:
    return RelaysFileError(f"{where}: {message}")


def _optional_text(value: Any, where: str, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail(where, f"{name} must be a string")
    if len(value) > _MAX_DESCRIPTION_CHARS:
        raise _fail(where, f"{name} must be at most {_MAX_DESCRIPTION_CHARS} characters")
    return value.strip() or None


def _credential(obj: Mapping[str, Any], where: str) -> dict[str, str] | None:
    keyrule = obj.get("keyrule")
    key = obj.get("key")
    token = obj.get("token")
    if token is not None and (keyrule is not None or key is not None):
        raise _fail(where, "token is mutually exclusive with keyrule and key")
    if token is not None:
        if not isinstance(token, str) or not token.strip().startswith("SharedAccessSignature "):
            raise _fail(where, "token must be a string starting with 'SharedAccessSignature '")
        return {"token": token.strip()}
    if keyrule is None and key is None:
        return None
    if not isinstance(keyrule, str) or not _KEYRULE_RE.match(keyrule):
        raise _fail(where, "keyrule must be an authorization rule name")
    if not isinstance(key, str) or not key.strip():
        raise _fail(where, "key must be a non-empty string")
    return {"keyrule": keyrule, "key": key.strip()}


def _parse_relay(obj: Any, where: str) -> Relay:
    if not isinstance(obj, dict):
        raise _fail(where, "must be an object")
    namespace = obj.get("namespace")
    if not isinstance(namespace, str) or not _NAMESPACE_RE.match(namespace):
        raise _fail(where, "namespace must be a Relay namespace name or host name")
    name = obj.get("name", namespace.split(".", 1)[0])
    if not isinstance(name, str) or not _RELAY_NAME_RE.match(name):
        raise _fail(where, "name must be 1-63 letters, digits, '.', '_' or '-'")
    relay_credential = _credential(obj, where)
    connections = obj.get("hybrid_connections")
    if not isinstance(connections, list):
        raise _fail(where, "hybrid_connections must be a list")

    targets: list[Target] = []
    seen: set[str] = set()
    for index, entry in enumerate(connections):
        entry_where = f"{where}.hybrid_connections[{index}]"
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict):
            raise _fail(entry_where, "must be a hybrid connection name or an object")
        path = entry.get("path")
        if not isinstance(path, str) or not _PATH_RE.match(path):
            raise _fail(entry_where, "path must be a hybrid connection name")
        if path.lower() in seen:
            raise _fail(entry_where, f"duplicate hybrid connection {path!r}")
        seen.add(path.lower())
        credential = _credential(entry, entry_where) or relay_credential
        if credential is None:
            raise _fail(entry_where, "needs keyrule and key (or token), here or on its relay")
        targets.append(
            Target(
                id=f"{name}/{path}",
                relay=name,
                namespace=namespace,
                path=path,
                description=_optional_text(entry.get("description"), entry_where, "description"),
                **credential,
            )
        )

    metadata: dict[str, str] = {}
    for key in _METADATA_FIELDS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            metadata[key] = value.strip()
    return Relay(
        name=name,
        namespace=namespace,
        targets=tuple(sorted(targets, key=lambda target: target.id.lower())),
        description=_optional_text(obj.get("description"), where, "description"),
        metadata=metadata,
    )


def parse_relays_document(document: Any) -> tuple[Relay, ...]:
    """Validate a decoded ``relays.json`` document."""
    if not isinstance(document, dict):
        raise RelaysFileError("relays file must contain a JSON object")
    version = document.get("version", RELAYS_FILE_VERSION)
    if version != RELAYS_FILE_VERSION:
        raise RelaysFileError(f"unsupported relays file version {version!r} (expected {RELAYS_FILE_VERSION})")
    relays_raw = document.get("relays")
    if not isinstance(relays_raw, list):
        raise RelaysFileError("relays must be a list")
    relays = [_parse_relay(item, f"relays[{index}]") for index, item in enumerate(relays_raw)]
    names: set[str] = set()
    for relay in relays:
        if relay.name.lower() in names:
            raise RelaysFileError(f"duplicate relay name {relay.name!r}; give each relay a unique name")
        names.add(relay.name.lower())
    return tuple(relays)


def read_relays_file(path: Path) -> tuple[Relay, ...]:
    """Read and validate ``path``. A missing file raises :class:`FileNotFoundError`."""
    with path.open("rb") as stream:
        raw = stream.read(MAX_RELAYS_FILE_BYTES + 1)
    if len(raw) > MAX_RELAYS_FILE_BYTES:
        raise RelaysFileError(f"relays file is larger than {MAX_RELAYS_FILE_BYTES} bytes")
    try:
        # utf-8-sig tolerates the BOM Windows PowerShell 5.1 writes by default.
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelaysFileError(f"relays file is not valid UTF-8 JSON: {exc}") from None
    return parse_relays_document(document)


@dataclass(frozen=True)
class RelaysSnapshot:
    """The relays the hub is currently serving."""

    path: Path
    relays: tuple[Relay, ...]
    file_exists: bool
    loaded_at: float | None
    error: str | None = None

    @property
    def targets(self) -> tuple[Target, ...]:
        return tuple(target for relay in self.relays for target in relay.targets)


class RelaysStore:
    """Hot-reloading view of ``relays.json``.

    The file is re-read when its size or modification time changes (checked at
    most every ``check_interval_s``). A missing file serves no relays; an invalid
    file keeps the last valid relays and reports the error.
    """

    def __init__(
        self,
        path: Path,
        *,
        check_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self._check_interval_s = check_interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._signature: tuple[int, int] | None = None
        self._last_check: float | None = None
        self._snapshot = RelaysSnapshot(self.path, (), False, None)

    def snapshot(self) -> RelaysSnapshot:
        with self._lock:
            now = self._clock()
            if self._last_check is None or now - self._last_check >= self._check_interval_s:
                self._refresh(now)
            return self._snapshot

    def reload(self) -> RelaysSnapshot:
        """Re-read the file now, even if it looks unchanged."""
        with self._lock:
            self._signature = None
            self._refresh(self._clock())
            return self._snapshot

    def _refresh(self, now: float) -> None:
        self._last_check = now
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            # Warn once at startup and again only if a hydrated file disappears.
            if self._snapshot.file_exists or self._snapshot.loaded_at is None:
                log.warning("Relays file %s not found; serving no relays until it is hydrated.", self.path)
            self._signature = None
            self._snapshot = RelaysSnapshot(self.path, (), False, time.time())
            return
        except OSError as exc:
            self._snapshot = RelaysSnapshot(
                self.path,
                self._snapshot.relays,
                True,
                self._snapshot.loaded_at,
                f"cannot read relays file: {exc}",
            )
            return

        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self._signature:
            return
        self._signature = signature
        try:
            relays = read_relays_file(self.path)
        except (RelaysFileError, OSError) as exc:
            log.warning("Ignoring invalid relays file %s: %s", self.path, exc)
            self._snapshot = RelaysSnapshot(
                self.path,
                self._snapshot.relays,
                True,
                self._snapshot.loaded_at,
                str(exc),
            )
            return
        self._snapshot = RelaysSnapshot(self.path, relays, True, time.time())
        log.info(
            "Loaded %d relay(s) with %d target(s) from %s",
            len(relays),
            len(self._snapshot.targets),
            self.path,
        )
