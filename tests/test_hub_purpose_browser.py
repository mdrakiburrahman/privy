"""Purpose UI checks use only fake listeners and an isolated, ephemeral hub port."""

import threading
import time

import pytest
from hub_fakes import Hub

from privy.hub.app import HubHTTPServer
from privy.hub.purpose import PurposeHistory

browser_api = pytest.importorskip("playwright.sync_api", reason="optional browser checks require playwright")
BOX = "dev/box1_windows"


@pytest.fixture
def workspace(tmp_path):
    hub = Hub(tmp_path)
    now = [time.time() - 5 * 3600]
    store = PurposeHistory(clock=lambda: now[0])
    hub.service.metrics.purposes = store
    for i in range(80):
        now[0] += 120
        store.record(BOX, None if i % 5 == 0 else f"privy: Investigating issue {i}.")
    now[0] += 120
    store.record(BOX, "<script>window.purposeInjected=true</script>")
    now[0] += 120
    store.record(BOX, None)
    now[0] = time.time()
    server = HubHTTPServer(("127.0.0.1", 0), hub.app)
    port = server.server_address[1]
    hub.app.set_port(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
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
            page.goto(f"http://127.0.0.1:{port}/ui/hycos?q=box1_windows")
            yield page, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_hover_scroll_pin_pagination_escape_and_no_row_navigation(workspace):
    page, errors = workspace
    trigger = page.locator(".purpose-trigger").first
    trigger.hover()
    panel = page.locator("#purpose-panel")
    browser_api.expect(panel).to_be_visible()
    browser_api.expect(panel.locator(".purpose-entry")).to_have_count(50)
    panel.hover()
    panel.locator(".purpose-entries").evaluate("(e) => e.scrollTop = 120")
    assert panel.locator(".purpose-entries").evaluate("(e) => e.scrollTop") == 120
    panel.locator("[data-purpose-pin]").click()
    assert "pinned" in panel.get_attribute("class")
    assert "/ui/hycos" in page.url
    panel.locator("[data-purpose-more]").click()
    browser_api.expect(panel.locator(".purpose-entry")).to_have_count(82)
    assert page.evaluate("window.purposeInjected") is None
    page.keyboard.press("Escape")
    browser_api.expect(panel).to_be_hidden()
    assert not errors


def test_keyboard_unknown_gap_and_refresh_stability(workspace):
    page, errors = workspace
    gap = page.locator('.purpose-segment[data-kind="unknown"]').last
    gap.focus()
    page.keyboard.press("Enter")
    panel = page.locator("#purpose-panel")
    browser_api.expect(panel).to_be_visible()
    browser_api.expect(panel.locator(".purpose-selection")).to_contain_text("Unknown")
    browser_api.expect(panel.locator(".purpose-entry")).to_have_count(50)
    panel.locator(".purpose-entries").evaluate("(e) => e.scrollTop = 100")
    page.wait_for_timeout(10500)
    assert panel.locator(".purpose-entries").evaluate("(e) => e.scrollTop") == 100
    assert page.locator("#hycoq").input_value() == "box1_windows"
    assert "/ui/hycos" in page.url and not errors


def test_failure_is_visible_and_retry_recovers(workspace):
    page, errors = workspace
    page.route(
        "**/v1/purposes?**",
        lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error":{"message":"History unavailable"}}'
        ),
    )
    page.locator(".purpose-trigger").first.click()
    panel = page.locator("#purpose-panel")
    browser_api.expect(panel.locator('[role="alert"]')).to_contain_text("History unavailable")
    assert panel.locator(".purpose-entry").count() == 0
    page.unroute("**/v1/purposes?**")
    panel.locator("[data-purpose-refresh]").click()
    browser_api.expect(panel.locator(".purpose-entry")).to_have_count(50)
    assert not errors


def test_dark_narrow_panel_stays_in_viewport(workspace):
    page, errors = workspace
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme="dark")
    page.locator(".purpose-trigger").first.click()
    panel = page.locator("#purpose-panel")
    browser_api.expect(panel).to_be_visible()
    bounds = panel.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 391
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 845
    assert not errors
