"""Capability-gated terminal rendering and the same-origin session bridge."""

from __future__ import annotations

import base64
import html
import json
import re
import threading

import pytest
from hub_fakes import FakeListener, Hub

from privy.client import RelayClient, RelayHTTPError
from privy.hub.app import HubApp
from privy.hub.remote_exec import execution_choices

BOX = "dev/box1_windows"
LINUX = "contoso-lab/gpu1_linux"
OWNER = "workspace-owner-0123456789"
REQUEST = "terminal-request-0123456789"


def info(system="Windows", terminal=True, **runtimes):
    return {
        "hostname": "test-host",
        "platform": {"system": system},
        "capabilities": {
            "languages": ["powershell", "bash", "python"],
            "terminal": {"version": 1, "shells": list(runtimes), "idle_ttl_s": 3600} if terminal else None,
        },
        "runtimes": runtimes,
    }


def data(response):
    assert response.status == 200
    match = re.search(r'id="exec-data" hidden data-targets="([^"]*)"', response.text)
    assert match
    return json.loads(html.unescape(match[1]))


class TerminalStub:
    def __init__(self):
        self.calls = []
        self.seq = 0
        self.closed = False
        self.output = b"prompt> "

    def __call__(self, payload):
        self.calls.append(payload)
        action = payload["action"]
        if action == "write":
            assert payload["seq"] == self.seq
            self.seq += 1
        if action == "close":
            self.closed = True
        offset = payload.get("offset", 0)
        output = self.output[offset:] if action == "read" else b""
        return {
            "kind": "terminal",
            "protocol_version": 1,
            "ok": True,
            "session_id": "remote-session",
            "state": "closed" if self.closed else "running",
            "next_seq": self.seq,
            "data_b64": base64.b64encode(output).decode(),
            "offset": offset + len(output),
            "start_offset": 0,
            "expires_in_s": 3600,
            "exit_code": None,
        }


@pytest.fixture
def workspace(tmp_path):
    terminal = TerminalStub()
    windows = FakeListener(info=info(powershell="pwsh", bash="bash"), terminal=terminal)
    hub = Hub(tmp_path, {BOX: windows, LINUX: FakeListener(legacy=True)})
    return hub, windows, terminal


def open_terminal(hub, **extra):
    return hub.call(
        "POST",
        "/v1/terminals",
        {
            "target": BOX,
            "shell": "powershell",
            "owner": OWNER,
            "request_id": REQUEST,
            **extra,
        },
    )


def operation(hub, action, **extra):
    return hub.call("POST", f"/v1/terminals/{REQUEST}/{action}", {"owner": OWNER, "target": BOX, **extra})


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ({}, []),
        (info(terminal=False, powershell="pwsh", bash="bash"), []),
        (info(powershell="pwsh", python="python"), ["powershell"]),
        (info("Linux", bash="/bin/bash", python="/usr/bin/python3"), ["bash"]),
        (info(powershell=None), []),
        (
            {"capabilities": {"terminal": {"version": 2, "shells": ["bash"]}}, "runtimes": {"bash": "bash"}},
            [],
        ),
        (
            {
                "capabilities": {"terminal": {"version": True, "shells": ["bash"]}},
                "runtimes": {"bash": "bash"},
            },
            [],
        ),
        ({"capabilities": [], "runtimes": {}}, []),
    ],
)
def test_choices_require_explicit_compatible_terminal_capability(description, expected):
    assert execution_choices(description) == expected


def test_only_capable_online_hycos_have_remote_actions(workspace):
    hub, windows, terminal = workspace
    text = hub.call("GET", "/ui/hycos").text
    assert 'href="/ui/exec?target=dev%2Fbox1_windows"' in text
    assert 'href="/ui/exec?target=contoso-lab%2Fgpu1_linux"' not in text
    assert 'href="/ui/exec?target=dev%2Fbox2_windows"' not in text
    assert not windows.requests and not terminal.calls
    windows.info_error = RelayHTTPError("offline", status_code=404)
    hub.registry.probe(hub.registry.resolve(BOX), refresh=True)
    assert 'href="/ui/exec?target=dev%2Fbox1_windows"' not in hub.call("GET", "/ui/hycos").text


def test_workspace_is_read_only_bundled_and_nonce_protected(workspace):
    hub, windows, terminal = workspace
    response = hub.call("GET", f"/ui/exec?target={BOX}")
    targets = data(response)
    assert len(targets) == 1 and targets[0]["languages"] == ["powershell", "bash"]
    assert targets[0]["default"] == "powershell"
    assert 'id="exec-terminal"' in response.text
    assert "Fresh process every run" not in response.text
    assert "leaving or refreshing" in response.text.lower()
    assert 'href="/ui/exec" class="on"' in response.text
    assert not windows.requests and not terminal.calls
    nonce = re.search(r"script-src 'nonce-([^']+)'", response.headers["Content-Security-Policy"])[1]
    assert f'<script nonce="{nonce}">' in response.text
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
    assert "cdn." not in response.text
    assert "documentOverride" in response.text


def test_unknown_legacy_and_empty_target_selections_do_not_execute(workspace):
    hub, _, terminal = workspace
    response = hub.call("GET", "/ui/exec?target=%22%3E%3Cscript%3Ex%3C%2Fscript%3E")
    assert 'data-target="&quot;&gt;&lt;script&gt;x&lt;/script&gt;"' in response.text
    assert len(data(response)) == 1
    assert not terminal.calls
    hub.relays_file.write_text('{"version":1,"relays":[]}', encoding="utf-8")
    assert data(hub.call("GET", "/ui/exec")) == []


