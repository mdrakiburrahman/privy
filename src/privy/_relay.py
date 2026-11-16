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
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class RelayConnectionString:
    namespace: str
    key_name: str
    key: str = field(repr=False)
    entity_path: str | None = None


def parse_relay_connection_string(value: str) -> RelayConnectionString:
    """Parse a key-based Azure Relay connection string without reflecting its secrets."""
    if not isinstance(value, str) or not value.strip() or len(value) > 16384:
        raise ValueError("Provide a key-based Relay connection string.")
    fields: dict[str, str] = {}
    allowed = {"endpoint", "sharedaccesskeyname", "sharedaccesskey", "entitypath"}
    for part in value.strip().rstrip(";").split(";"):
        name, separator, content = part.partition("=")
        name, content = name.strip().lower(), content.strip()
        if not separator or name not in allowed or name in fields or not content:
            raise ValueError("Connection string contains missing, duplicate, or unsupported fields.")
        fields[name] = content
    if not {"endpoint", "sharedaccesskeyname", "sharedaccesskey"} <= fields.keys():
        raise ValueError("Connection string requires Endpoint, SharedAccessKeyName, and SharedAccessKey.")
    try:
        endpoint = urllib.parse.urlsplit(fields["endpoint"])
        valid = (
            endpoint.scheme == "sb"
            and endpoint.hostname is not None
            and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*\.servicebus\.windows\.net", endpoint.netloc)
            and endpoint.path in ("", "/")
            and not endpoint.query
            and not endpoint.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Endpoint must be sb://<namespace>.servicebus.windows.net/.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", fields["sharedaccesskeyname"]):
        raise ValueError("SharedAccessKeyName must be an authorization policy name.")
    try:
        decoded_key = base64.b64decode(fields["sharedaccesskey"], validate=True)
    except ValueError:
        raise ValueError("SharedAccessKey must be a non-empty base64 key.") from None
    if not decoded_key:
        raise ValueError("SharedAccessKey must be a non-empty base64 key.")
    return RelayConnectionString(
        namespace=endpoint.hostname,
        key_name=fields["sharedaccesskeyname"],
        key=fields["sharedaccesskey"],
        entity_path=fields.get("entitypath"),
    )


def mint_relay_sas(
    connection_string: str, entity_path: str | None = None, ttl_seconds: int = 21600
) -> dict[str, str | int]:
    """Mint one entity-scoped token and its HTTPS sender URL, without retaining credentials."""
    connection = parse_relay_connection_string(connection_string)
    if entity_path is not None and not isinstance(entity_path, str):
        raise ValueError("Hybrid Connection name must be text.")
    supplied = entity_path.strip() if entity_path else None
    path = connection.entity_path or supplied
    if supplied and connection.entity_path and supplied != connection.entity_path:
        raise ValueError("Hybrid Connection name conflicts with EntityPath.")
    if not path or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path):
        raise ValueError(
            "Provide a specific Hybrid Connection name using letters, numbers, '.', '_', '-' or '/'."
        )
    if any(segment in (".", "..") for segment in path.split("/")):
        raise ValueError("Hybrid Connection name cannot contain relative path segments.")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise ValueError("Lifetime must be a positive whole number of seconds.")
    # Keep the expiry representable in the returned UTC timestamp on all supported platforms.
    if ttl_seconds > 253402300799 - math.floor(time.time()):
        raise ValueError("Lifetime exceeds the supported expiry timestamp.")
    token = create_sas_token(connection.namespace, path, connection.key_name, connection.key, ttl_seconds)
    expires_at = parse_sas_token(token)["se"]
    endpoint = f"https://{connection.namespace}/{path}"
    return {
        "token": token,
        "sender_url": endpoint + "?" + urllib.parse.urlencode({"sb-hc-token": token}),
        "endpoint": endpoint,
        "expires_at": expires_at,
        "expires_utc": _format_utc(expires_at),
    }
