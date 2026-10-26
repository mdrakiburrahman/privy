"""Real Chromium, xterm, Bash PTY, and hub integration without Relay credentials."""

from __future__ import annotations

import base64
import json
import re
import threading
import time

import pytest
from hub_fakes import FakeListener, Hub

from privy.client import RelayHTTPError
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


def compose(page, text):
    page.locator("#exec-command").fill(text)
    page.locator("#exec-command").press("Control+Enter")


def test_composer_submits_multiline_once_in_the_same_shell(browser_workspace):
    page, _, linux, manager, errors = browser_workspace
    connect(page)
    browser_api.expect(page.locator("#exec-command")).to_be_focused()
    before = len([item for item in linux.terminal_requests if item["action"] == "write"])
    compose(
        page,
        "PRIVY_COMPOSED='λ-é'\ncomposer_function() { printf 'FUNCTION=%s\\n' \"$PRIVY_COMPOSED\"; }\ncd /tmp",
    )
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    compose(page, "composer_function; printf 'DIRECTORY=%s\\n' \"$PWD\"")
    browser_api.expect(output(page)).to_contain_text("FUNCTION=λ-é")
    browser_api.expect(output(page)).to_contain_text("DIRECTORY=/tmp")
    writes = [item for item in linux.terminal_requests if item["action"] == "write"][before:]
    commands = [item for item in writes if item["user_input"]]
    assert len(commands) == 2
    assert len(manager._sessions) == 1 and not linux.requests and not errors
    assert all(len(base64.b64decode(item["data_b64"])) <= 16384 for item in commands)


def test_composer_completion_history_and_editing_are_local(browser_workspace):
    page, _, linux, _, errors = browser_workspace
    connect(page)
    editor = page.locator("#exec-command")
    baseline = len([item for item in linux.terminal_requests if item["action"] == "write"])
    editor.fill("prin")
    browser_api.expect(page.locator("#exec-suggestions")).to_be_visible()
    editor.press("Tab")
    assert editor.input_value() == "printf"
    editor.press("Enter")
    assert editor.input_value() == "printf\n"
    editor.press("Control+z")
    assert editor.input_value() == "prin"
    editor.press("Control+Shift+z")
    assert editor.input_value() == "printf\n"
    editor.fill("printf 'LOCAL=%s\\n' done")
    page.wait_for_timeout(150)
    assert len([item for item in linux.terminal_requests if item["action"] == "write"]) == baseline
    editor.press("Control+Enter")
    browser_api.expect(output(page)).to_contain_text("LOCAL=done")
    page.locator("#exec-history-prev").click()
    assert editor.input_value() == "printf 'LOCAL=%s\\n' done"
    assert not errors


@pytest.mark.parametrize(
    "draft",
    ["x" * 8193, "λ" * 4097, "echo \x1b[31mhidden", "echo \x00bad"],
    ids=["ascii-limit", "utf8-limit", "escape", "nul"],
)
def test_composer_rejects_oversize_and_hidden_control_input(browser_workspace, draft):
    page, _, linux, _, _ = browser_workspace
    connect(page)
    before = len([item for item in linux.terminal_requests if item["action"] == "write"])
    compose(page, draft)
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text(re.compile("8 KiB|control"))
    assert len([item for item in linux.terminal_requests if item["action"] == "write"]) == before
    assert page.locator("#exec-command").input_value() == draft


def test_composer_local_latency_is_independent_of_delayed_delivery(browser_workspace):
    page, _, linux, _, errors = browser_workspace
    connect(page)
    gate = threading.Event()
    original = linux.terminal

    def delayed(payload):
        if payload["action"] == "read":
            time.sleep(1)
        if payload["action"] == "write" and payload.get("user_input"):
            assert gate.wait(5)
        return original(payload)

    linux.terminal = delayed
    try:
        compose(page, "printf 'DELAYED=%s\\n' done")
        page.locator("#exec-command").press("Control+Enter")
        browser_api.expect(page.locator("#exec-run")).to_be_disabled()
        page.wait_for_timeout(1100)
        measurements = page.locator("#exec-command").evaluate("""async editor => {
          const samples = [];
          for (let i = 0; i < 40; i++) {
            const before = performance.now();
            editor.value = i % 2 ? "prin" : "ech";
            editor.dispatchEvent(new Event("input", {bubbles:true}));
            await new Promise(requestAnimationFrame);
            samples.push(performance.now() - before);
          }
          samples.sort((a,b)=>a-b);
          return {p95:samples[Math.ceil(samples.length*.95)-1], max:samples.at(-1)};
        }""")
        print("Composer local render latency:", json.dumps(measurements))
        assert measurements["p95"] <= 50
        browser_api.expect(page.locator("#exec-suggestions")).to_be_visible()
    finally:
        gate.set()
    browser_api.expect(output(page)).to_contain_text("DELAYED=done")
    commands = [item for item in linux.terminal_requests if item["action"] == "write" and item["user_input"]]
    assert len(commands) == 1 and not errors


