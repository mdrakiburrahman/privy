"""Remote Exec rendering and its existing API lifecycle; no Relay credentials required."""

from __future__ import annotations

import html
import json
import re

import pytest
from hub_fakes import FakeListener, Hub, blocking_run, exec_result

from privy.client import RelayHTTPError
from privy.hub.remote_exec import execution_choices

BOX = "dev/box1_windows"
LINUX = "contoso-lab/gpu1_linux"


def info(system="Windows", **runtimes):
    return {
        "hostname": "test-host",
        "platform": {"system": system},
        "capabilities": {"languages": ["powershell", "bash", "python"], "inprocess_python": True},
        "runtimes": runtimes,
    }


def data(response):
    assert response.status == 200
    match = re.search(r'id="exec-data" hidden data-targets="([^"]*)"', response.text)
    assert match
    return json.loads(html.unescape(match.group(1)))


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ({}, []),
        ({"capabilities": {"languages": ["python"], "inprocess_python": True}}, []),
        (info(python=None, powershell="pwsh"), ["powershell"]),
        (info("Linux", bash="/bin/bash", python="/usr/bin/python3"), ["bash", "python"]),
        ({"runtimes": {"bash": "/bin/bash"}}, []),
        ({"capabilities": {"languages": ["ruby"]}, "runtimes": {"ruby": "ruby"}}, []),
    ],
)
def test_choices_require_advertised_language_and_subprocess_runtime(description, expected):
    assert execution_choices(description) == expected


def test_remote_actions_are_online_only_and_keep_exact_ids(tmp_path):
    hub = Hub(
        tmp_path,
        {BOX: FakeListener(info=info(powershell="pwsh")), "dev/box2_windows": FakeListener(legacy=True)},
    )
    hub.listeners[LINUX] = FakeListener(
        info_error=RelayHTTPError("offline", status_code=404, detail="no listeners")
    )
    text = hub.call("GET", "/ui/hycos").text
    assert 'href="/ui/exec"' in text
    assert 'href="/ui/exec?target=dev%2Fbox1_windows"' in text
    assert 'href="/ui/exec?target=dev%2Fbox2_windows"' in text
    assert 'href="/ui/exec?target=contoso-lab%2Fgpu1_linux"' not in text
    assert 'aria-label="Remote execute on' in text
    assert hub.jobs.list() == []


def test_page_is_read_only_nonce_protected_and_lists_actual_runtimes(tmp_path):
    windows = FakeListener(info=info(powershell="pwsh", python=None))
    linux = FakeListener(info=info("Linux", bash="/bin/bash", python="/usr/bin/python3"))
    hub = Hub(tmp_path, {BOX: windows, LINUX: linux})
    response = hub.call("GET", "/ui/exec?target=contoso-lab%2Fgpu1_linux")
    targets = {item["id"]: item for item in data(response)}
    assert targets[BOX]["languages"] == ["powershell"]
    assert targets[LINUX]["languages"] == ["bash", "python"]
    assert targets[LINUX]["default"] == "bash"
    assert 'data-target="contoso-lab/gpu1_linux"' in response.text
    assert "<title>Remote Exec · privy hub</title>" in response.text
    assert 'href="/ui/exec" class="on"' in response.text
    assert "Fresh process every run" in response.text
    assert not windows.requests and not linux.requests
    assert hub.call("POST", "/ui/exec", {}).status == 405
    nonce = re.search(r"script-src 'nonce-([^']+)'", response.headers["Content-Security-Policy"]).group(1)
    assert f'<script nonce="{nonce}">' in response.text
    assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
    assert "cdn." not in response.text


def test_empty_legacy_and_unknown_selection_do_not_execute(tmp_path):
    hub = Hub(tmp_path, {BOX: FakeListener(legacy=True)})
    targets = {item["id"]: item for item in data(hub.call("GET", "/ui/exec?target=missing"))}
    assert targets[BOX]["languages"] == []
    assert hub.jobs.list() == []
    hub.relays_file.write_text('{"version":1,"relays":[]}', encoding="utf-8")
    assert data(hub.call("GET", "/ui/exec")) == []


