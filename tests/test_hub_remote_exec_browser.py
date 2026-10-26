"""Optional real-browser regression: uv run --with playwright pytest tests/test_hub_remote_exec_browser.py."""

from __future__ import annotations

import threading

import pytest
from hub_fakes import FakeListener, Hub, blocking_run, exec_result

from privy.hub.app import HubHTTPServer

browser_api = pytest.importorskip("playwright.sync_api", reason="optional browser checks require playwright")

BOX = "dev/box1_windows"
LINUX = "contoso-lab/gpu1_linux"


def listener_info(system, **runtimes):
    return {
        "hostname": "browser-test-host",
        "platform": {"system": system},
        "capabilities": {"languages": ["python", "bash", "powershell"]},
        "runtimes": runtimes,
    }


@pytest.fixture
def browser_workspace(tmp_path):
    windows = FakeListener(info=listener_info("Windows", powershell="pwsh"))
    linux = FakeListener(info=listener_info("Linux", bash="/bin/bash", python="/usr/bin/python3"))
    hub = Hub(tmp_path, {BOX: windows, LINUX: linux})
    hub.app.set_port(7750)
    server = HubHTTPServer(("127.0.0.1", 7750), hub.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 1100})
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
            yield page, hub, windows, linux, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_editor_lifecycle_completion_refresh_reconnect_and_layout(browser_workspace, tmp_path):
    page, hub, windows, linux, errors = browser_workspace
    expect = browser_api.expect
    page.goto("http://127.0.0.1:7750/ui/hycos")
    page.locator('a[href="/ui/exec?target=dev%2Fbox1_windows"]').click()
    expect(page.locator("#exec-target")).to_have_value(BOX)
    assert page.locator("#exec-language option").all_text_contents() == ["PowerShell"]
    assert not windows.requests and not linux.requests
    editor = page.locator("#exec-code")
    editor.fill("Get-D")
    expect(page.locator("#exec-suggestions")).to_be_visible()
    editor.press("Tab")
    expect(editor).to_have_value("Get-Date")
    page.locator("#exec-run").hover()
    assert page.locator("#exec-run").evaluate(
        "button => getComputedStyle(button).backgroundColor !== getComputedStyle(button).color"
    )
    editor.press("Control+Enter")
    expect(page.locator("#exec-state")).to_have_text("succeeded · exit 0")
    expect(page.locator("#exec-stdout")).to_contain_text("ran powershell: Get-Date")
    assert len(windows.requests) == 1
    editor.fill("windows draft")
    page.locator("#exec-target").select_option(LINUX)
    assert page.locator("#exec-language option").all_text_contents() == ["Bash", "Python"]
    editor.fill("linux draft")
    page.locator("#exec-language").select_option("python")
    editor.fill("print('python draft')")
    page.locator("#exec-language").select_option("bash")
    expect(editor).to_have_value("linux draft")
    page.locator("#exec-target").select_option(BOX)
    expect(editor).to_have_value("windows draft")
    page.evaluate("document.dispatchEvent(new Event('remote-exec-refresh'))")
    expect(page.locator("#exec-refresh")).to_be_enabled()
    expect(editor).to_have_value("windows draft")
    editor.fill("Get-")
    editor.press("Escape")
    expect(page.locator("#exec-suggestions")).to_be_hidden()
    editor.press("Tab")
    expect(editor).to_have_value("Get-  ")
    page.locator("#exec-history button", has_text="Load script").first.click()
    expect(editor).to_have_value("Get-Date")
    assert len(windows.requests) == 1
    page.locator("#exec-run").click()
    expect(page.locator("#exec-state")).to_have_text("succeeded · exit 0")
    assert len(windows.requests) == 2

    def fail(request, on_output, on_submit, listener):
        on_submit("failed")
        on_output("stderr", b"<script>window.injected=true</script>")
        return exec_result(
            exit_code=9, stderr=b"<script>window.injected=true</script>", error="expected failure"
        )

    windows.run = fail
    editor.fill("exit 9")
    page.locator("#exec-run").click()
    expect(page.locator("#exec-state")).to_have_text("failed · exit 9")
    expect(page.locator("#exec-message")).to_contain_text("expected failure")
    expect(page.locator("#exec-stderr")).to_have_text("<script>window.injected=true</script>")
    assert page.evaluate("window.injected") is None

    windows.run = blocking_run
    editor.fill("wait")
    page.locator("#exec-run").click()
    expect(page.locator("#exec-stdout")).to_have_text("started\n")
    expect(page.locator("#exec-target")).to_be_disabled()
    page.reload()
    expect(page.locator("#exec-stdout")).to_have_text("started\n")
    assert len(windows.requests) == 4
    page.locator("#exec-cancel").click()
    expect(page.locator("#exec-state")).to_contain_text("cancelled")
    assert windows.cancelled == ["remote-blocking"]
    page.screenshot(path=str(tmp_path / "remote-exec-desktop.png"), full_page=True)
    page.emulate_media(color_scheme="dark")
    page.screenshot(path=str(tmp_path / "remote-exec-dark.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "remote-exec-mobile.png"), full_page=True)
    assert not errors
    assert hub.jobs.counts()["running"] == 0
    print("SCREENSHOTS", tmp_path)


def test_disconnection_and_unknown_target_never_resubmit(browser_workspace):
    page, hub, windows, linux, errors = browser_workspace
    expect = browser_api.expect
    page.goto("http://127.0.0.1:7750/ui/exec?target=not-configured")
    expect(page.locator("#exec-target")).to_have_value("")
    expect(page.locator("#exec-message")).to_contain_text("no longer configured")
    page.locator("#exec-code").fill("Get-Date")
    expect(page.locator("#exec-run")).to_be_disabled()
    page.locator("#exec-target").select_option(BOX)
    page.locator("#exec-code").fill("Get-Date")
    windows.run = blocking_run
    page.route("**/v1/jobs/*?*", lambda route: route.abort())
    page.locator("#exec-run").click()
    expect(page.locator("#exec-message")).to_contain_text("Output connection interrupted")
    expect(page.locator("#exec-reconnect")).to_be_visible()
    expect(page.locator("#exec-run")).to_be_disabled()
    assert len(windows.requests) == 1
    page.unroute("**/v1/jobs/*?*")
    page.locator("#exec-reconnect").click()
    expect(page.locator("#exec-stdout")).to_have_text("started\n")
    page.locator("#exec-cancel").click()
    expect(page.locator("#exec-state")).to_contain_text("cancelled")
    assert len(windows.requests) == 1
    assert not errors


def test_changed_runtime_is_rejected_before_execution(browser_workspace):
    page, hub, windows, linux, errors = browser_workspace
    expect = browser_api.expect
    page.goto("http://127.0.0.1:7750/ui/exec?target=" + BOX)
    page.locator("#exec-code").fill("Get-Date")
    windows.info["runtimes"]["powershell"] = None
    page.locator("#exec-run").click()
    expect(page.locator("#exec-message")).to_contain_text("no longer available")
    expect(page.locator("#exec-run")).to_be_disabled()
    assert windows.requests == []
    assert hub.jobs.list() == []
    assert not errors


def test_uncertain_submission_is_not_automatically_retried(browser_workspace):
    page, hub, windows, linux, errors = browser_workspace
    expect = browser_api.expect
    page.goto("http://127.0.0.1:7750/ui/exec?target=" + BOX)
    page.locator("#exec-code").fill("Get-Date")

    def lose_response(route):
        route.fetch()
        route.abort()

    page.route("**/v1/jobs", lose_response)
    page.locator("#exec-run").click()
    expect(page.locator("#exec-message")).to_contain_text("Submission was not retried")
    expect(page.locator("#exec-history")).to_contain_text("Get-Date")
    page.locator("#exec-refresh").click()
    expect(page.locator("#exec-refresh")).to_be_enabled()
    assert len(windows.requests) == 1
    assert len(hub.jobs.list()) == 1
    assert not errors


def test_large_output_is_bounded_with_full_log_available(browser_workspace):
    page, hub, windows, linux, errors = browser_workspace
    expect = browser_api.expect

    def loud(request, on_output, on_submit, listener):
        on_submit("large-output")
        output = b"x" * 100000 + b"END_OF_OUTPUT"
        on_output("stdout", output)
        return exec_result(stdout=output)

    windows.run = loud
    page.goto("http://127.0.0.1:7750/ui/exec?target=" + BOX)
    page.locator("#exec-code").fill("large output")
    page.locator("#exec-run").click()
    expect(page.locator("#exec-state")).to_have_text("succeeded \u00b7 exit 0")
    output = page.locator("#exec-stdout").inner_text()
    assert output.endswith("END_OF_OUTPUT")
    assert "latest output" in output
    assert len(output) < 65700
    expect(page.locator("#exec-log")).to_be_visible()
    assert not errors