def test_composer_lost_ack_requires_explicit_reconnect_without_duplicate_execution(browser_workspace):
    page, _, linux, _, errors = browser_workspace
    connect(page)
    original, lost = linux.terminal, False

    def lose_ack(payload):
        nonlocal lost
        result = original(payload)
        if payload["action"] == "write" and payload.get("user_input") and not lost:
            lost = True
            raise RelayHTTPError("controlled lost acknowledgement", timed_out=True)
        return result

    linux.terminal = lose_ack
    compose(page, "PRIVY_COUNTER=$(( ${PRIVY_COUNTER:-0} + 1 )); printf 'COUNT=%s\\n' \"$PRIVY_COUNTER\"")
    browser_api.expect(page.locator("#exec-state")).to_have_text("Connection interrupted")
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("uncertain")
    browser_api.expect(page.locator("#exec-run")).to_be_disabled()
    page.locator("#exec-command").fill("printf 'NEW_DRAFT=%s\\n' kept")
    page.locator("#exec-reconnect").click()
    browser_api.expect(page.locator("#exec-state")).to_have_text("Connected")
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    browser_api.expect(output(page)).to_contain_text("COUNT=1")
    assert page.locator("#exec-command").input_value() == "printf 'NEW_DRAFT=%s\\n' kept"
    commands = [item for item in linux.terminal_requests if item["action"] == "write" and item["user_input"]]
    assert len(commands) == 2 and commands[0]["seq"] == commands[1]["seq"]
    assert commands[0]["data_b64"] == commands[1]["data_b64"] and not errors


def test_composer_read_failure_keeps_local_editing_and_existing_shell(browser_workspace):
    page, _, linux, manager, errors = browser_workspace
    connect(page)
    original, failed = linux.terminal, False

    def fail_read(payload):
        nonlocal failed
        if payload["action"] == "read" and not failed:
            failed = True
            raise RelayHTTPError("controlled read failure", status_code=502)
        return original(payload)

    linux.terminal = fail_read
    browser_api.expect(page.locator("#exec-state")).to_have_text("Connection interrupted", timeout=10000)
    page.locator("#exec-command").fill("prin")
    page.locator("#exec-command").press("Tab")
    assert page.locator("#exec-command").input_value() == "printf"
    browser_api.expect(page.locator("#exec-run")).to_be_disabled()
    page.locator("#exec-reconnect").click()
    browser_api.expect(page.locator("#exec-state")).to_have_text("Connected")
    compose(page, "printf 'RECOVERED=%s\\n' yes")
    browser_api.expect(output(page)).to_contain_text("RECOVERED=yes")
    assert len(manager._sessions) == 1 and not errors


def test_composer_ime_does_not_submit_and_exact_utf8_limit_is_accepted(browser_workspace):
    page, _, linux, _, errors = browser_workspace
    connect(page)
    editor = page.locator("#exec-command")
    editor.fill("#" + "λ" * 4095 + "a")
    editor.dispatch_event("compositionstart")
    editor.press("Control+Enter")
    assert not any(item["action"] == "write" and item["user_input"] for item in linux.terminal_requests)
    editor.dispatch_event("compositionend")
    editor.press("Control+Enter")
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    commands = [item for item in linux.terminal_requests if item["action"] == "write" and item["user_input"]]
    assert len(commands) == 1 and len(base64.b64decode(commands[0]["data_b64"])) <= 16384
    assert not errors


