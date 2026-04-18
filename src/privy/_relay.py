"""SAS token + URL helpers for Azure Relay Hybrid Connections.

Ported from the reference implementation in `.temp/relay-demo/common/relay.py`.
Pure functions — no file or environment reads.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import time
import urllib.parse


def _hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key=key, msg=msg, digestmod=hashlib.sha256).digest()


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


def create_sas_token(
    service_namespace: str,
    entity_path: str,
    sas_key_name: str,
    sas_key: str,
    ttl_seconds: int = 60 * 60 * 48,
) -> str:
    uri = f"http://{service_namespace}/{entity_path}"
    encoded_uri = urllib.parse.quote(uri, safe="")
    expiry = math.floor(time.time()) + ttl_seconds
    signature = f"{encoded_uri}\n{expiry}".encode()
    digest = _hmac_sha256(sas_key.encode("utf-8"), signature)
    sig = urllib.parse.quote(base64.b64encode(digest))
    return f"SharedAccessSignature sr={encoded_uri}&sig={sig}&se={expiry}&skn={sas_key_name}"


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
