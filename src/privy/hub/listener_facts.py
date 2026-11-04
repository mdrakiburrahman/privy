"""What the hub knows about each listener's OS and versions, and filters over it.

Facts come from the listener's own description (``listener_info``) when it is
online, else from the last description it gave, which is kept in
``listener-info-state.json`` so offline listeners still show their details after
a hub restart. The environment (Windows host, WSL, devcontainer) is not reported
by listeners; it is inferred from the hybrid connection name's suffix.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from privy.hub.config import Target
from privy.hub.home import _write_private
from privy.hub.jobs import utc_iso
from privy.hub.registry import ONLINE, ProbeResult

log = logging.getLogger("privy.hub")

STATE_FILENAME = "listener-info-state.json"
#: Filterable facets as ``(name, label)``; the names are also list_targets parameters.
FACETS: tuple[tuple[str, str], ...] = (
    ("os", "OS"),
    ("os_version", "OS version"),
    ("environment", "Environment"),
    ("arch", "Arch"),
    ("privy_version", "Privy"),
    ("python_version", "Python"),
    ("shell", "Shell"),
)
UNKNOWN_VALUE = "Unknown"
NO_SHELL = "none"

_FAMILIES = {"windows": "Windows", "linux": "Linux", "darwin": "macOS"}
_ARCHES = {"amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}
_ENVIRONMENTS = {
    "windows": "Windows host",
    "win": "Windows host",
    "wsl": "WSL",
    "devcontainer": "Devcontainer",
    "linux": "Linux host",
    "ubuntu": "Linux host",
    "debian": "Linux host",
    "mac": "macOS host",
    "macos": "macOS host",
    "darwin": "macOS host",
}
#: Parts of a description that change on every probe and say nothing about the machine.
_VOLATILE = ("jobs", "uptime_s", "time_utc")
#: An unchanged description is rewritten at most this often (only its "seen at" moves).
REFRESH_S = 300.0
#: Descriptions of listeners not seen for this long are forgotten.
FORGET_S = 90 * 24 * 60 * 60
SAVE_DELAY_S = 2.0


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def environment_of(path: str) -> str | None:
    suffix = path.rsplit("_", 1)[-1].lower() if "_" in path else ""
    return _ENVIRONMENTS.get(suffix)


def describe(info: Mapping[str, Any] | None, path: str) -> dict[str, Any]:
    """Structured OS and version facts from a listener description (``None`` when unknown)."""
    info = info if isinstance(info, Mapping) else None
    platform = (info or {}).get("platform")
    platform = platform if isinstance(platform, Mapping) else {}
    python = (info or {}).get("python")
    python = python if isinstance(python, Mapping) else {}
    capabilities = (info or {}).get("capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    terminal = capabilities.get("terminal")
    terminal = terminal if isinstance(terminal, Mapping) else {}

    system = _text(platform.get("system"))
    family = _FAMILIES.get(system.lower(), system) if system else None
    release = _text(platform.get("release"))
    version = _text(platform.get("version"))
    description = _text(platform.get("description")) or ""
    build = version.rsplit(".", 1)[-1] if family == "Windows" and version else None
    if family == "macOS" and description.startswith("macOS-"):
        shown = description.split("-")[1]
    else:
        shown = release
    os_version = " · ".join(part for part in (" ".join(filter(None, (family, shown))), build) if part)
    machine = _text(platform.get("machine"))
    shells: list[str] | None = None
    if info is not None:
        reported = terminal.get("shells")
        shells = (
            sorted(item for item in reported if isinstance(item, str)) if isinstance(reported, list) else []
        )
    return {
        "os_family": family,
        "os_release": release,
        "os_build": build,
        "os_version": os_version or None,
        "arch": _ARCHES.get(machine.lower(), machine) if machine else None,
        "privy_version": _text((info or {}).get("privy_version")),
        "python_version": _text(python.get("version")),
        "environment": environment_of(path),
        "shells": shells,
    }


def facet_values(facts: Mapping[str, Any]) -> dict[str, list[str]]:
    """Each facet's values for one listener, as shown in filters."""
    shells = facts.get("shells")
    values = {
        name: [facts.get(key) or UNKNOWN_VALUE]
        for name, key in (
            ("os", "os_family"),
            ("os_version", "os_version"),
            ("environment", "environment"),
            ("arch", "arch"),
            ("privy_version", "privy_version"),
            ("python_version", "python_version"),
        )
    }
    values["shell"] = list(shells) if shells else [NO_SHELL] if shells is not None else [UNKNOWN_VALUE]
    return values