def test_bridge_preserves_target_session_owner_input_offsets_and_cleanup(workspace):
    hub, windows, terminal = workspace
    opened = open_terminal(hub)
    assert opened.status == 200 and opened.payload["session_id"] == REQUEST
    read = operation(hub, "read", offset=0, wait_s=0, max_bytes=32768)
    assert base64.b64decode(read.payload["data_b64"]) == b"prompt> "
    assert read.payload["offset"] == len(b"prompt> ")
    write = operation(hub, "write", seq=0, data_b64="eD0xDQ==")
    assert write.status == 200 and write.payload["next_seq"] == 1
    assert terminal.calls[-1]["session_id"] == "remote-session"
    assert terminal.calls[-1]["owner"] == OWNER
    assert operation(hub, "resize", cols=120, rows=40).status == 200
    assert terminal.calls[-1]["cols"] == 120
    assert operation(hub, "close").status == 200 and terminal.closed
    assert operation(hub, "close").status == 200
    assert operation(hub, "write", seq=1, data_b64="DQ==").status == 410
    assert not windows.requests and hub.jobs.list() == []
    audit = hub.audit_path.read_text()
    assert "terminal.open" in audit and "terminal.close" in audit
    assert OWNER not in audit and "eD0xDQ==" not in audit


@pytest.mark.parametrize("change", ["python", "offline", "capability", "runtime"])
def test_open_revalidates_capability_runtime_shell_and_online_status(workspace, change):
    hub, windows, terminal = workspace
    hub.call("GET", "/ui/exec")
    extra = {}
    if change == "python":
        extra["shell"] = "python"
    elif change == "offline":
        windows.info_error = RelayHTTPError("offline", status_code=404)
    elif change == "capability":
        windows.info["capabilities"].pop("terminal")
    else:
        windows.info["runtimes"]["powershell"] = None
    assert open_terminal(hub, **extra).status in (400, 409)
    assert not terminal.calls


def test_cannot_reopen_or_take_another_workspaces_session(workspace):
    hub, _, terminal = workspace
    assert open_terminal(hub).status == 200
    calls = len(terminal.calls)
    assert open_terminal(hub).status == 409
    assert open_terminal(hub, owner="another-owner-0123456789").status == 403
    assert operation(hub, "read", owner="another-owner-0123456789").status == 403
    assert operation(hub, "close", owner="another-owner-0123456789").status == 403
    assert len(terminal.calls) == calls


def test_close_before_open_prevents_late_orphan_creation(workspace):
    hub, _, terminal = workspace
    assert operation(hub, "close").status == 200
    assert open_terminal(hub).status == 409
    assert terminal.closed and [call["action"] for call in terminal.calls] == ["close"]


def test_closing_while_opening_closes_the_eventual_remote_process(workspace):
    hub, listener, terminal = workspace
    started, release = threading.Event(), threading.Event()
    original = listener.terminal

    def pending(payload):
        if payload["action"] == "open":
            started.set()
            assert release.wait(3)
        return original(payload)

    listener.terminal = pending
    results = []
    thread = threading.Thread(target=lambda: results.append(open_terminal(hub)))
    thread.start()
    try:
        assert started.wait(3)
        assert operation(hub, "close").status == 200
    finally:
        release.set()
        thread.join(3)
    assert results[0].status == 409 and terminal.closed


def test_lost_open_response_is_not_automatically_retried_and_can_be_closed(workspace):
    hub, listener, terminal = workspace
    original = listener.terminal

    def uncertain(payload):
        if payload["action"] == "open":
            original(payload)
            raise RelayHTTPError("response lost", timed_out=True)
        return original(payload)

    listener.terminal = uncertain
    assert open_terminal(hub).status == 502
    assert open_terminal(hub).status == 409
    assert operation(hub, "close").status == 200 and terminal.closed
    assert [call["action"] for call in terminal.calls] == ["open", "close"]


def test_terminal_body_and_tokens_never_reach_console_labels():
    body = json.dumps({"owner": OWNER, "data_b64": "secret-keystrokes"}).encode()
    label = HubApp.describe("POST", f"/v1/terminals/{REQUEST}/write", body)
    assert OWNER not in label and "secret-keystrokes" not in label
    assert "write" in label


def test_terminal_write_routes_keep_origin_and_json_protection(workspace):
    hub, _, terminal = workspace
    assert hub.call("POST", "/v1/terminals", {}, headers={"Origin": "http://untrusted.example"}).status == 403
    assert hub.call("GET", "/v1/terminals").status == 405
    assert hub.call("POST", "/v1/terminals", b"{}", headers={"Content-Type": "text/plain"}).status == 415
    assert open_terminal(hub, owner="short").status == 400
    assert not terminal.calls


def test_client_terminal_transport_is_distinct_and_versioned(monkeypatch):
    sent = []
    client = RelayClient(namespace="ns", path="path", keyrule="rule", key="key")
    monkeypatch.setattr(
        client, "_post_json", lambda payload, **kwargs: sent.append(json.loads(payload)) or {"ok": True}
    )
    client.terminal({"action": "open", "owner": OWNER, "shell": "bash"})
    assert sent == [
        {"action": "open", "owner": OWNER, "shell": "bash", "kind": "terminal", "protocol_version": 1}
    ]
