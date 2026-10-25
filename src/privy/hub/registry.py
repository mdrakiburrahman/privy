"""Targets the hub can reach, how to address them, and whether they are online."""

from __future__ import annotations

import difflib
import fnmatch
import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from privy._relay import RelayTokenError, redact_relay_secrets
from privy.client import RelayClient, RelayHTTPError
from privy.hub import console
from privy.hub.config import RelaysSnapshot, RelaysStore, Target

log = logging.getLogger("privy.hub")

#: Lifetime of the SAS tokens the hub mints for each Relay request.
DEFAULT_TOKEN_TTL_S = 60 * 60
DEFAULT_PROBE_TTL_S = 30.0
DEFAULT_PROBE_TIMEOUT_S = 10.0

ONLINE = "online"
OFFLINE = "offline"
NOT_FOUND = "not_found"
UNAUTHORIZED = "unauthorized"
UNREACHABLE = "unreachable"
ERROR = "error"
UNKNOWN = "unknown"

ClientFactory = Callable[[Target], Any]


class TargetLookupError(LookupError):
    """A target name did not identify exactly one hydrated listener."""

    def __init__(self, message: str, candidates: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.candidates = list(candidates)


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of asking one listener to describe itself."""

    status: str
    checked_at: float
    latency_ms: int | None = None
    info: dict[str, Any] | None = None
    legacy: bool = False
    error: str | None = None
    fingerprint: str = ""


def describe_relay_failure(exc: BaseException) -> tuple[str, str]:
    """Map a failed Relay request to ``(status, human-readable reason)``.

    Azure Relay answers HTTP 404 both when no listener is connected
    (``EndpointNotFound``: "There are no listeners connected") and when the
    hybrid connection does not exist ("No service is hosted"), so the body text
    decides between ``offline`` and ``not_found``.
    """
    if isinstance(exc, RelayHTTPError):
        detail = exc.detail.lower()
        code = exc.status_code
        if exc.timed_out:
            return UNREACHABLE, "timed out waiting for the listener; it may be asleep or reconnecting"
        if code == 404 and ("no listeners" in detail or "endpointnotfound" in detail):
            return OFFLINE, "no listener is connected to this hybrid connection"
        if code == 404:
            return NOT_FOUND, "this hybrid connection does not exist in the namespace"
        if code in (502, 503):
            return OFFLINE, "Azure Relay could not route the request to a listener"
        if code == 504:
            return (
                UNREACHABLE,
                "the listener did not acknowledge the request in time; it may be asleep or reconnecting",
            )
        if code in (401, 403):
            return (
                UNAUTHORIZED,
                f"Azure Relay rejected the hydrated credential (HTTP {code}); re-hydrate keys",
            )
        if code is None:
            return ERROR, redact_relay_secrets(str(exc))
        return ERROR, f"Azure Relay returned HTTP {code}"
    if isinstance(exc, RelayTokenError):
        return UNAUTHORIZED, redact_relay_secrets(str(exc))
    return ERROR, redact_relay_secrets(f"{type(exc).__name__}: {exc}")


def _matches(target: Target, pattern: str) -> bool:
    pattern = pattern.strip().lower()
    if not pattern:
        return False
    if fnmatch.fnmatchcase(target.id.lower(), pattern):
        return True
    return "/" not in pattern and fnmatch.fnmatchcase(target.path.lower(), pattern)


def machine_name(path: str) -> str:
    """The machine part of a hybrid connection name: ``devbox01_windows`` -> ``devbox01``."""
    return path.rsplit("_", 1)[0].lower() if "_" in path else path.lower()


_WINDOWS_SUFFIXES = ("windows", "win")
_POSIX_SUFFIXES = ("linux", "wsl", "devcontainer", "ubuntu", "debian", "mac", "macos", "darwin")


def language_from(info: dict[str, Any] | None, path: str) -> str | None:
    """Default language for a listener: ``powershell`` on Windows, ``bash`` elsewhere.

    Uses the listener's reported OS, else the hybrid connection's suffix
    (``_windows``, ``_wsl``, ...); ``None`` when neither tells.
    """
    system = str(((info or {}).get("platform") or {}).get("system") or "")
    if system == "Windows":
        return "powershell"
    if system:
        return "bash"
    suffix = path.rsplit("_", 1)[-1].lower() if "_" in path else ""
    if suffix in _WINDOWS_SUFFIXES:
        return "powershell"
    if suffix in _POSIX_SUFFIXES:
        return "bash"
    return None


class TargetRegistry:
    """Resolve, select, and probe the targets hydrated in ``relays.json``."""

    def __init__(
        self,
        store: RelaysStore,
        *,
        client_factory: ClientFactory | None = None,
        probe_ttl_s: float = DEFAULT_PROBE_TTL_S,
        probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
        token_ttl_s: int = DEFAULT_TOKEN_TTL_S,
        max_probe_workers: int = 16,
    ) -> None:
        self.store = store
        self._client_factory = client_factory
        self._probe_ttl_s = probe_ttl_s
        self._probe_timeout_s = probe_timeout_s
        self._token_ttl_s = token_ttl_s
        self._max_probe_workers = max_probe_workers
        self._cache: dict[str, ProbeResult] = {}
        self._cache_lock = threading.Lock()
        #: Called after every fresh probe (the dashboard's uptime and latency).
        self.on_probe: Callable[[Target, ProbeResult], None] | None = None

    # ---- lookup -----------------------------------------------------------

    def snapshot(self) -> RelaysSnapshot:
        return self.store.snapshot()

    def client(self, target: Target) -> Any:
        """A Relay client for ``target`` that mints short-lived SAS tokens."""
        if self._client_factory is not None:
            return self._client_factory(target)
        return RelayClient(**target.relay_client_kwargs(ttl_seconds=self._token_ttl_s))

    def resolve(self, name: str) -> Target:
        """Find one target by name.

        Accepted, in order: the full id (``relay/hyco``), the hybrid connection
        name, the machine name (the hybrid connection name without a trailing
        ``_windows``-style suffix), or the listener's host name. Any of them may
        be prefixed with ``relay/``. When several targets match, the single one
        that is online wins.
        """
        wanted = name.strip().lower()
        targets = self.snapshot().targets
        if not targets:
            raise TargetLookupError(
                f"unknown target {name!r}: no relays are hydrated (see {self.snapshot().path})"
            )
        if not wanted:
            raise TargetLookupError("target must not be empty; call list_targets to see all targets")
        for target in targets:
            if target.id.lower() == wanted:
                return target
        scope, short = list(targets), wanted
        relay, slash, rest = wanted.partition("/")
        if slash:
            scoped = [target for target in targets if target.relay.lower() == relay]
            if scoped:
                scope, short = scoped, rest.strip()
                if not short:
                    raise TargetLookupError(
                        f"target {name!r} names no machine after the relay",
                        [target.id for target in scoped],
                    )
        candidates = [target for target in scope if target.path.lower() == short]
        if not candidates:
            candidates = [target for target in scope if machine_name(target.path) == short]
        if not candidates:
            self.probe_many(scope)
            candidates = [target for target in scope if self._has_hostname(target, short)]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            probes = self.probe_many(candidates)
            online = [target for target in candidates if probes[target.id].status == ONLINE]
            if len(online) == 1:
                return online[0]
            ordered = sorted(candidates, key=lambda item: item.id)
            listing = ", ".join(f"{target.id} ({probes[target.id].status})" for target in ordered)
            raise TargetLookupError(
                f"target {name!r} is ambiguous; use one of: {listing}",
                [target.id for target in ordered],
            )
        names = sorted(
            {target.id for target in targets}
            | {target.path for target in targets}
            | {machine_name(target.path) for target in targets}
        )
        close = difflib.get_close_matches(name.strip(), names, n=5, cutoff=0.5)
        hint = f"; did you mean: {', '.join(close)}" if close else "; call list_targets to see all targets"
        raise TargetLookupError(f"unknown target {name!r}{hint}", close)

    def _hostname(self, target: Target) -> str | None:
        cached = self.cached(target)
        if cached is None or not cached.info:
            return None
        hostname = cached.info.get("hostname")
        return hostname if isinstance(hostname, str) else None

    def _has_hostname(self, target: Target, wanted: str) -> bool:
        hostname = self._hostname(target)
        return hostname is not None and hostname.strip().lower() == wanted

    def default_language(self, target: Target) -> str | None:
        """``powershell`` for Windows listeners, ``bash`` elsewhere, ``None`` if unknown."""
        return language_from(self.probe(target).info, target.path)

    def select(self, patterns: Iterable[str]) -> list[Target]:
        """Targets whose id (or hybrid connection name) matches any glob pattern."""
        patterns = [pattern for pattern in patterns if pattern.strip()]
        return [
            target
            for target in sorted(self.snapshot().targets, key=lambda item: item.id.lower())
            if any(_matches(target, pattern) for pattern in patterns)
        ]

    # ---- probing ----------------------------------------------------------

    @property
    def probe_ttl_s(self) -> float:
        """How long a probe result is reused before the listener is asked again."""
        return self._probe_ttl_s

    def cached(self, target: Target) -> ProbeResult | None:
        with self._cache_lock:
            result = self._cache.get(target.id)
        if result is None or result.fingerprint != target.credential_fingerprint:
            return None
        return result

    def probe(self, target: Target, *, refresh: bool = False) -> ProbeResult:
        cached = self.cached(target)
        if cached is not None and not refresh and time.time() - cached.checked_at < self._probe_ttl_s:
            return cached
        result = self._probe_now(target)
        with self._cache_lock:
            self._cache[target.id] = result
        console.probe(target, result)
        if self.on_probe is not None:
            self.on_probe(target, result)
        return result

    def probe_many(self, targets: Sequence[Target], *, refresh: bool = False) -> dict[str, ProbeResult]:
        if not targets:
            return {}
        workers = max(1, min(self._max_probe_workers, len(targets)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="privy-hub-probe") as pool:
            results = list(pool.map(lambda target: self.probe(target, refresh=refresh), targets))
        return {target.id: result for target, result in zip(targets, results)}

    def note_failure(self, target: Target, exc: BaseException) -> tuple[str, str]:
        """Record what a failed job learned about the target's reachability."""
        status, reason = describe_relay_failure(exc)
        if status in (OFFLINE, NOT_FOUND, UNAUTHORIZED):
            with self._cache_lock:
                self._cache[target.id] = ProbeResult(
                    status=status,
                    checked_at=time.time(),
                    error=reason,
                    fingerprint=target.credential_fingerprint,
                )
        return status, reason

    def _probe_now(self, target: Target) -> ProbeResult:
        started = time.monotonic()
        fingerprint = target.credential_fingerprint
        try:
            info = self.client(target).listener_info(http_timeout_s=self._probe_timeout_s)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a status
            status, reason = describe_relay_failure(exc)
            log.debug("probe %s: %s (%s)", target.id, status, reason)
            return ProbeResult(
                status=status,
                checked_at=time.time(),
                latency_ms=int((time.monotonic() - started) * 1000),
                error=reason,
                fingerprint=fingerprint,
            )
        return ProbeResult(
            status=ONLINE,
            checked_at=time.time(),
            latency_ms=int((time.monotonic() - started) * 1000),
            info=info,
            legacy=info is None,
            fingerprint=fingerprint,
        )
