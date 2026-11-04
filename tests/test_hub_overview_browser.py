from test_hub_leases_browser import browser_api
from test_hub_leases_browser import lease_browser as lease_browser


def test_overview_range_resolution_and_reload(lease_browser):
    page, _, _, _, base, errors = lease_browser
    page.goto(base + "/ui")
    page.locator("#metrics-range").select_option("30d")
    page.locator("#metrics-resolution").select_option("1m")
    page.get_by_role("button", name="Apply", exact=True).click()
    browser_api.expect(page.locator("#metrics-effective")).to_contain_text("Adjusted")
    assert "range=30d" in page.url and "resolution=1m" in page.url
    page.reload()
    browser_api.expect(page.locator("#metrics-range")).to_have_value("30d")
    browser_api.expect(page.locator("#metrics-resolution")).to_have_value("1m")
    assert "resolution=1m" in page.get_by_role("link", name="Stats JSON").get_attribute("href")
    page.locator("#metrics-range").select_option("all")
    page.get_by_role("button", name="Apply", exact=True).click()
    browser_api.expect(page.locator("#main")).to_contain_text("all retained history")
    assert not errors


def test_overview_custom_dates_validate_and_survive_refresh(lease_browser):
    page, _, _, _, base, errors = lease_browser
    page.goto(base + "/ui")
    page.locator("#metrics-range").select_option("custom")
    page.locator("#metrics-start").fill("2026-01-02T00:00")
    page.locator("#metrics-end").fill("2026-01-01T00:00")
    page.get_by_role("button", name="Apply", exact=True).click()
    browser_api.expect(page.locator("#metrics-error")).to_contain_text("before")
    page.locator("#metrics-end").fill("2026-01-03T00:00")
    page.get_by_role("button", name="Apply", exact=True).click()
    assert "range=custom" in page.url and "start=" in page.url and "end=" in page.url
    page.reload()
    browser_api.expect(page.locator("#metrics-start")).to_have_value("2026-01-02T00:00")
    browser_api.expect(page.locator("#metrics-end")).to_have_value("2026-01-03T00:00")
    assert not errors


def test_custom_dates_use_browser_timezone_and_preserve_interval(lease_browser):
    page, _, _, _, base, _ = lease_browser
    context = page.context.browser.new_context(timezone_id="Pacific/Honolulu")
    try:
        page = context.new_page()
        page.goto(base + "/ui?range=custom&start=2026-01-02T00:00:00Z&end=2026-01-03T00:00:00Z")
        browser_api.expect(page.locator("#metrics-start")).to_have_value("2026-01-01T14:00")
        browser_api.expect(page.locator("#metrics-end")).to_have_value("2026-01-02T14:00")
        page.get_by_role("button", name="Apply", exact=True).click()
        browser_api.expect(page.locator("#metrics-effective")).to_contain_text("2026-01-02T00:00:00.000Z")
        browser_api.expect(page.locator("#metrics-start")).to_have_value("2026-01-01T14:00")
    finally:
        context.close()


def test_inflight_refresh_does_not_replace_unsaved_filters(lease_browser):
    page, _, _, _, base, errors = lease_browser
    page.clock.install()
    page.goto(base + "/ui")

    def edit_while_refreshing(route):
        page.locator("#metrics-range").select_option("custom")
        page.locator("#metrics-start").fill("2026-01-02T12:34")
        page.locator("#metrics-end").fill("2026-01-03T12:34")
        route.continue_()

    page.route(base + "/ui", edit_while_refreshing)
    with page.expect_response(
        lambda response: response.url == base + "/ui" and response.request.headers.get("x-privy-ui") == "1"
    ) as refreshed:
        page.clock.fast_forward(6000)
    refreshed.value.finished()
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    browser_api.expect(page.locator("#metrics-controls")).to_have_attribute("data-dirty", "1")
    browser_api.expect(page.locator("#metrics-custom")).to_be_visible()
    browser_api.expect(page.locator("#metrics-start")).to_have_value("2026-01-02T12:34")
    browser_api.expect(page.locator("#metrics-end")).to_have_value("2026-01-03T12:34")
    assert not errors
