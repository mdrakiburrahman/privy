"""SAS token + URL helpers for Azure Relay Hybrid Connections.

Ported from the reference implementation in `.temp/relay-demo/common/relay.py`.
Pure functions — no file or environment reads.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import math
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import TypedDict, Union

DEFAULT_TOKEN_TTL_SECONDS = 60 * 60 * 48

TokenProvider = Union[str, Callable[[], str]]

RELAY_SECRET_ENV_VARS = (
    "PRIVY_RELAY_TOKEN",
    "PRIVY_RELAY_KEY",
    "PRIVY_RELAY_SEND_KEY",
    "PRIVY_RELAY_LISTEN_KEY",
)


class SasTokenClaims(TypedDict):
    sr: str
    sig: str
    se: int
    skn: str


class RelayTokenError(ValueError):
    """Base class for invalid or unusable Azure Relay SAS tokens."""


class RelayTokenExpiredError(RelayTokenError):
    """Raised when a SAS token can no longer be used."""

    def __init__(self, expires_at: int) -> None:
        self.expires_at = expires_at
        super().__init__(f"relay token expired at {_format_utc(expires_at)}")


class RelayTokenAudienceError(RelayTokenError):
    """Raised when a SAS token was minted for a different Relay audience."""


_TOKEN_QUERY_RE = re.compile(r"(?i)(sb-hc-token=)[^&\s\"']+")
_RAW_TOKEN_RE = re.compile(r"(?i)SharedAccessSignature\s+sr=[^\s\"']+")


def _hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key=key, msg=msg, digestmod=hashlib.sha256).digest()


def _format_utc(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def fqdn(namespace: str) -> str:
    """Return the fully-qualified servicebus hostname for a Relay namespace.

    Accepts either the short namespace (``"myns"``) or a value that already
    contains ``.servicebus.windows.net`` — in the latter case it is returned
    unchanged.
    """
    if not namespace:
        raise ValueError("namespace must be non-empty")
    if "." in namespace:
        return namespace
    return f"{namespace}.servicebus.windows.net"


def relay_audience(service_namespace: str, entity_path: str) -> str:
    """Return the audience string Azure Relay expects in a SAS token."""
    if not entity_path:
        raise ValueError("path must be non-empty")
    return f"http://{fqdn(service_namespace)}/{entity_path.lstrip('/')}"


def create_sas_token(
    service_namespace: str,
    entity_path: str,
    sas_key_name: str,
    sas_key: str,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
) -> str:
    if not sas_key_name or not sas_key:
        raise ValueError("keyrule and key must be non-empty")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive integer")
    uri = relay_audience(service_namespace, entity_path)
    encoded_uri = urllib.parse.quote(uri, safe="")
    expiry = math.floor(time.time()) + ttl_seconds
    signature = f"{encoded_uri}\n{expiry}".encode()
    digest = _hmac_sha256(sas_key.encode("utf-8"), signature)
    sig = urllib.parse.quote(base64.b64encode(digest), safe="")
    return f"SharedAccessSignature sr={encoded_uri}&sig={sig}&se={expiry}&skn={sas_key_name}"


def parse_sas_token(token: str) -> SasTokenClaims:
    """Parse the non-secret claims needed to validate an Azure Relay SAS token."""
    prefix = "SharedAccessSignature "
    raw = token.strip()
    if not raw.startswith(prefix):
        raise RelayTokenError("relay token must start with 'SharedAccessSignature '")
    try:
        parsed = urllib.parse.parse_qs(
            raw[len(prefix) :],
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise RelayTokenError("relay token contains malformed fields") from exc

    required = ("sr", "sig", "se", "skn")
    missing = [
        name for name in required if name not in parsed or len(parsed[name]) != 1 or not parsed[name][0]
    ]
    if missing:
        raise RelayTokenError("relay token missing required fields: " + ", ".join(missing))
    try:
        expires_at = int(parsed["se"][0])
    except ValueError as exc:
        raise RelayTokenError("relay token field 'se' must be a Unix timestamp") from exc
    if expires_at <= 0:
        raise RelayTokenError("relay token field 'se' must be a positive Unix timestamp")
    return {
        "sr": parsed["sr"][0],
        "sig": parsed["sig"][0],
        "se": expires_at,
        "skn": parsed["skn"][0],
    }


def validate_sas_token(
    token: str,
    service_namespace: str,
    entity_path: str,
    *,
    now: float | None = None,
) -> SasTokenClaims:
    """Validate token expiry and that its audience covers the Relay path."""
    claims = parse_sas_token(token)
    current_time = time.time() if now is None else now
    if claims["se"] <= math.floor(current_time):
        raise RelayTokenExpiredError(claims["se"])

    expected = relay_audience(service_namespace, entity_path)
    audience = claims["sr"].rstrip("/")
    if not (expected == audience or expected.startswith(audience + "/")):
        raise RelayTokenAudienceError(f"relay token audience {claims['sr']!r} does not cover {expected!r}")
    return claims


def redact_relay_secrets(text: str) -> str:
    """Remove SAS token values from exception and diagnostic text."""
    redacted = _TOKEN_QUERY_RE.sub(r"\1<redacted>", text)
    return _RAW_TOKEN_RE.sub("SharedAccessSignature <redacted>", redacted)


class RelayCredential:
    """Resolve and validate either key-based or caller-provided Relay tokens."""

    def __init__(
        self,
        *,
        namespace: str,
        path: str,
        keyrule: str | None = None,
        key: str | None = None,
        token: TokenProvider | None = None,
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    ) -> None:
        if not namespace or not path:
            raise ValueError("namespace and path are required")
        if token is not None and (keyrule is not None or key is not None):
            raise ValueError("token is mutually exclusive with keyrule and key")
        if bool(keyrule) != bool(key):
            raise ValueError("keyrule and key must be provided together")
        if token is None and not (keyrule and key):
            raise ValueError("provide token, or both keyrule and key")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")

        self.namespace = fqdn(namespace)
        self.path = path
        self._keyrule = keyrule
        self._key = key
        self._provider = token
        self._ttl_seconds = ttl_seconds

    def resolve(self) -> tuple[str, SasTokenClaims]:
        if self._provider is None:
            assert self._keyrule is not None and self._key is not None
            token = create_sas_token(
                self.namespace,
                self.path,
                self._keyrule,
                self._key,
                self._ttl_seconds,
            )
        else:
            token = self._provider() if callable(self._provider) else self._provider
            if not isinstance(token, str) or not token.strip():
                raise RelayTokenError("relay token provider returned an empty or non-string token")
        return token, validate_sas_token(token, self.namespace, self.path)


def create_listen_url(service_namespace: str, entity_path: str, token: str | None = None) -> str:
    url = f"wss://{service_namespace}/$hc/{entity_path}?sb-hc-action=listen&sb-hc-id=privy"
    if token:
        url += "&sb-hc-token=" + urllib.parse.quote(token)
    return url


def create_http_send_url(service_namespace: str, entity_path: str, token: str | None = None) -> str:
    url = f"https://{service_namespace}/{entity_path}?sb-hc-action=connect&sb-hc-id=privy"
    if token:
        url += "&sb-hc-token=" + urllib.parse.quote(token)
    return url
