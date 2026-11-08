"""Lease UI behavior in Chromium with a throwaway hub and stub terminal."""

import base64
import threading
import time

import pytest
from hub_fakes import FakeListener, Hub, relays_document, write_relays

from privy.hub.app import HubHTTPServer

browser_api = pytest.importorskip("playwright.sync_api", reason="optional browser checks require playwright")
BOX = "dev/box1_windows"
WSL = "dev/box1_wsl"
CONTAINER = "dev/box1_devcontainer"


class LeaseTerminal:
    def __init__(self):
        self.calls = []
        self.seq = 0
        self.closed = False

    def __call__(self, payload):
        self.calls.append(payload)
        action = payload["action"]
        if action == "open":
            self.seq = 0
            self.closed = False
        elif action == "write":
            assert payload["seq"] == self.seq
            self.seq += 1
        elif action == "close":
            self.closed = True
        elif action == "read":
            time.sleep(0.02)
        return {
            "kind": "terminal",
            "protocol_version": 1,
            "ok": True,
            "session_id": "lease-test-remote",
            "state": "closed" if self.closed else "running",
            "next_seq": self.seq,
            "data_b64": "",
            "offset": payload.get("offset", 0),
            "start_offset": 0,
            "expires_in_s": 3600,
            "exit_code": None,
        }


@pytest.fixture
def lease_browser(tmp_path):
    terminal = LeaseTerminal()
    info = {
        "hostname": "lease-test-host",
        "platform": {"system": "Windows"},
        "capabilities": {"terminal": {"version": 1, "shells": ["powershell"], "idle_ttl_s": 3600}},
        "runtimes": {"powershell": "pwsh"},
    }
    hub = Hub(
        tmp_path,
        {
            BOX: FakeListener(info=info, terminal=terminal),
            WSL: FakeListener(hostname="WSL"),
            CONTAINER: FakeListener(hostname="CONTAINER"),
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
        "Other <script>session</script>", kind="copilot-app", host="test-host", session_id="session-other"
    )
    lease = hub.service.leases.acquire(
        client["client_id"],
        target=BOX,
        labels={"repo": "owner/repo", "task": "<script>"},
        purpose="privy: Testing lease dashboard.",
    )
    server = HubHTTPServer(("127.0.0.1", 0), hub.app)
    hub.app.set_port(server.server_address[1])
    base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1500, "height": 1100})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: (
                    errors.append(message.text)
                    if message.type == "error" and "Content Security Policy" in message.text
                    else None
                ),
            )
            yield page, hub, lease, terminal, base, errors
            browser.close()
    finally:
        hub.app.rest.terminals.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_hyco_lease_state_filter_and_url_survive_refresh(lease_browser):
    page, _, _, _, base, errors = lease_browser
    page.goto(base + "/ui/hycos")
    page.locator("#hycolease").select_option("leased")
    browser_api.expect(page.locator("#hycos tbody tr:visible")).to_have_count(3)
    assert "lease_state=leased" in page.url
    page.reload()
    browser_api.expect(page.locator("#hycolease")).to_have_value("leased")
    browser_api.expect(page.locator("#hycos tbody tr:visible")).to_have_count(3)
    page.locator("#hycolease").select_option("free")
    browser_api.expect(page.locator("#hycos tbody tr:visible")).to_have_count(1)
    assert not errors


def test_hyco_search_matches_own_layer_not_sibling_lease_members(lease_browser):
    page, _, _, _, base, errors = lease_browser
    page.goto(base + "/ui/hycos?q=wsl")
    browser_api.expect(page.locator("#hycos tbody tr:visible")).to_have_count(1)
    browser_api.expect(page.locator("#hycos tbody tr:visible")).to_contain_text("box1_wsl")
    page.locator("#hycoq").fill("_windows")
    browser_api.expect(page.locator("#hycos tbody tr:visible")).to_have_count(2)
    page.locator("#hycoq").fill("wsl")
    page.locator("#topchips").get_by_role("link", name="7d", exact=True).click()
    browser_api.expect(page.locator("#hycoq")).to_have_value("wsl")
    browser_api.expect(page.locator("#hycos tbody tr:visible")).to_have_count(1)
    page.locator("#hycoq").fill("does-not-exist")
    browser_api.expect(page.locator("#hyconone")).to_be_visible()
    page.locator("#hycoq").fill("")
    browser_api.expect(page.locator("#hycos tbody tr:visible")).to_have_count(4)
    assert not errors