def test_history_is_scoped_complete_and_escaped_not_executed(tmp_path):
    hub = Hub(tmp_path, {BOX: FakeListener(info=info(powershell="pwsh"))})
    code = "</div><script>alert('x')</script>"
    result = hub.call("POST", "/v1/jobs", {"target": BOX, "language": "powershell", "code": code}).payload
    targets = {item["id"]: item for item in data(hub.call("GET", "/ui/exec"))}
    record = targets[BOX]["history"][0]
    assert record["job_id"] == result["job_id"] and record["code"] == code
    assert record["state"] == "succeeded" and record["timeout_s"] == 600
    assert targets[LINUX]["history"] == []
    response = hub.call("GET", "/ui/exec?target=%22%3E%3Cscript%3Ex%3C%2Fscript%3E")
    assert (
        code not in response.text
        and 'data-target="&quot;&gt;&lt;script&gt;x&lt;/script&gt;"' in response.text
    )
    assert len(hub.listeners[BOX].requests) == 1


def test_history_does_not_offer_shared_python_or_truncated_scripts(tmp_path):
    hub = Hub(tmp_path)
    hub.call("POST", "/v1/jobs", {"target": BOX, "language": "python", "mode": "inprocess", "code": "x=1"})
    code = "#" * 65537
    hub.call("POST", "/v1/jobs", {"target": BOX, "language": "bash", "code": code})
    targets = {item["id"]: item for item in data(hub.call("GET", "/ui/exec"))}
    assert len(targets[BOX]["history"]) == 1
    assert targets[BOX]["history"][0]["code"] is None


def test_workspace_job_can_be_reattached_without_consuming_another_readers_output(tmp_path):
    listener = FakeListener(info=info(powershell="pwsh"), run=blocking_run)
    hub = Hub(tmp_path, {BOX: listener})
    result = hub.call(
        "POST",
        "/v1/jobs",
        {
            "target": BOX,
            "language": "powershell",
            "mode": "subprocess",
            "code": "Get-Date",
            "wait_s": 0,
            "max_output_bytes": 0,
        },
    ).payload
    assert listener.started.wait(2)
    job_id = result["job_id"]
    path = f"/v1/jobs/{job_id}?wait_s=0&stdout_offset=0&stderr_offset=0"
    first = hub.call("GET", path).payload
    second = hub.call("GET", path).payload
    assert first["stdout"] == second["stdout"] == "started\n"
    assert first["stdout_offset"] == len(b"started\n")
    cancel = hub.call("POST", f"/v1/jobs/{job_id}/cancel").payload
    assert cancel["state"] == "cancelled"
    assert listener.cancelled == ["remote-blocking"]
    assert len(listener.requests) == 1


def test_failed_output_and_exit_status_remain_explicit(tmp_path):
    def fail(request, on_output, on_submit, listener):
        on_submit("failure")
        on_output("stderr", b"<script>not html</script>\n")
        return exec_result(exit_code=7, stderr=b"<script>not html</script>\n", error="command failed")

    hub = Hub(tmp_path, {BOX: FakeListener(run=fail)})
    result = hub.call("POST", "/v1/jobs", {"target": BOX, "code": "exit 7"}).payload
    assert result["state"] == "failed" and result["exit_code"] == 7
    assert result["error"] == "command failed"
    record = next(item for item in data(hub.call("GET", "/ui/exec")) if item["id"] == BOX)["history"][0]
    assert record["state"] == "failed"
    log = hub.call("GET", f"/ui/jobs/{result['job_id']}/log")
    assert log.media_type().startswith("text/plain")
    assert "<script>not html</script>" in log.text


def test_workspace_write_endpoints_keep_origin_protection(tmp_path):
    hub = Hub(tmp_path)
    result = hub.call(
        "POST",
        "/v1/jobs",
        {"target": BOX, "code": "hostname"},
        headers={"Origin": "http://untrusted.example"},
    )
    assert result.status == 403
    assert hub.jobs.list() == []
