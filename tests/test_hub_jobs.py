import json
import threading

import pytest
from hub_fakes import FakeListener, Hub, blocking_run, exec_result

from privy.client import RelayHTTPError
from privy.hub.jobs import (
    CANCELLED,
    ERROR,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TIMED_OUT,
    HubBusyError,
    JobLookupError,
    OutputBuffer,
)
from privy.hub.registry import OFFLINE
from privy.transfer import TransferError, TransferResult

NO_LISTENER = '{"error":{"code":"EndpointNotFound","message":"There are no listeners connected"}}'


def _run(hub, target="dev/box1_windows", *, code="echo hi", language="bash", wait_s=5.0, **options):
    job = hub.jobs.start_exec(
        hub.registry.resolve(target),
        language=language,
        code=code,
        mode=options.pop("mode", "subprocess"),
        timeout_s=options.pop("timeout_s", 30.0),
        origin="test",
        **options,
    )
    hub.jobs.wait(job, wait_s)
    return job


def test_output_buffer_keeps_the_tail_with_absolute_offsets():
    buffer = OutputBuffer(limit=5)
    buffer.append(b"abc")
    buffer.append(b"defgh")

    assert (buffer.start, buffer.total) == (3, 8)
    assert buffer.tail(3) == b"fgh"
    assert buffer.read(0, 10) == (3, b"defgh")
    assert buffer.read(6, 1) == (6, b"g")
    assert buffer.read(99, 1) == (8, b"")


def test_exec_job_streams_output_and_records_the_remote_job(tmp_path):
    hub = Hub(tmp_path)

    job = _run(hub, code="hostname")

    view = job.view(max_output_bytes=1024)
    assert view["state"] == SUCCEEDED and view["done"] is True
    assert view["exit_code"] == 0
    assert view["stdout"] == "ran bash: hostname\n"
    assert view["stdout_truncated"] is False
    assert view["remote_job_id"] == "remote-1"
    assert view["duration_ms"] is not None


@pytest.mark.parametrize(
    ("result", "state", "error"),
    [
        (exec_result(exit_code=3, stderr=b"nope\n"), FAILED, None),
        (exec_result(exit_code=1, timed_out=True, error="timeout"), TIMED_OUT, "timeout"),
        (exec_result(exit_code=1, error="unknown_job"), ERROR, "the listener no longer knows this job"),
        (exec_result(exit_code=2, error="bad_request"), FAILED, "bad_request"),
    ],
)
def test_exec_results_map_to_job_states(tmp_path, result, state, error):
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(run=lambda *args: result)})

    job = _run(hub)

    assert job.state == state
    assert job.exit_code == result.exit_code
    assert (job.error or "").startswith(error or "")


def test_relay_failures_become_error_jobs_and_mark_the_target(tmp_path):
    def offline(request, on_output, on_submit, listener):
        raise RelayHTTPError("relay request failed", status_code=404, detail=NO_LISTENER)

    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(run=offline)})

    job = _run(hub)

    assert job.state == ERROR
    assert job.error.startswith("offline: no listener is connected")
    assert hub.registry.cached(job.target).status == OFFLINE


def test_cancel_stops_the_remote_job(tmp_path):
    listener = FakeListener(run=blocking_run)
    hub = Hub(tmp_path, {"dev/box1_windows": listener})
    job = _run(hub, wait_s=0)
    assert listener.started.wait(5)
    assert job.state == RUNNING

    hub.jobs.cancel(job.id)
    hub.jobs.wait(job, 5)

    assert job.state == CANCELLED
    assert listener.cancelled == ["remote-blocking"]
    assert job.view(max_output_bytes=100)["stdout"] == "started\n"


def test_cancelling_a_queued_job_never_runs_it(tmp_path):
    first_listener = FakeListener(run=blocking_run)
    second_listener = FakeListener()
    hub = Hub(
        tmp_path,
        {"dev/box1_windows": first_listener, "dev/box2_windows": second_listener},
        max_running=1,
    )
    first = _run(hub, wait_s=0)
    assert first_listener.started.wait(5)
    second = _run(hub, "dev/box2_windows", wait_s=0.2)
    assert second.state == QUEUED

    hub.jobs.cancel(second.id)
    first_listener.cancel_event.set()
    hub.jobs.wait(first, 5)

    assert second.state == CANCELLED
    assert second_listener.requests == []


def test_queue_limit_rejects_new_work(tmp_path):
    hub = Hub(tmp_path, max_running=1, max_queued=0)

    with pytest.raises(HubBusyError):
        _run(hub)


def test_finished_jobs_are_retained_up_to_the_limit(tmp_path):
    hub = Hub(tmp_path, max_retained=1)
    first = _run(hub)
    second = _run(hub)

    assert hub.jobs.get(second.id) is second
    with pytest.raises(JobLookupError):
        hub.jobs.get(first.id)


def test_read_output_pages_without_splitting_utf8(tmp_path):
    payload = "é" * 10

    def run(request, on_output, on_submit, listener):
        on_submit("remote-1")
        on_output("stdout", payload.encode())
        return exec_result(stdout=payload.encode())

    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(run=run)})
    job = _run(hub)

    page = job.read_output("stdout", 0, 5)
    assert page["data"] == "éé"
    assert page["next_offset"] == 4
    assert page["eof"] is False
    rest = job.read_output("stdout", page["next_offset"], 100)
    assert rest["data"] == "é" * 8
    assert rest["eof"] is True


