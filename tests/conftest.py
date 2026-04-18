"""Shared pytest fixtures.

The end-to-end tests require real Azure Relay credentials. Populate them by
copying ``.env.template`` → ``.env`` and running::

    set -a; source .env; set +a
    uv run pytest

If any of the ``PRIVY_RELAY_*`` variables are missing, the e2e tests are
skipped.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator

import pytest

from privy import RelayClient, RelayServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

REQUIRED_VARS = (
    "PRIVY_RELAY_NAMESPACE",
    "PRIVY_RELAY_PATH",
    "PRIVY_RELAY_KEYRULE",
    "PRIVY_RELAY_KEY",
)


def _relay_creds_from_env() -> dict[str, str] | None:
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
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
        pytest.skip("Set PRIVY_RELAY_NAMESPACE/PATH/KEYRULE/KEY (see .env.template) to run e2e tests.")
    return creds


@pytest.fixture(scope="session")
def relay_server(relay_creds: dict[str, str]) -> Iterator[RelayServer]:
    server = RelayServer(**relay_creds)
    thread = threading.Thread(target=server.serve_forever, name="privy-server-test", daemon=True)
    thread.start()
    try:
        assert server.wait_until_listening(timeout=30), "server never reported listening"
        yield server
    finally:
        server.stop()
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def relay_client(relay_creds: dict[str, str], relay_server: RelayServer) -> RelayClient:
    return RelayClient(**relay_creds)
