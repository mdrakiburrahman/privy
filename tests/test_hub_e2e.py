"""End-to-end hub tests through a real Azure Relay Hybrid Connection."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import uuid
from types import SimpleNamespace

import pytest
import requests

from privy import __version__
from privy.hub.app import build_hub
from privy.hub.home import read_token

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def hub(relay_creds, relay_server, tmp_path_factory):
    home = tmp_path_factory.mktemp("hub")
    missing_path = f"privy-hub-missing-{uuid.uuid4().hex[:8]}"
    credential = {"keyrule": relay_creds["keyrule"], "key": relay_creds["key"]}
    document = {
        "version": 1,
        "relays": [
            {
                "name": "gci",
                "namespace": relay_creds["namespace"],
                "hybrid_connections": [
                    {"path": relay_creds["path"], **credential},
                    {"path": missing_path, **credential},
                ],
            }
        ],
    }
    (home / "relays.json").write_text(json.dumps(document), encoding="utf-8")
    runtime = build_hub(home=home, port=0, version=__version__)
    thread = threading.Thread(target=runtime.serve_forever, name="privy-hub-e2e", daemon=True)
    thread.start()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {read_token(home)}"
    try:
        yield SimpleNamespace(
            url=runtime.url,
            session=session,
            target=f"gci/{relay_creds['path']}",
            missing=f"gci/{missing_path}",
            home=home,
        )
    finally:
        runtime.shutdown()
        thread.join(10)


def _post(hub, path, body):
    response = hub.session.post(f"{hub.url}{path}", json=body, timeout=180)
    assert response.status_code == 200, response.text
    return response.json()


def _get(hub, path, **params):
    response = hub.session.get(f"{hub.url}{path}", params=params, timeout=180)
    assert response.status_code == 200, response.text
    return response.json()


def test_hub_probes_the_live_listener_and_a_missing_hybrid_connection(hub):
    payload = _get(hub, "/v1/targets", refresh="true")

    by_id = {target["id"]: target for target in payload["targets"]}
    live = by_id[hub.target]
    assert live["status"] == "online"
    assert live["listener"]["hostname"] == socket.gethostname()
    assert live["listener"]["privy_version"] == __version__
    assert by_id[hub.missing]["status"] == "not_found"

    detail = _get(hub, f"/v1/targets/{hub.target}")
    assert detail["listener_info"]["pid"] == os.getpid()


def test_hub_runs_python_and_bash_through_the_relay(hub):
    marker = uuid.uuid4().hex

    python = _post(
        hub,
        "/v1/jobs",
        {"target": hub.target, "language": "python", "code": f"print({marker!r})", "wait_s": 60},
    )
    bash = _post(
        hub, "/v1/jobs", {"target": hub.target, "language": "bash", "code": f"printf {marker}", "wait_s": 60}
    )

    assert python["state"] == "succeeded" and python["stdout"].strip() == marker
    assert bash["state"] == "succeeded" and bash["stdout"] == marker
    assert python["remote_job_id"]


def test_hub_returns_running_jobs_and_collects_them_later(hub):
    code = "import time\nprint('first', flush=True)\ntime.sleep(4)\nprint('second')"

    started = _post(hub, "/v1/jobs", {"target": hub.target, "language": "python", "code": code, "wait_s": 1})
    assert started["state"] in ("queued", "running") and started["next_step"]

    finished = _get(hub, f"/v1/jobs/{started['job_id']}", wait_s=60)
    assert finished["state"] == "succeeded"
    assert finished["stdout"].splitlines() == ["first", "second"]
    page = _get(hub, f"/v1/jobs/{started['job_id']}/output", offset=0, limit=5)
    assert page["data"] == "first"


def test_hub_cancels_a_remote_job(hub):
    started = _post(
        hub,
        "/v1/jobs",
        {"target": hub.target, "language": "python", "code": "import time; time.sleep(120)", "wait_s": 2},
    )

    cancelled = _post(hub, f"/v1/jobs/{started['job_id']}/cancel", {})

    assert cancelled["state"] == "cancelled"


def test_hub_copies_files_both_ways(hub, tmp_path):
    payload = os.urandom(300_000)
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    remote = tmp_path / "remote.bin"
    returned = tmp_path / "returned.bin"

    upload = _post(
        hub,
        "/v1/files/upload",
        {"target": hub.target, "local_path": str(source), "remote_path": str(remote), "wait_s": 60},
    )
    download = _post(
        hub,
        "/v1/files/download",
        {"target": hub.target, "remote_path": str(remote), "local_path": str(returned), "wait_s": 60},
    )

    digest = hashlib.sha256(payload).hexdigest()
    assert upload["state"] == "succeeded" and upload["result"]["sha256"] == digest
    assert download["state"] == "succeeded" and returned.read_bytes() == payload


def test_hub_broadcast_skips_missing_targets(hub):
    marker = uuid.uuid4().hex

    payload = _post(
        hub,
        "/v1/broadcasts",
        {"targets": ["gci/*"], "language": "python", "code": f"print({marker!r})", "wait_s": 60},
    )

    assert payload["done"] is True
    assert [job["target"] for job in payload["jobs"]] == [hub.target]
    assert payload["jobs"][0]["stdout"].strip() == marker
    assert payload["skipped"][0]["target"] == hub.missing
    assert payload["skipped"][0]["status"] == "not_found"


def test_mcp_tools_call_runs_code_on_the_listener(hub):
    marker = uuid.uuid4().hex
    initialize = _post(
        hub,
        "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
    )
    assert initialize["result"]["serverInfo"]["name"] == "privy-hub"

    response = hub.session.post(
        f"{hub.url}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "run",
                "arguments": {"target": hub.target, "language": "python", "code": f"print({marker!r})"},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "e2e", "version": "1"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
        headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "run"},
        timeout=180,
    )

    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["stdout"].strip() == marker