def test_output_beyond_the_limit_is_reported_as_skipped(tmp_path):
    def run(request, on_output, on_submit, listener):
        on_submit("remote-1")
        on_output("stdout", b"0123456789")
        return exec_result(stdout=b"0123456789")

    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(run=run)}, output_limit_bytes=4)
    job = _run(hub)

    view = job.view(max_output_bytes=100)
    assert view["stdout"] == "6789" and view["stdout_bytes"] == 10 and view["stdout_truncated"] is True
    page = job.read_output("stdout", 0, 100)
    assert page["offset"] == 6 and page["skipped_bytes"] == 6 and page["data"] == "6789"


def test_audit_log_records_requests_and_outcomes(tmp_path):
    hub = Hub(tmp_path)
    job = _run(hub, code="whoami")

    events = [json.loads(line) for line in hub.audit_path.read_text(encoding="utf-8").splitlines()]

    assert [event["event"] for event in events] == ["job.start", "job.finish"]
    assert events[0]["code"] == "whoami" and events[0]["target"] == "dev/box1_windows"
    assert events[0]["origin"] == "test"
    assert events[1]["state"] == SUCCEEDED and events[1]["job_id"] == job.id


def _transfer(direction, source, destination, overwrite, progress):
    progress(5, 10)
    progress(10, 10)
    return TransferResult(direction, str(source), str(destination), 10, "a" * 64, "transfer-1")


def test_transfer_jobs_report_progress_and_result(tmp_path):
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(transfer=_transfer)})
    job = hub.jobs.start_transfer(
        hub.registry.resolve("dev/box1_windows"),
        direction="upload",
        local_path="C:/data/model.pkl",
        remote_path="/tmp/model.pkl",
        overwrite=False,
        origin="test",
    )
    hub.jobs.wait(job, 5)

    view = job.view(max_output_bytes=0)
    assert view["state"] == SUCCEEDED
    assert view["progress"] == {"bytes_done": 10, "bytes_total": 10}
    assert view["result"]["sha256"] == "a" * 64


def test_transfer_failures_and_cancellation(tmp_path):
    release = threading.Event()

    def refuse(direction, source, destination, overwrite, progress):
        raise TransferError("destination_exists", "destination exists")

    def slow(direction, source, destination, overwrite, progress):
        progress(1, 10)
        release.wait(5)
        progress(2, 10)
        raise AssertionError("cancellation should stop the transfer")

    hub = Hub(
        tmp_path,
        {"dev/box1_windows": FakeListener(transfer=refuse), "dev/box2_windows": FakeListener(transfer=slow)},
    )
    refused = hub.jobs.start_transfer(
        hub.registry.resolve("dev/box1_windows"),
        direction="download",
        local_path="C:/x",
        remote_path="/x",
        overwrite=False,
        origin="test",
    )
    slow_job = hub.jobs.start_transfer(
        hub.registry.resolve("dev/box2_windows"),
        direction="upload",
        local_path="C:/y",
        remote_path="/y",
        overwrite=False,
        origin="test",
    )
    hub.jobs.wait(refused, 5)
    hub.jobs.cancel(slow_job.id)
    release.set()
    hub.jobs.wait(slow_job, 5)

    assert refused.state == FAILED and refused.error == "destination_exists: destination exists"
    assert slow_job.state == CANCELLED


def test_broadcast_starts_one_job_per_target(tmp_path):
    hub = Hub(tmp_path)
    targets = hub.registry.select(["dev/*"])

    broadcast, started = hub.jobs.start_broadcast(
        targets,
        patterns=["dev/*"],
        skipped=[{"target": "contoso-lab/gpu1_linux", "status": "offline", "reason": "x"}],
        language="powershell",
        code="hostname",
        mode="subprocess",
        timeout_s=30,
        origin="test",
    )
    found, jobs = hub.jobs.get_broadcast(broadcast.id)
    hub.jobs.wait_all(jobs, 5)

    assert found is broadcast and jobs == started
    assert sorted(job.target.id for job in jobs) == ["dev/box1_windows", "dev/box2_windows"]
    assert all(job.state == SUCCEEDED and job.broadcast_id == broadcast.id for job in jobs)
    assert broadcast.skipped[0]["target"] == "contoso-lab/gpu1_linux"


def test_a_broadcast_with_every_target_skipped_is_still_retrievable(tmp_path):
    hub = Hub(tmp_path)

    broadcast, jobs = hub.jobs.start_broadcast(
        [],
        patterns=["dev/*"],
        skipped=[{"target": "dev/box1_windows", "status": "offline", "reason": "x"}],
        language="bash",
        code="true",
        mode="subprocess",
        timeout_s=30,
        origin="test",
    )

    assert jobs == []
    found, found_jobs = hub.jobs.get_broadcast(broadcast.id)
    assert found is broadcast and found_jobs == []


def test_a_job_that_cannot_start_is_not_left_queued(tmp_path, monkeypatch):
    hub = Hub(tmp_path, max_running=1, max_queued=1)

    def fail_to_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", fail_to_start)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        _run(hub, wait_s=0)
    monkeypatch.undo()

    assert hub.jobs.counts() == {"queued": 0, "running": 0, "retained": 0}
    assert _run(hub).state == SUCCEEDED


def test_audit_log_survives_unencodable_text(tmp_path):
    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(run=lambda *args: exec_result())})

    job = _run(hub, code="print('\ud800')")

    assert job.state == SUCCEEDED
    events = [json.loads(line) for line in hub.audit_path.read_text(encoding="utf-8").splitlines()]
    assert events[0]["code"] == "print('\ud800')"