def matches(facts: Mapping[str, Any], filters: Mapping[str, Sequence[str]]) -> bool:
    """Whether a listener passes every facet filter.

    Values of one facet are alternatives; facets must all match. Matching ignores
    case and accepts ``*`` globs; ``os_version`` also matches the bare release or build.
    """
    values = facet_values(facts)
    values["os_version"] += [value for value in (facts.get("os_release"), facts.get("os_build")) if value]
    for name, wanted in filters.items():
        patterns = [item.strip().lower() for item in wanted if item.strip()]
        if not patterns:
            continue
        have = [value.lower() for value in values[name]]
        if not any(fnmatch.fnmatchcase(value, pattern) for value in have for pattern in patterns):
            return False
    return True


class ListenerInfoStore:
    """The last description each listener gave, kept across hub restarts.

    A cache: an unreadable file is ignored and write failures are logged.
    """

    def __init__(
        self,
        home: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        save_delay_s: float = SAVE_DELAY_S,
    ) -> None:
        self.path = home / STATE_FILENAME if home is not None else None
        self._clock = clock
        self._save_delay_s = save_delay_s
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._known: dict[str, dict[str, Any]] = {}
        self._saved_at: dict[str, float] = {}
        self._timer: threading.Timer | None = None
        self._dirty = False
        if self.path is not None:
            self._load(self.path)

    def _load(self, path: Path) -> None:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", path, exc)
            return
        targets = state.get("targets") if isinstance(state, dict) and state.get("version") == 1 else None
        if not isinstance(targets, dict):
            log.warning("ignoring %s: expected version 1 with a targets object", path)
            return
        cutoff = self._clock() - FORGET_S
        for target_id, item in targets.items():
            if not isinstance(item, dict) or not isinstance(item.get("info"), dict):
                continue
            seen = item.get("seen_at")
            if isinstance(seen, (int, float)) and not isinstance(seen, bool) and seen >= cutoff:
                self._known[target_id] = {"seen_at": float(seen), "info": item["info"]}
                self._saved_at[target_id] = float(seen)

    def record(self, target: Target, result: ProbeResult) -> None:
        if result.status != ONLINE or not isinstance(result.info, dict):
            return
        info = {key: value for key, value in result.info.items() if key not in _VOLATILE}
        with self._lock:
            previous = self._known.get(target.id)
            self._known[target.id] = {"seen_at": result.checked_at, "info": info}
            if (
                previous is not None
                and previous["info"] == info
                and result.checked_at - self._saved_at.get(target.id, 0.0) < REFRESH_S
            ):
                return
            self._dirty = True
            if self.path is not None and self._timer is None:
                self._timer = threading.Timer(self._save_delay_s, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def last(self, target_id: str) -> tuple[dict[str, Any], float] | None:
        with self._lock:
            item = self._known.get(target_id)
        return (item["info"], item["seen_at"]) if item else None

    def flush(self) -> None:
        """Write pending changes now."""
        with self._write_lock:
            with self._lock:
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                if self.path is None or not self._dirty:
                    return
                self._dirty = False
                cutoff = self._clock() - FORGET_S
                targets = {key: dict(item) for key, item in self._known.items() if item["seen_at"] >= cutoff}
                self._saved_at = {key: item["seen_at"] for key, item in targets.items()}
            temporary = self.path.with_suffix(".tmp")
            try:
                _write_private(
                    temporary,
                    json.dumps({"version": 1, "targets": targets}, ensure_ascii=True, sort_keys=True) + "\n",
                )
                temporary.replace(self.path)
            except OSError as exc:
                log.warning("could not save %s: %s", self.path, exc)
                with self._lock:
                    self._dirty = True


def facts_for(
    store: ListenerInfoStore, target_id: str, path: str, probe: ProbeResult | None
) -> dict[str, Any]:
    """Facts from the live probe, else the last known description (``info_stale``)."""
    if probe is not None and probe.status == ONLINE and probe.info is not None:
        info, seen, stale = probe.info, probe.checked_at, False
    else:
        known = store.last(target_id)
        info, seen, stale = (known[0], known[1], True) if known else (None, None, False)
    facts = describe(info, path)
    facts.update(info_seen_at=utc_iso(seen), info_seen_at_ts=seen, info_stale=stale)
    return facts
