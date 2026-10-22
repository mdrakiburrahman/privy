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

    # ---- lookup -----------------------------------------------------------

    def snapshot(self) -> RelaysSnapshot:
        return self.store.snapshot()

    def client(self, target: Target) -> Any:
        """A Relay client for ``target`` that mints short-lived SAS tokens."""
        if self._client_factory is not None:
            return self._client_factory(target)
        return RelayClient(**target.relay_client_kwargs(ttl_seconds=self._token_ttl_s))

    def resolve(self, name: str) -> Target:
        """Find a target by id (``relay/path``) or by a hybrid connection name unique across relays."""
        wanted = name.strip().lower()
        targets = self.snapshot().targets
        for target in targets:
            if target.id.lower() == wanted:
                return target
        by_path = [target for target in targets if target.path.lower() == wanted]
        if len(by_path) == 1:
            return by_path[0]
        if by_path:
            ids = sorted(target.id for target in by_path)
            raise TargetLookupError(f"target {name!r} is ambiguous; use one of: {', '.join(ids)}", ids)
        names = [target.id for target in targets] + [target.path for target in targets]
        close = difflib.get_close_matches(name, names, n=5, cutoff=0.5)
        if not targets:
            raise TargetLookupError(
                f"unknown target {name!r}: no relays are hydrated (see {self.snapshot().path})"
            )
        hint = f"; did you mean: {', '.join(close)}" if close else "; call list_targets to see all targets"
        raise TargetLookupError(f"unknown target {name!r}{hint}", close)

    def select(self, patterns: Iterable[str]) -> list[Target]:
        """Targets whose id (or hybrid connection name) matches any glob pattern."""
        patterns = [pattern for pattern in patterns if pattern.strip()]
        return [
            target
            for target in sorted(self.snapshot().targets, key=lambda item: item.id.lower())
            if any(_matches(target, pattern) for pattern in patterns)
        ]

    # ---- probing ----------------------------------------------------------

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
