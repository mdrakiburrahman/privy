"""Real Chromium, xterm, Bash PTY, and hub integration without Relay credentials."""

from __future__ import annotations

import re
import threading
import time

import pytest
from hub_fakes import FakeListener, Hub

from privy.hub.app import HubHTTPServer
from privy.terminal import TerminalManager

browser_api = pytest.importorskip("playwright.sync_api", reason="optional browser checks require playwright")
BOX = "dev/box1_windows"
LINUX = "contoso-lab/gpu1_linux"


@pytest.fixture
def browser_workspace(tmp_path):
    manager = TerminalManager()
    linux = FakeListener(
        info={
            "hostname": "browser-test-host",
            "platform": {"system": "Linux"},
            "capabilities": {"languages": ["bash", "python"], "terminal": manager.capabilities()},
            "runtimes": {"bash": "/bin/bash", "python": "/usr/bin/python3"},
        },
        terminal=manager.handle,
    )
    legacy = FakeListener(
        info={
            "hostname": "legacy-host",
            "platform": {"system": "Windows"},
            "capabilities": {"languages": ["powershell"]},
            "runtimes": {"powershell": "pwsh"},
        }
    )
    hub = Hub(tmp_path, {BOX: legacy, LINUX: linux})
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
            yield page, hub, linux, manager, errors
            browser.close()
    finally:
        hub.app.rest.terminals.close()
        manager.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def output(page):
    return page.locator(".xterm-rows")


def command(page, text):
    textarea = page.locator(".xterm-helper-textarea")
    textarea.focus()
    textarea.press_sequentially(text)
    textarea.press("Enter")


def connect(page):
    page.goto("http://127.0.0.1:7750/ui/exec")
    page.locator("#exec-connect").click()
    browser_api.expect(page.locator("#exec-state")).to_have_text("Connected")


def assert_closed(session):
    deadline = time.monotonic() + 3
    while session.state == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    session.reader.join(timeout=3)
    assert session.state == "closed" and not session.backend.alive()
    assert not session.reader.is_alive()


def test_all_sidebar_icons_and_capability_only_navigation(browser_workspace):
    page, _, linux, _, errors = browser_workspace
    page.goto("http://127.0.0.1:7750/ui/hycos")
    assert page.locator(".sidebar nav a").count() == page.locator(".sidebar nav a svg").count() == 9
    assert page.locator("a.remote-link").count() == 1
    assert page.locator("a.remote-link").get_attribute("href") == "/ui/exec?target=contoso-lab%2Fgpu1_linux"
    assert page.locator(".sidebar nav a svg").evaluate_all(
        "icons => icons.every(icon => icon.getBoundingClientRect().width === 16 && icon.getBoundingClientRect().height === 16)"
    )
    page.locator("a.remote-link").click()
    browser_api.expect(page.locator("#exec-state")).to_have_text("Connected")
    assert page.locator("#exec-target option").count() == 1
    assert page.locator("#exec-language option").all_text_contents() == ["Bash"]
    assert any(request["action"] == "open" for request in linux.terminal_requests)
    assert not linux.requests and not errors


def test_real_shell_state_cwd_prompts_interrupt_and_unicode(browser_workspace):
    page, _, _, _, errors = browser_workspace
    expect = browser_api.expect
    connect(page)
    expect(output(page)).not_to_contain_text("Connect to a terminal-capable listener")
    command(page, "PS1='privy> '; PRIVY_STATE=delight; cd /tmp")
    command(page, 'printf \'STATE=%s:%s\\n\' "$PRIVY_STATE" "$PWD"')
    expect(output(page)).to_contain_text("STATE=delight:/tmp")
    command(page, "read -p 'ANSWER> ' answer; printf 'REPLY=%s\\n' \"$answer\"")
    expect(output(page)).to_contain_text("ANSWER>")
    command(page, "interactive-terminal")
    expect(output(page)).to_contain_text("REPLY=interactive-terminal")
    command(page, "sleep 60")
    expect(output(page)).to_contain_text("sleep 60")
    page.locator(".xterm-helper-textarea").press("Control+c")
    expect(output(page)).to_contain_text("^C")
    page.wait_for_function("""() => {
      const rows = [...document.querySelectorAll(".xterm-rows > div")].map(row => row.textContent.trim()).filter(Boolean);
      return rows.at(-1) === "privy>";
    }""")
    command(page, "printf 'AFTER=%s\\n' \"$PRIVY_STATE\"")
    expect(output(page)).to_contain_text("AFTER=delight")
    page.locator(".xterm-helper-textarea").evaluate(
        """(element, text) => {
      const clipboard = new DataTransfer();
      clipboard.setData("text/plain", text);
      element.dispatchEvent(new ClipboardEvent("paste", {
        clipboardData: clipboard, bubbles: true, cancelable: true
      }));
    }""",
        "printf 'UNICODE=%s\\n' 'λ-é'",
    )
    page.locator(".xterm-helper-textarea").press("Enter")
    expect(output(page)).to_contain_text("UNICODE=λ-é")
    assert not errors


