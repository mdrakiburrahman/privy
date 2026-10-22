import io
import logging
import sys
import time

import pytest
from hub_fakes import FakeListener, Hub, exec_result

from privy.client import RelayHTTPError
from privy.executor import set_echo_output
from privy.hub import console

NO_LISTENER = '{"error":{"code":"EndpointNotFound","message":"There are no listeners connected"}}'


@pytest.fixture(autouse=True)
def _console(caplog):
    caplog.set_level(logging.INFO, logger="privy.hub")
    set_echo_output(False)
    yield
    set_echo_output(False)


def _messages(caplog):
    return [record.getMessage() for record in caplog.records if record.name == "privy.hub"]


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_probes_print_one_line_each(tmp_path, caplog):
    offline = FakeListener(info_error=RelayHTTPError("x", status_code=404, detail=NO_LISTENER))
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(hostname="BOX1"), "dev/box2_windows": offline})

    hub.registry.probe_many(hub.registry.select(["dev/*"]))
    hub.registry.probe_many(hub.registry.select(["dev/*"]))  # cached: nothing new to say

    probes = sorted(message for message in _messages(caplog) if message.startswith("probe "))
    assert len(probes) == 2
    assert probes[0].startswith("probe dev/box1_windows: online ") and "host=BOX1 privy=0.2.0" in probes[0]
    assert probes[1].startswith("probe dev/box2_windows: offline ") and "no listener" in probes[1]


def test_jobs_print_a_request_box_live_output_and_a_result_box(tmp_path, caplog, monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    set_echo_output(True)

    def run(request, on_output, on_submit, listener):
        on_submit("remote-123456789")
        on_output("stdout", b"hello\nwor")
        on_output("stderr", b"careful\n")
        on_output("stdout", b"ld\n")
        return exec_result(stdout=b"hello\nworld\n", stderr=b"careful\n", job_id="remote-123456789")

    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(run=run, hostname="BOX1")})
    hub.registry.probe(hub.registry.resolve("dev/box1_windows"))
    result = hub.call(
        "POST", "/v1/jobs", {"target": "dev/box1_windows", "language": "bash", "code": "say hi"}
    )
    job_id = result.payload["job_id"]

    prefix = f"[{job_id[:8]} box1_windows] "
    assert _wait_for(lambda: out.getvalue() == f"{prefix}hello\n{prefix}world\n")
    assert err.getvalue() == f"{prefix}careful\n"
    messages = _messages(caplog)
    run_box = next(message for message in messages if "▶ RUN" in message)
    assert f"▶ RUN {job_id}  relay=dev (contoso-relay)  hyco=box1_windows  host=BOX1" in run_box
    assert "via=rest  language=bash  mode=subprocess  timeout=600s" in run_box
    assert "│   say hi" in run_box
    result_box = next(message for message in messages if "◀ RESULT" in message)
    assert f"◀ RESULT {job_id}  relay=dev  hyco=box1_windows  ✓ succeeded  exit=0" in result_box
    assert "remote=remote-1" in result_box
    assert "│   hello\n│   world" in result_box and "│   careful" in result_box


def test_failed_jobs_and_broadcasts_are_boxed_too(tmp_path, caplog):
    hub = Hub(tmp_path, {"dev/box2_windows": FakeListener(run=lambda *args: exec_result(exit_code=3))})

    hub.call("POST", "/v1/broadcasts", {"targets": ["dev/*"], "code": "exit 3", "wait_s": 5})

    messages = _messages(caplog)
    broadcast = next(message for message in messages if "▶ BROADCAST" in message)
    assert "targets (2): dev/box1_windows, dev/box2_windows" in broadcast and "│   exit 3" in broadcast
    runs = [message for message in messages if "▶ RUN" in message]
    assert len(runs) == 2 and all("broadcast=b" in message and "exit 3" not in message for message in runs)
    assert any("✗ failed  exit=3" in message for message in messages if "◀ RESULT" in message)


def test_transfer_progress_logs_quarter_milestones(tmp_path, caplog):
    hub = Hub(tmp_path)
    job = hub.jobs.start_transfer(
        hub.registry.resolve("dev/box1_windows"),
        direction="upload",
        local_path="C:/x",
        remote_path="/x",
        overwrite=False,
        origin="test",
    )
    caplog.clear()

    last = 0
    for done in (10, 30, 60, 80, 100):
        last = console.transfer_progress(job, done, 100, last)

    milestones = [message for message in _messages(caplog) if " upload " in message]
    assert [message.split(" upload ")[1].split("%")[0] for message in milestones] == ["25", "50", "75"]


def test_compact_json_clips_long_arguments():
    assert console.compact_json({"code": "x"}) == '{"code":"x"}'
    clipped = console.compact_json({"code": "y" * 500})
    assert len(clipped) == console.ARGS_PREVIEW_CHARS and clipped.endswith("…")