def test_hyco_facet_filters_chips_counts_columns_and_url(lease_browser):
    page, _, _, _, base, errors = lease_browser
    page.goto(base + "/ui/hycos")
    rows = page.locator("#hycos tbody tr:visible")
    pop = page.locator("#facet-pop")
    page.locator('.facet-pill[data-facet="environment"]').click()
    browser_api.expect(pop.locator("label", has_text="Windows host")).to_contain_text("2")
    pop.locator("label", has_text="WSL").locator("input").check()
    browser_api.expect(rows).to_have_count(1)
    pop.locator("label", has_text="Devcontainer").locator("input").check()
    browser_api.expect(rows).to_have_count(2)
    page.keyboard.press("Escape")
    browser_api.expect(pop).to_be_hidden()
    assert "environment=WSL&environment=Devcontainer" in page.url
    browser_api.expect(page.locator(".facet-chip")).to_have_count(2)

    page.locator('.facet-pill[data-facet="os"]').click()
    browser_api.expect(pop.locator("label", has_text="Windows")).to_contain_text("2")
    page.locator("h1").click()
    browser_api.expect(pop).to_be_hidden()
    page.locator(".facet-chip", has_text="WSL").click()
    browser_api.expect(rows).to_have_count(1)
    page.reload()
    browser_api.expect(page.locator('.facet-pill[data-facet="environment"]')).to_have_text(
        "Environment: Devcontainer"
    )
    browser_api.expect(rows).to_contain_text("box1_devcontainer")
    page.get_by_role("button", name="Clear all").click()
    browser_api.expect(rows).to_have_count(4)

    arch = page.locator("#hycos th.col-arch")
    browser_api.expect(arch).to_be_hidden()
    page.locator("#hycocols").click()
    pop.locator("label", has_text="Arch").locator("input").check()
    browser_api.expect(arch).to_be_visible()
    page.reload()
    browser_api.expect(arch).to_be_visible()
    browser_api.expect(page.locator("#hycos td.col-arch", has_text="x86_64").first).to_be_visible()

    page.goto(base + "/ui/hycos?privy_version=0.2.*&environment=WSL,Devcontainer")
    browser_api.expect(rows).to_have_count(2)
    page.locator('.facet-pill[data-facet="privy_version"]').click()
    browser_api.expect(pop.locator("label", has_text="0.2.*").locator("small")).to_have_text("2")
    assert not errors


def test_take_manual_lease_with_duration_picker_reload_and_release(lease_browser):
    page, hub, _, _, base, errors = lease_browser
    page.goto(base + "/ui/leases")
    page.get_by_role("button", name="Take lease", exact=True).click()
    page.locator("#lease-group").select_option("dev/box2")
    page.locator("#lease-holder").fill("Manual operator")
    page.locator("#lease-purpose").fill("Reserved for my work")
    page.locator("#lease-hours").fill("35")
    page.locator("#lease-minutes").fill("59")
    page.get_by_role("button", name="Increase minutes", exact=True).click()
    browser_api.expect(page.locator("#lease-duration")).to_have_text("36:00")
    page.locator("#lease-create").get_by_role("button", name="Take lease", exact=True).click()
    browser_api.expect(page.locator("#s-lease-peek")).to_contain_text("Reserved for my work")
    manual = next(item for item in hub.service.leases.list_leases() if item.get("manual"))
    assert manual["ttl_s"] == 36 * 3600 and manual["holder"]["name"] == "Manual operator"
    before = manual["expires_at"]
    page.reload()
    assert hub.service.leases.get_lease(manual["lease_id"])["expires_at"] == before
    page.get_by_role("button", name="Release", exact=True).click()
    page.locator(".lease-dialog").get_by_role("button", name="Release", exact=True).click()
    browser_api.expect(page.locator("[data-owner-release]:visible")).to_have_count(0)
    assert hub.service.leases.get_lease(manual["lease_id"])["state"] == "released"
    assert not errors


