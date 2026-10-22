"""Side-effect-free description of a listener, answered for ``kind="listener_info"``.

A hub or operator sends this request to check that a listener is reachable and
to learn what the machine can run, without executing any code. Listeners that
predate this request reject the unknown kind with an ordinary ``bad_request``
execution response, which still proves they are online.
"""

from __future__ import annotations

import datetime as dt
import getpass
import os
import platform
import socket
import sys
import time
from typing import Any

from privy.executor import available_runtimes, job_counts
from privy.protocol import LISTENER_INFO_KIND, PROTOCOL_VERSION

__all__ = ["LISTENER_INFO_KIND", "collect_listener_info"]


def _current_user() -> str | None:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser raises when no login name can be found
        return os.environ.get("USERNAME") or os.environ.get("USER")


def collect_listener_info(
    *,
    started_at: float | None = None,
    proxy_enabled: bool = False,
) -> dict[str, Any]:
    """Describe this listener process and host. Never runs submitted code."""
    from privy import __version__

    now = time.time()
    runtimes = available_runtimes()
    languages = ["python"]
    languages.extend(name for name in ("bash", "powershell") if runtimes.get(name))
    return {
        "kind": LISTENER_INFO_KIND,
        "protocol_version": PROTOCOL_VERSION,
        "privy_version": __version__,
        "hostname": socket.gethostname(),
        "user": _current_user(),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "description": platform.platform(),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "frozen": bool(getattr(sys, "frozen", False)),
        },
        "runtimes": runtimes,
        "capabilities": {
            "languages": languages,
            "inprocess_python": True,
            "streaming": True,
            "file_transfer": True,
            "http_proxy": proxy_enabled,
        },
        "jobs": job_counts(),
        "uptime_s": None if started_at is None else max(0, int(now - started_at)),
        "time_utc": dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