def test_resize_refresh_and_native_completion_preserve_session(browser_workspace):
    page, _, linux, manager, errors = browser_workspace
    expect = browser_api.expect
    connect(page)
    command(page, "export PRIVY_COMPLETION=works")
    textarea = page.locator(".xterm-helper-textarea")
    textarea.press_sequentially("echo $PRIVY_COMPLET")
    textarea.press("Tab")
    textarea.press("Enter")
    expect(page.locator(".xterm-rows > div").filter(has_text=re.compile(r"^\s*works\s*$"))).to_have_count(1)
    command(page, "PRIVY_STATE=refresh-proof")
    page.locator("#exec-refresh").click()
    expect(page.locator("#exec-refresh")).to_be_enabled()
    page.evaluate("document.dispatchEvent(new Event('remote-exec-refresh'))")
    expect(page.locator("#exec-refresh")).to_be_enabled()
    assert len(manager._sessions) == 1
    page.set_viewport_size({"width": 1150, "height": 900})
    page.wait_for_function("document.getElementById('exec-size').textContent !== '80 × 24'")
    command(page, "stty size")
    size = page.locator("#exec-size").inner_text().split(" × ")
    expect(output(page)).to_contain_text(f"{size[1]} {size[0]}")
    command(page, "printf 'PERSIST=%s\\n' \"$PRIVY_STATE\"")
    expect(output(page)).to_contain_text("PERSIST=refresh-proof")
    assert any(item["action"] == "resize" for item in linux.terminal_requests)
    assert not errors


def test_navigation_reload_and_disconnect_close_the_owned_process(browser_workspace):
    page, _, _, manager, errors = browser_workspace
    expect = browser_api.expect
    connect(page)
    first = next(iter(manager._sessions.values()))
    page.reload()
    expect(page.locator("#exec-state")).to_have_text("Connected")
    assert_closed(first)
    second = [item for item in manager._sessions.values() if item.state == "running"][0]
    page.locator('.sidebar a[href="/ui/hycos"]').click()
    expect(page.locator("h1")).not_to_have_text("Remote Exec")
    assert_closed(second)
    page.locator("a.remote-link").click()
    expect(page.locator("#exec-state")).to_have_text("Connected")
    third = [item for item in manager._sessions.values() if item.state == "running"][0]
    page.locator("#exec-disconnect").click()
    expect(page.locator("#exec-state")).to_have_text("Disconnected")
    assert_closed(third)
    assert not errors


def test_unknown_and_runtime_disappearing_never_fall_back(browser_workspace):
    page, _, linux, manager, errors = browser_workspace
    expect = browser_api.expect
    page.goto(f"http://127.0.0.1:7750/ui/exec?target={BOX}")
    expect(page.locator("#exec-connect")).to_be_disabled()
    assert not manager._sessions
    page.goto("http://127.0.0.1:7750/ui/exec")
    linux.info["capabilities"].pop("terminal")
    page.locator("#exec-connect").click()
    expect(page.locator("#exec-message")).to_contain_text("does not advertise")
    assert not manager._sessions and not linux.requests
    page.locator("#exec-disconnect").click()
    expect(page.locator("#exec-state")).to_have_text("Disconnected")
    assert not errors
