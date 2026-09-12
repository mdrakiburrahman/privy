"""Shared fixtures for real Azure Relay end-to-end tests."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator

import pytest

from privy import RelayClient, RelayServer, create_sas_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

REQUIRED_RELAY_VARS = (
    "PRIVY_RELAY_NAMESPACE",
    "PRIVY_RELAY_PATH",
    "PRIVY_RELAY_KEYRULE",
    "PRIVY_RELAY_KEY",
)


def _relay_creds_from_env() -> dict[str, str] | None:
    missing = [name for name in REQUIRED_RELAY_VARS if not os.environ.get(name)]
    if missing:
        if os.environ.get("PRIVY_REQUIRE_E2E") == "1":
            pytest.fail(
                "required E2E relay variables are missing: " + ", ".join(missing),
                pytrace=False,
            )
        return None
    return {
        "namespace": os.environ["PRIVY_RELAY_NAMESPACE"],
        "path": os.environ["PRIVY_RELAY_PATH"],
        "keyrule": os.environ["PRIVY_RELAY_KEYRULE"],
        "key": os.environ["PRIVY_RELAY_KEY"],
    }


@pytest.fixture(scope="session")
def relay_creds() -> dict[str, str]:
    creds = _relay_creds_from_env()
    if creds is None:
        pytest.skip("Set PRIVY_RELAY_NAMESPACE/PATH/KEYRULE/KEY (see .env.template) to run E2E tests.")
    return creds


@pytest.fixture(scope="session")
def relay_server(relay_creds: dict[str, str]) -> Iterator[RelayServer]:
    def listener_token() -> str:
        return create_sas_token(
            relay_creds["namespace"],
            relay_creds["path"],
            relay_creds["keyrule"],
            relay_creds["key"],
            ttl_seconds=10 * 60,
        )

    server = RelayServer(
        namespace=relay_creds["namespace"],
        path=relay_creds["path"],
        token=listener_token,
    )
    thread = threading.Thread(target=server.serve_forever, name="privy-server-test", daemon=True)
    thread.start()
    try:
        assert server.wait_until_listening(timeout=30), "server never reported listening"
        yield server
    finally:
        server.stop()
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def relay_client(
    relay_creds: dict[str, str],
    relay_server: RelayServer,
) -> RelayClient:
    return RelayClient(**relay_creds)