def test_take_lease_validation_and_conflict_preserve_draft(lease_browser):
    page, _, _, _, base, errors = lease_browser
    page.goto(base + "/ui/leases")
    page.get_by_role("button", name="Take lease", exact=True).click()
    page.locator("#lease-group").select_option("dev/box2")
    page.locator("#lease-purpose").fill("Keep this draft")
    page.locator("#lease-hours").fill("0")
    page.locator("#lease-minutes").fill("0")
    page.locator("#lease-create").get_by_role("button", name="Take lease", exact=True).click()
    assert not page.locator("#lease-minutes").evaluate("(e) => e.checkValidity()")
    page.locator("#lease-hours").fill("8")
    page.route(
        "**/v1/leases",
        lambda route: route.fulfill(
            status=409,
            content_type="application/json",
            body='{"error":{"code":"group_leased","message":"Group was just reserved by another client"}}',
        ),
    )
    page.locator("#lease-create").get_by_role("button", name="Take lease", exact=True).click()
    browser_api.expect(page.locator("#lease-create-error")).to_contain_text("just reserved")
    browser_api.expect(page.locator("#lease-purpose")).to_have_value("Keep this draft")
    page.get_by_role("button", name="Cancel", exact=True).click()
    browser_api.expect(page.locator("#lease-create")).to_have_count(0)
    assert not errors


def test_lease_search_label_client_group_filters_and_peek(lease_browser):
    page, _, lease, _, base, errors = lease_browser
    page.goto(base + "/ui/leases")
    page.locator("#leaselabel").fill("repo=owner/repo task=<script>")
    browser_api.expect(page.locator("#leases tbody tr:visible")).to_have_count(1)
    browser_api.expect(page.locator("#lease-clients tbody tr:visible")).to_have_count(1)
    browser_api.expect(page.locator("#lease-groups tbody tr:visible")).to_have_count(1)
    page.locator("#leaseq").fill("session-other")
    browser_api.expect(page.locator("#leases tbody tr:visible")).to_have_count(1)
    page.locator("#leaselabel").fill("repo=other")
    browser_api.expect(page.locator("#leasenone")).to_be_visible()
    page.locator("#leaselabel").fill("")
    page.locator("#leases a").filter(has_text="Peek").first.click()
    browser_api.expect(page.locator("#s-lease-peek")).to_contain_text("acquired")
    browser_api.expect(page.locator("#s-lease-peek")).to_contain_text(lease["lease_id"])
    assert not page.locator("#main script").count() and not errors


def test_force_release_requires_confirmation_and_keeps_running_terminal(lease_browser):
    page, hub, lease, terminal, base, errors = lease_browser
    page.goto(base + f"/ui/exec?target={BOX}")
    page.get_by_role("button", name="Open terminal anyway", exact=True).click()
    browser_api.expect(page.locator("#exec-state")).to_have_text("Connected")
    dashboard_page = page.context.new_page()
    dashboard_page.goto(base + "/ui/leases")
    dashboard_page.locator("[data-force-release]").click()
    dashboard_page.get_by_role("button", name="Cancel", exact=True).click()
    assert hub.service.leases.get_lease(lease["lease_id"])["state"] == "active"
    dashboard_page.locator("[data-force-release]").click()
    browser_api.expect(dashboard_page.locator(".lease-dialog")).to_contain_text(
        "remote work may still be running"
    )
    with dashboard_page.expect_response(lambda response: response.url.endswith("/force-release")) as released:
        dashboard_page.locator(".lease-dialog").get_by_role(
            "button", name="Force release", exact=True
        ).click()
    assert released.value.status == 200
    assert hub.service.leases.get_lease(lease["lease_id"])["state"] == "released"
    assert not terminal.closed
    assert not any(call["action"] == "close" for call in terminal.calls)
    browser_api.expect(page.locator("#exec-state")).to_have_text("Connected")
    browser_api.expect(dashboard_page.locator("[data-force-release]")).to_have_count(0)
    dashboard_page.close()
    assert not errors


