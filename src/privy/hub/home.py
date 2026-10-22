"""The hub's home directory: hydrated relays, audit log, and runtime state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from privy.hub.config import RELAYS_FILENAME

DEFAULT_PORT = 7748
HOME_ENV = "PRIVY_HUB_HOME"
STATE_FILENAME = "hub-state.json"
AUDIT_FILENAME = "audit.jsonl"
#: Bearer token file written by hub 0.2.x; the hub no longer authenticates callers.
LEGACY_TOKEN_FILENAME = "hub-token"

__all__ = [
    "AUDIT_FILENAME",
    "DEFAULT_PORT",
    "HOME_ENV",
    "LEGACY_TOKEN_FILENAME",
    "RELAYS_FILENAME",
    "STATE_FILENAME",
    "default_home",
    "ensure_home",
    "read_state",
    "write_state",
]


def default_home() -> Path:
    """``$PRIVY_HUB_HOME``, else a per-user application data directory."""
    configured = os.environ.get(HOME_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "privy" / "hub"
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "privy" / "hub"


def ensure_home(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(home, 0o700)
    return home


def _write_private(path: Path, text: str) -> None:
    """Create ``path`` readable only by this user (POSIX mode; Windows inherits the folder ACL)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(text.encode("utf-8"))


def write_state(home: Path, state: dict[str, Any]) -> None:
    """Record where the running hub listens (no secrets) for ``privy hub status``."""
    _write_private(home / STATE_FILENAME, json.dumps(state, indent=2) + "\n")


def read_state(home: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((home / STATE_FILENAME).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