def test_composer_fallback_envelope_preserves_scope_quotes_tabs_and_syntax_errors(browser_workspace):
    page, _, _, manager, errors = browser_workspace
    connect(page)
    command(page, "bind 'set enable-bracketed-paste off'")
    page.wait_for_timeout(100)
    compose(
        page,
        'PRIVY_QUOTED="it\'s a \\\\ path"\ncomposed_fn() {\n\tprintf \'VALUE=%s\\n\' "$PRIVY_QUOTED"\n}\ncd /tmp',
    )
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    compose(page, "composed_fn; printf 'CWD=%s\\n' \"$PWD\"")
    browser_api.expect(output(page)).to_contain_text("VALUE=it's a \\ path")
    browser_api.expect(output(page)).to_contain_text("CWD=/tmp")
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    compose(page, "if then")
    browser_api.expect(output(page)).to_contain_text("syntax error")
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    compose(page, "composed_fn")
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    assert len(manager._sessions) == 1 and not errors


def test_composer_queues_after_unacknowledged_interactive_input(browser_workspace):
    page, _, linux, _, errors = browser_workspace
    connect(page)
    gate, started = threading.Event(), threading.Event()
    original = linux.terminal

    def delayed(payload):
        if payload["action"] == "write" and payload.get("user_input") and not started.is_set():
            started.set()
            assert gate.wait(5)
        return original(payload)

    linux.terminal = delayed
    try:
        terminal = page.locator(".xterm-helper-textarea")
        terminal.focus()
        terminal.press_sequentially("printf 'DIRECT=%s\\n' first")
        terminal.press("Enter")
        assert started.wait(2)
        compose(page, "printf 'COMPOSED=%s\\n' second")
        browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Sending")
        browser_api.expect(page.locator("#exec-run")).to_be_disabled()
    finally:
        gate.set()
    browser_api.expect(output(page)).to_contain_text("DIRECT=first")
    browser_api.expect(output(page)).to_contain_text("COMPOSED=second")
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    assert not errors


def test_composer_caret_movement_and_shift_selection_do_not_apply_stale_completion(browser_workspace):
    page, _, _, _, errors = browser_workspace
    connect(page)
    editor = page.locator("#exec-command")
    editor.fill("prin")
    editor.press("ArrowLeft")
    browser_api.expect(page.locator("#exec-suggestions")).to_be_hidden()
    editor.fill("prin")
    editor.press("Shift+ArrowUp")
    browser_api.expect(page.locator("#exec-suggestions")).to_be_hidden()
    assert editor.evaluate("e=>e.selectionStart!==e.selectionEnd")
    editor.fill("prin")
    editor.press("Control+Shift+ArrowUp")
    browser_api.expect(page.locator("#exec-suggestions")).to_be_hidden()
    assert editor.evaluate("e=>e.selectionStart!==e.selectionEnd")
    assert not errors


def test_composer_double_click_does_not_run_twice_after_fast_ack(browser_workspace):
    page, _, linux, _, errors = browser_workspace
    connect(page)
    page.locator("#exec-command").fill("printf 'ONCE=%s\\n' yes")
    page.locator("#exec-run").dispatch_event("click", {"detail": 1})
    browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    page.locator("#exec-run").dispatch_event("click", {"detail": 2})
    page.wait_for_timeout(100)
    commands = [item for item in linux.terminal_requests if item["action"] == "write" and item["user_input"]]
    assert len(commands) == 1 and not errors


def test_composer_history_is_bounded_and_discarded_on_navigation(browser_workspace):
    page, _, _, _, errors = browser_workspace
    connect(page)
    for index in range(52):
        compose(page, f"# local-history-{index}")
        browser_api.expect(page.locator("#exec-delivery")).to_contain_text("Delivered")
    for _ in range(50):
        page.locator("#exec-history-prev").click()
    assert page.locator("#exec-command").input_value() == "# local-history-2"
    browser_api.expect(page.locator("#exec-history-prev")).to_be_disabled()
    page.reload()
    browser_api.expect(page.locator("#exec-command")).to_have_value("")
    browser_api.expect(page.locator("#exec-history-prev")).to_be_disabled()
    assert not errors