def test_human_open_and_run_warn_but_stay_unleased_and_cancel_preserves_draft(lease_browser):
    page, hub, lease, terminal, base, errors = lease_browser
    before = hub.service.leases.get_lease(lease["lease_id"])["expires_at"]
    page.goto(base + f"/ui/exec?target={BOX}")
    modal = page.locator(".lease-dialog")
    browser_api.expect(modal).to_be_visible()
    for text in (
        "Other <script>session</script>",
        "session-other",
        "dev/box1",
        "owner/repo",
        "Testing lease dashboard",
    ):
        browser_api.expect(modal).to_contain_text(text)
    assert not page.locator(".lease-dialog script").count()
    assert not any(call["action"] == "open" for call in terminal.calls)
    page.get_by_role("button", name="Cancel", exact=True).click()
    browser_api.expect(page.locator("#exec-connect")).to_be_enabled()
    page.locator("#exec-connect").click()
    page.get_by_role("button", name="Open terminal anyway", exact=True).click()
    browser_api.expect(page.locator("#exec-state")).to_have_text("Connected")
    page.locator("#exec-command").fill("Write-Output 'human command'")
    page.locator("#exec-run").click()
    browser_api.expect(modal).to_be_visible()
    page.keyboard.press("Escape")
    browser_api.expect(page.locator("#exec-command")).to_have_value("Write-Output 'human command'")
    browser_api.expect(page.locator("#exec-run")).to_be_enabled()
    assert not any(call["action"] == "write" for call in terminal.calls)
    page.locator("#exec-command").press("Control+Enter")
    page.get_by_role("button", name="Run anyway", exact=True).click()
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    written = [call for call in terminal.calls if call["action"] == "write"]
    assert len(written) == 1
    assert "human command" in base64.b64decode(written[0]["data_b64"]).decode()
    assert all("lease_id" not in call for call in terminal.calls)
    after = hub.service.leases.get_lease(lease["lease_id"])
    assert after["expires_at"] == before and after["client_id"] == lease["client_id"]
    assert not errors


def test_force_release_api_failure_remains_visible_and_preserves_lease(lease_browser):
    page, hub, lease, _, base, errors = lease_browser
    page.goto(base + "/ui/leases")
    page.route(
        "**/force-release",
        lambda route: route.fulfill(
            status=403, content_type="application/json", body='{"error":{"message":"Origin rejected"}}'
        ),
    )
    page.locator("[data-force-release]").click()
    page.locator(".lease-dialog").get_by_role("button", name="Force release", exact=True).click()
    browser_api.expect(page.locator("#lease-action-error")).to_have_text("Origin rejected")
    assert hub.service.leases.get_lease(lease["lease_id"])["state"] == "active"
    browser_api.expect(page.locator("[data-force-release]")).to_be_enabled()
    assert not errors


def test_lookup_failure_is_advisory_and_uses_rendered_lease_warning(lease_browser):
    page, _, _, terminal, base, errors = lease_browser
    page.route("**/v1/targets", lambda route: route.abort())
    page.goto(base + "/ui/exec")
    page.locator("#exec-connect").click()
    browser_api.expect(page.locator(".lease-dialog")).to_be_visible()
    page.get_by_role("button", name="Open terminal anyway", exact=True).click()
    browser_api.expect(page.locator("#exec-state")).to_have_text("Connected")
    assert any(call["action"] == "open" for call in terminal.calls)
    assert not errors
