"""Lease dashboard rendering uses the same public store as the advisory API."""

import html
import json
import re
import time

import pytest
from hub_fakes import FakeListener, Hub, relays_document, write_relays

from privy.hub import dashboard
from privy.hub.metrics import HubMetrics

BOX = "dev/box1_windows"
WSL = "dev/box1_wsl"
CONTAINER = "dev/box1_devcontainer"


@pytest.fixture
def leased_hub(tmp_path):
    hub = Hub(
        tmp_path,
        {
            BOX: FakeListener(hostname="BOX1"),
            WSL: FakeListener(hostname="WSL1"),
            CONTAINER: FakeListener(hostname="CONTAINER1"),
            "dev/box2_windows": FakeListener(hostname="BOX2"),
        },
    )
    document = relays_document()
    document["relays"] = [document["relays"][0]]
    document["relays"][0]["hybrid_connections"] = [
        {"path": target.split("/", 1)[1], "keyrule": "all", "key": "a2V5MQ=="}
        for target in (BOX, WSL, CONTAINER, "dev/box2_windows")
    ]
    write_relays(hub.relays_file, document)
    hub.call("GET", "/ui/hycos")
    client = hub.service.leases.register_client(
        "Lease <script>holder</script>", kind="copilot-app", host="LOCAL", session_id="session-123"
    )
    lease = hub.service.leases.acquire(
        client["client_id"],
        target=BOX,
        labels={"repo": "owner/repo", "task": "<script>"},
        purpose="privy: <script>working</script>",
    )
    return hub, client, lease


def test_hycos_show_whole_group_lease_and_filter_with_escaped_metadata(leased_hub):
    hub, _, lease = leased_hub
    response = hub.call("GET", "/ui/hycos?lease_state=leased")
    assert response.status == 200
    text = response.text
    assert 'id="hycolease"' in text and '<th data-sort>Lease<span class="s"></span></th>' in text
    assert text.count('data-lease-state="leased"') == 3
    assert text.count('data-lease-state="free"') == 1
    assert text.count(f"lease {lease['lease_id']} · group dev/box1") == 3
    assert "repo=owner/repo" in text and "task=&lt;script&gt;" in text
    assert "Lease &lt;script&gt;holder&lt;/script&gt;" in text
    assert "<script>holder</script>" not in text
    assert "idle TTL 30m, renews on use" in text
    assert " style=" not in text and "unsafe-inline" not in response.headers["Content-Security-Policy"]


def test_leases_tab_clients_groups_peek_and_effective_rules_are_read_only(leased_hub):
    hub, client, lease = leased_hub
    before = hub.service.leases.get_lease(lease["lease_id"])["expires_at"]
    response = hub.call("GET", f"/ui/leases?peek={lease['lease_id']}")
    assert response.status == 200
    text = response.text
    for key in ("leases", "lease-clients", "lease-groups", "lease-peek", "lease-rules"):
        assert f'id="s-{key}"' in text
    assert client["client_id"] in text and "session-123" in text
    assert "acquired" in text and "event history · read-only" in text
    assert "version: 1" in text and "cooldown: 15m" in text and "max_leases_per_client: 4" in text
    assert "In-memory defaults" in text
    assert f'data-force-release="{lease["lease_id"]}"' in text
    assert f"/ui/jobs?lease={lease['lease_id']}" in text
    assert f"/ui/logs?target=%2A&amp;lease={lease['lease_id']}" in text
    assert hub.service.leases.get_lease(lease["lease_id"])["expires_at"] == before
    assert hub.call("POST", "/ui/leases").status == 405


def test_lease_history_state_links_and_unknown_peek(leased_hub):
    hub, client, lease = leased_hub
    hub.service.leases.release(lease["lease_id"], client["client_id"])
    active = hub.call("GET", "/ui/leases").text
    released = hub.call("GET", "/ui/leases?state=released&q=holder&label=repo%3Downer%2Frepo").text
    assert f'data-force-release="{lease["lease_id"]}"' not in active
    assert 'class="lease-state released"' in released
    assert 'value="repo=owner/repo"' in released
    assert "state=all&amp;q=holder&amp;label=repo%3Downer%2Frepo" in released
    assert hub.call("GET", "/ui/leases?peek=unknown").status == 200
    assert "This lease no longer exists." in hub.call("GET", "/ui/leases?peek=unknown").text


def test_expired_and_pinned_lease_rendering(leased_hub):
    hub, _, lease = leased_hub
    store = hub.service.leases
    clock = time.time()
    store._clock = lambda: clock + 1801
    page = hub.call("GET", "/ui/leases?state=expired").text
    assert 'class="lease-state expired"' in page
    assert 'data-force-release="' not in re.search(r"<main[^>]*>([\s\S]*?)</main>", page)[1]
    timer = dashboard.lease_timer({**lease, "running_jobs": ["j-one"]}, clock + 9999)
    assert "pinned: 1 job running" in timer
    assert "expires 30m after the last job ends" in timer
    assert "in 0m" not in timer


def test_logs_and_jobs_filter_by_lease_and_preserve_it_in_navigation(leased_hub):
    hub, _, lease = leased_hub
    first = hub.call(
        "POST", "/v1/jobs", {"target": BOX, "code": "leased-command", "lease_id": lease["lease_id"]}
    )
    second = hub.call("POST", "/v1/jobs", {"target": BOX, "code": "human-command"})
    assert first.status == second.status == 200
    deadline = time.monotonic() + 5
    while not all(hub.jobs.get(job.payload["job_id"]).done for job in (first, second)):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    jobs = hub.call("GET", f"/ui/jobs?lease={lease['lease_id']}").text
    logs = hub.call("GET", f"/ui/logs?lease={lease['lease_id']}").text
    for text in (jobs, logs):
        assert "leased-command" in text and "human-command" not in text
        assert "Clear lease filter" in text
        assert f"lease={lease['lease_id']}&amp;range=7d" in text
    assert f"state=failed&amp;lease={lease['lease_id']}" in jobs
    assert f"target=dev%2Fbox1_windows&amp;lease={lease['lease_id']}" in logs
    assert "No jobs match." in hub.call("GET", "/ui/jobs?lease=unknown").text
    restored = HubMetrics()
    restored.restore(hub.audit_path)
    assert restored.entry(first.payload["job_id"]).view()["lease_id"] == lease["lease_id"]


def test_remote_exec_target_payload_exposes_holder_without_acquiring_a_lease(tmp_path):
    info = {
        "hostname": "BOX1",
        "platform": {"system": "Windows"},
        "capabilities": {"terminal": {"version": 1, "shells": ["powershell"]}},
        "runtimes": {"powershell": "pwsh"},
    }
    hub = Hub(tmp_path, {BOX: FakeListener(info=info)})
    hub.call("GET", "/ui/hycos")
    client = hub.service.leases.register_client("Other session", session_id="other-session")
    lease = hub.service.leases.acquire(client["client_id"], target=BOX, purpose="privy: Other work.")
    response = hub.call("GET", "/ui/exec")
    targets = json.loads(html.unescape(re.search(r'data-targets="([^"]*)"', response.text)[1]))
    assert targets[0]["lease"]["lease_id"] == lease["lease_id"]
    assert targets[0]["lease"]["holder"]["session_id"] == "other-session"
    assert "Open terminal" in response.text and "Your clicks run without a lease" in response.text
    assert not hub.listeners[BOX].terminal_requests
