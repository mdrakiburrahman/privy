"""End-to-end tests through a real Azure Relay Hybrid Connection."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

from privy import RELAY_RESPONSE_LIMIT_S, CommandSpec, RelayClient, create_sas_token

pytestmark = pytest.mark.e2e


def test_bash_and_python_execution(relay_client: RelayClient):
    marker = uuid.uuid4().hex

    bash = relay_client.run_bash(f"printf {marker}", timeout_s=10)
    python = relay_client.run_python(f"print({marker!r})", timeout_s=10)

    assert bash.ok and bash.stdout == marker
    assert python.ok and python.stdout.strip() == marker


def test_remote_errors_and_timeouts_are_preserved(relay_client: RelayClient):
    failed = relay_client.run_bash("echo before; exit 7", timeout_s=10)
    timed_out = relay_client.run_python("import time; time.sleep(5)", timeout_s=1)

    assert failed.exit_code == 7
    assert failed.stdout == "before\n"
    assert timed_out.timed_out
    assert timed_out.error == "timeout"


def test_async_job_outlives_single_relay_response(relay_client: RelayClient):
    sleep_s = RELAY_RESPONSE_LIMIT_S + 10

    result = relay_client.run_python(
        f"import time; time.sleep({sleep_s}); print('survived')",
        timeout_s=sleep_s + 30,
    )

    assert result.ok
    assert result.stdout == "survived\n"
    assert result.job_id


def test_injected_token_and_callable_provider(
    relay_creds: dict[str, str],
    relay_server,
):
    token = create_sas_token(
        relay_creds["namespace"],
        relay_creds["path"],
        relay_creds["keyrule"],
        relay_creds["key"],
        ttl_seconds=300,
    )
    calls = 0

    def provider() -> str:
        nonlocal calls
        calls += 1
        return token

    client = RelayClient(
        namespace=relay_creds["namespace"],
        path=relay_creds["path"],
        token=provider,
    )

    assert client.run_bash("true", timeout_s=10).ok
    assert client.run_bash("true", timeout_s=10).ok
    assert calls == 2


def test_binary_file_upload_and_download(relay_client: RelayClient, tmp_path):
    payload = os.urandom(2 * 1024 * 1024 + 123)
    local_source = tmp_path / "source.bin"
    remote = tmp_path / "remote.bin"
    local_destination = tmp_path / "destination.bin"
    local_source.write_bytes(payload)

    upload = relay_client.upload_file(local_source, remote)
    download = relay_client.download_file(remote, local_destination)

    assert upload.sha256 == download.sha256
    assert local_destination.read_bytes() == payload


def test_dependency_graph_parallelism_and_skips(relay_client: RelayClient):
    started = time.monotonic()
    result = relay_client.run_many(
        [
            CommandSpec(id="a", kind="bash", code="sleep 1; echo a", timeout_s=10),
            CommandSpec(id="b", kind="bash", code="sleep 1; echo b", timeout_s=10),
            CommandSpec(id="after-a", kind="bash", code="echo c", depends_on=("a",), timeout_s=10),
            CommandSpec(id="fails", kind="bash", code="exit 9", timeout_s=10),
            CommandSpec(
                id="skipped",
                kind="bash",
                code="echo should-not-run",
                depends_on=("fails",),
                timeout_s=10,
            ),
        ],
        max_parallel=4,
    )
    elapsed = time.monotonic() - started
    outcomes = {outcome.id: outcome for outcome in result.outcomes}

    assert elapsed < 4
    assert outcomes["a"].state == "succeeded"
    assert outcomes["b"].state == "succeeded"
    assert outcomes["after-a"].state == "succeeded"
    assert outcomes["fails"].result.exit_code == 9
    assert outcomes["skipped"].state == "skipped"


def test_cli_executes_through_the_relay(relay_server):
    marker = uuid.uuid4().hex
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "privy",
            "client",
            "--bash",
            f"printf {marker}",
            "--timeout-s",
            "10",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == marker
