import base64
import contextlib
import copy
import hashlib
import json
import pathlib
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

import pytest
from hub_fakes import FakeListener, Hub, blocking_run, exec_result

from privy.hub.jobs import CANCELLED, RUNNING, SUCCEEDED, HubJob, JobManager, utc_iso
from privy.hub.listener_facts import ListenerInfoStore
from privy.hub.metrics import ReportSelection
from privy.hub.registry import ONLINE, ProbeResult
from privy.hub.rest import RestApi
from privy.hub.service import HubError, HubService
from privy.hub.shared_jobs import SharedAuditLog, SharedJob, SharedJobManager, job_record
from privy.hub.shared_metrics import SharedHubMetrics, SharedMetrics
from privy.hub.sql_store import StorageError


class Backend:
    def __init__(self, owner="one", records=None):
        self.owner = owner
        self.records = records if records is not None else {}
        self.events = []
        self.lock = threading.RLock()
        self.output_reads = 0
        self.cursor_read_calls = 0
        self.query_count = 0
        self.list_calls = []

    def put(self, kind, key, payload):
        with self.lock:
            self.events.append((kind, key, copy.deepcopy(payload)))
            self.records[kind, key] = copy.deepcopy(payload)

    enqueue = put

    def get(self, kind, key):
        self.query_count += 1
        with self.lock:
            return copy.deepcopy(self.records.get((kind, key)))

    def list(
        self,
        kind,
        *,
        limit=200,
        before=None,
        after=None,
        target=None,
        lease_id=None,
        client_id=None,
        states=None,
    ):
        self.query_count += 1
        self.list_calls.append(
            {"kind": kind, "limit": limit, "target": target, "lease_id": lease_id, "states": states}
        )
        with self.lock:
            rows = [copy.deepcopy(v) for (k, _), v in self.records.items() if k == kind]
        for name, wanted in (("target", target), ("lease_id", lease_id), ("client_id", client_id)):
            if wanted is not None:
                rows = [r for r in rows if str(r.get(name, "")).lower() == wanted.lower()]
        rows.sort(key=lambda r: r.get("created_at", r.get("at", 0)), reverse=True)
        if before is not None:
            rows = [r for r in rows if r.get("created_at", r.get("at", 0)) < before]
        if after is not None:
            rows = [r for r in rows if r.get("created_at", r.get("at", 0)) >= after]
        if states is not None:
            rows = [r for r in rows if r.get("state") in states]
        return rows[:limit]

    def count_jobs_by_target(self, *, lease_id=None):
        counts = {}
        with self.lock:
            rows = [copy.deepcopy(v) for (kind, _), v in self.records.items() if kind == "job"]
        for row in rows:
            if lease_id is None or row.get("lease_id") == lease_id:
                target = row.get("target")
                if target is not None:
                    counts[target] = counts.get(target, 0) + 1
        return counts

    def count_job_records(self, *, lease_id=None, states=None, after=None, before=None):
        with self.lock:
            rows = [copy.deepcopy(v) for (kind, _), v in self.records.items() if kind == "job"]
        return sum(
            1
            for row in rows
            if (lease_id is None or row.get("lease_id") == lease_id)
            and (states is None or row.get("state") in states)
            and (after is None or row.get("created_at", row.get("at", 0)) >= after)
            and (before is None or row.get("created_at", row.get("at", 0)) < before)
        )

    def count_jobs(self):
        states = [r["state"] for (kind, _), r in self.records.items() if kind == "job"]
        return {"running": states.count(RUNNING), "queued": states.count("queued"), "retained": len(states)}

    def output(self, job_id, stream, offset, limit):
        self.output_reads += 1
        rows = self.list("output", target=job_id, limit=100000)
        return sorted(
            [r for r in rows if r["stream"] == stream and r["end"] > offset and r["start"] < offset + limit],
            key=lambda r: r["start"],
        )

    def pieces(self, job_id, index, limit=200):
        self.output_reads += 1
        return sorted(
            [r for r in self.list("output", target=job_id, limit=100000) if r["index"] >= index],
            key=lambda r: r["index"],
        )[:limit]

    def read_cursors(self, job_id, streams):
        self.cursor_read_calls += 1
        with self.lock:
            return {stream: self.records.get(("cursor", f"{job_id}:{stream}"), 0) for stream in streams}

    def advance_cursor(self, job_id, stream, end):
        with self.lock:
            key = ("cursor", f"{job_id}:{stream}")
            previous = self.records.get(key, 0)
            self.records[key] = max(previous, end)
            return previous

    def request_cancel(self, job_id):
        value = {"job_id": job_id, "requested": True, "acknowledged": False}
        self.put("cancel", job_id, value)
        return value

    def pending_cancels(self):
        return [
            key
            for (kind, key), value in list(self.records.items())
            if kind == "cancel" and not value["acknowledged"] and self.get("job", key)["owner"] == self.owner
        ]

    def ack_cancel(self, job_id):
        with self.lock:
            self.records["cancel", job_id]["acknowledged"] = True

    def flush(self, timeout=10):
        return True

    def status(self):
        return {"mode": "azure_sql", "pending": 0}


def manager(hub, backend, **kwargs):
    return SharedJobManager(hub.registry, backend=backend, **kwargs)


def start(jobs, hub, **kwargs):
    return jobs.start_exec(
        hub.registry.resolve("dev/box1_windows"),
        language="bash",
        code="hello",
        mode="subprocess",
        timeout_s=30,
        origin="test",
        **kwargs,
    )


def test_completed_job_cursor_pages_preserve_multibyte_output(tmp_path):
    backend = Backend()
    hub = Hub(tmp_path)
    job = HubJob(
        job_type="exec",
        target=hub.registry.resolve("dev/box1_windows"),
        spec={"language": "python", "code": "text"},
        origin="test",
        output_limit=100,
    )
    data = ("\u20ac" * 2).encode()
    job.stdout.append(data)
    job.chunks.append(("stdout", 0, len(data)))
    job.state = SUCCEEDED
    backend.put("job", job.id, job_record(job, backend.owner))
    backend.put(
        "output",
        job.id + ":0",
        {
            "target": job.id,
            "job_id": job.id,
            "index": 0,
            "stream": "stdout",
            "start": 0,
            "end": len(data),
            "data": base64.b64encode(data).decode(),
        },
    )
    shared = SharedJob(backend, backend.get("job", job.id))
    first = shared.view(max_output_bytes=4)
    second = shared.view(max_output_bytes=4)
    assert first["stdout"] == "\u20ac" and first["stdout_offset"] == 3
    assert second["stdout"] == "\u20ac" and second["stdout_offset"] == 6


def test_admission_is_pinned_and_synchronously_persisted_before_execution(tmp_path):
    backend = Backend()
    pinned = []

    def run(request, on_output, on_submit, listener):
        assert pinned
        assert backend.get("job", pinned[0])["state"] in ("queued", RUNNING)
        on_submit("remote")
        on_output("stdout", b"ok")
        return exec_result()

    hub = Hub(tmp_path, {"dev/box1_windows": FakeListener(run=run)})
    jobs = manager(hub, backend, on_job=lambda job: pinned.append(job.id))
    finished = []
    jobs.on_finish = lambda job: finished.append(backend.get("job", job.id)["state"])
    try:
        job = start(jobs, hub, purpose="testing", lease_id="lease")
        jobs.wait(job, 5)
        assert finished == [SUCCEEDED]
        assert uuid.UUID(job.id).hex == job.id
        assert backend.get("job", job.id)["purpose"] == "testing"
        assert backend.get("job", job.id)["owner"] == backend.owner
    finally:
        jobs.close()


def test_failed_admission_never_launches_and_retains_pin_until_completion_retry(tmp_path):
    backend = Backend()
    hub = Hub(tmp_path)
    pins = []
    jobs = manager(hub, backend, cancel_poll_s=10, on_job=lambda job: pins.append(job.id))
    jobs.on_finish = lambda job: pins.remove(job.id)
    backend.put = lambda *args: (_ for _ in ()).throw(RuntimeError("SQL unavailable"))
    enqueue = backend.enqueue
    backend.enqueue = backend.put
    try:
        with pytest.raises(RuntimeError, match="SQL unavailable"):
            start(jobs, hub)
        assert len(pins) == 1
        assert not hub.listeners
        assert jobs._jobs == {}
        job_id = pins[0]
        backend.enqueue = enqueue
        jobs._retry_finishes()
        assert not pins and not jobs._pending_finishes
        assert backend.get("job", job_id)["state"] == "error"
    finally:
        jobs.close()


def test_restart_reads_complete_output_lazily_without_reexecuting(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend, output_limit_bytes=2)
    try:
        job = start(jobs, hub)
        jobs.wait(job, 5)
        other = manager(hub, Backend("two", backend.records))
        try:
            proxy = other.get(job.id)
            assert isinstance(proxy, SharedJob)
            assert other.backend.output_reads == 0
            assert proxy.summary()["state"] == SUCCEEDED
            assert proxy.read_output("stdout", 0, 100)["data"] == "ran bash: hello\n"
            assert len(hub.listeners["dev/box1_windows"].requests) == 1
            assert other.counts()["retained"] == 1
        finally:
            other.close()
    finally:
        jobs.close()


def test_list_summaries_use_one_snapshot_without_per_job_queries(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    try:
        for index in range(200):
            job = HubJob(
                job_type="exec",
                target=hub.registry.resolve("dev/box1_windows"),
                spec={"code": "summary", "language": "python"},
                origin="test",
                output_limit=0,
            )
            job.id = f"summary-{index}"
            backend.put("job", job.id, job_record(job, backend.owner))
        page = jobs.list(limit=200)
        queried = backend.query_count
        assert len([job.summary(include_created=True) for job in page]) == 200
        assert backend.query_count == queried
        assert backend.output_reads == 0
    finally:
        jobs.close()


def test_shared_summary_reads_once_and_zero_wait_does_not_refresh(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    job = start(jobs, hub)
    try:
        proxy = SharedJob(backend, backend.get("job", job.id))
        before = backend.query_count
        proxy.summary()
        assert backend.query_count == before + 1
        before = backend.query_count
        jobs.wait(proxy, 0)
        assert backend.query_count == before
    finally:
        jobs.close()


def test_shared_view_batches_cursor_reads_and_output_can_reuse_job_snapshot(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    job = HubJob(
        job_type="exec",
        target=hub.registry.resolve("dev/box1_windows"),
        spec={"language": "bash", "code": "x"},
        origin="test",
        output_limit=100,
    )
    try:
        jobs._admit(job)
        jobs._append_output(job, "stdout", b"abcdef")
        proxy = SharedJob(backend, backend.get("job", job.id))
        before = backend.cursor_read_calls
        view = proxy.view(max_output_bytes=100, advance=False, refresh=False)
        assert view["stdout"] == "abcdef"
        assert backend.cursor_read_calls == before + 1
        before = backend.query_count
        result = proxy.read_output("stdout", 0, 100, refresh=False)
        assert result["data"] == "abcdef"
        assert backend.query_count == before + 1
    finally:
        jobs.close()


def test_shared_cursors_are_atomic_and_do_not_reserve_missing_output(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    job = HubJob(
        job_type="exec",
        target=hub.registry.resolve("dev/box1_windows"),
        spec={"language": "bash", "code": "x"},
        origin="test",
        output_limit=100,
    )
    jobs = manager(hub, backend)
    try:
        jobs._admit(job)
        jobs._append_output(job, "stdout", b"abcdef")
        one = SharedJob(backend, backend.get("job", job.id))
        two = SharedJob(Backend("two", backend.records), backend.get("job", job.id))
        assert one.view(max_output_bytes=100)["stdout"] == "abcdef"
        assert two.view(max_output_bytes=100)["stdout"] == ""
        jobs._append_output(job, "stdout", b"ghi")
        assert two.view(max_output_bytes=100)["stdout"] == "ghi"
        assert one.view(max_output_bytes=100, offsets={"stdout": 0})["stdout"] == "abcdefghi"
        assert backend.records["cursor", f"{job.id}:stdout"] == 9
    finally:
        jobs.close()


def test_shared_partial_utf8_and_missing_chunks_do_not_create_cursor_gaps(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    job = HubJob(
        job_type="exec",
        target=hub.registry.resolve("dev/box1_windows"),
        spec={"language": "bash", "code": "x"},
        origin="test",
        output_limit=100,
    )
    try:
        jobs._admit(job)
        jobs._append_output(job, "stdout", b"\xc3")
        proxy = SharedJob(backend, backend.get("job", job.id))
        assert proxy.view(max_output_bytes=100)["stdout_offset"] == 0
        jobs._append_output(job, "stdout", b"\xa9")
        assert proxy.view(max_output_bytes=100)["stdout"] == "é"
        rows = backend.list("output", target=job.id)
        backend.records.pop(("output", f"{job.id}:{1:020d}"))
        backend.records.pop(("cursor", f"{job.id}:stdout"))
        assert proxy.view(max_output_bytes=100)["stdout_offset"] == 0
        backend.put("output", f"{job.id}:{1:020d}", next(r for r in rows if r["index"] == 1))
        assert proxy.view(max_output_bytes=100)["stdout"] == "é"
    finally:
        jobs.close()


def test_pieces_page_in_arrival_order_and_wait_refreshes_remote_jobs(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    job = HubJob(
        job_type="exec",
        target=hub.registry.resolve("dev/box1_windows"),
        spec={"language": "bash", "code": "x"},
        origin="test",
        output_limit=2,
    )
    try:
        jobs._admit(job)
        for i in range(205):
            jobs._append_output(job, "stderr" if i % 2 else "stdout", b"x")
        proxy = SharedJob(backend, backend.get("job", job.id))
        first, index, done = proxy.next_pieces(0, 0)
        assert len(first) == 200 and index == 200 and not done
        second, index, _ = proxy.next_pieces(index, 0)
        assert len(second) == 5 and index == 205
        assert second[0].stream == "stdout"

        def finish():
            with job.cond:
                jobs._finish_locked(job, SUCCEEDED)

        timer = threading.Timer(0.05, finish)
        timer.start()
        try:
            jobs.wait(proxy, 1)
            assert proxy.done
        finally:
            timer.join()
    finally:
        jobs.close()


def test_cancellation_routes_to_owner_and_is_acknowledged(tmp_path):
    listener = FakeListener(run=blocking_run)
    hub = Hub(tmp_path, {"dev/box1_windows": listener})
    backend = Backend()
    jobs = manager(hub, backend, cancel_poll_s=0.01)
    other = manager(hub, Backend("two", backend.records), cancel_poll_s=0.01)
    try:
        job = start(jobs, hub)
        assert listener.started.wait(5)
        proxy = other.cancel(job.id)
        assert backend.get("cancel", job.id)["requested"]
        jobs.wait(proxy, 5)
        assert proxy.summary()["state"] == CANCELLED
        deadline = time.monotonic() + 2
        while not backend.get("cancel", job.id)["acknowledged"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend.get("cancel", job.id)["acknowledged"]
        assert listener.cancelled == ["remote-blocking"]
    finally:
        jobs.close()
        other.close()
        listener.cancel_event.set()


def test_job_and_audit_payloads_do_not_contain_credentials_or_local_paths(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    target = hub.registry.resolve("dev/box1_windows")
    job = HubJob(
        job_type="upload",
        target=target,
        spec={"local_path": r"C:\private\input", "remote_path": "/remote/input", "overwrite": True},
        origin="test",
        output_limit=0,
    )
    job.client = object()
    job.request = object()
    try:
        jobs._admit(job)
        SharedAuditLog(backend).record("transfer", **job.spec, token="SECRET", client=object())
        data = str(backend.events)
        assert target.key not in data and "SECRET" not in data
        assert r"C:\private\input" not in data
        assert "<owner-local>" in data and "/remote/input" in data
        assert "object at" not in data
    finally:
        jobs.close()


def test_listener_facts_and_metrics_survive_restart_and_include_other_hubs(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    metrics = SharedMetrics(backend, clock=lambda: time.time() + 1)
    jobs.on_job = metrics.job_added
    target = hub.registry.resolve("dev/box1_windows")
    probe = ProbeResult(ONLINE, time.time(), latency_ms=12, info={"platform": {"system": "Windows"}})
    store = ListenerInfoStore(backend=backend)
    store.record(target, probe)
    metrics.probe(target, probe)
    metrics.call("rest", 200, 0.1)
    try:
        job = start(jobs, hub, purpose="persisted")
        jobs.wait(job, 5)
        second = Backend("two", backend.records)
        restored = SharedMetrics(second, clock=lambda: time.time() + 2)
        assert second.output_reads == 0
        assert ListenerInfoStore(backend=second).last(target.id) == (probe.info, probe.checked_at)
        assert restored.entry(job.id).view()["state"] == SUCCEEDED
        report = restored.report([target], {}, selection=ReportSelection("all"))
        assert report["targets"][0]["jobs"] == 1
        assert report["targets"][0]["latency_p50_ms"] == 12
        assert report["targets"][0]["purpose"]["latest"] == "persisted"
        assert report["calls"]["total"] == 1
        assert second.output_reads == 0
        assert restored.entry(job.id).output(100)[0][0].data == b"ran bash: hello\n"
    finally:
        jobs.close()


def test_output_records_have_absolute_offsets_and_immutable_chunk_ids(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    job = HubJob(
        job_type="exec",
        target=hub.registry.resolve("dev/box1_windows"),
        spec={"language": "bash", "code": "x"},
        origin="test",
        output_limit=2,
    )
    try:
        jobs._admit(job)
        jobs._append_output(job, "stdout", b"abc")
        jobs._append_output(job, "stderr", b"d")
        jobs._append_output(job, "stdout", b"ef")
        rows = sorted(backend.list("output", target=job.id), key=lambda r: r["index"])
        assert [(r["stream"], r["start"], r["end"]) for r in rows] == [
            ("stdout", 0, 3),
            ("stderr", 0, 1),
            ("stdout", 3, 5),
        ]
        assert b"".join(base64.b64decode(r["data"]) for r in rows) == b"abcdef"
    finally:
        jobs.close()


def test_completion_journal_failure_keeps_pin_until_retry(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    pinned = []
    jobs = manager(hub, backend, cancel_poll_s=10, on_job=lambda job: pinned.append(job.id))
    jobs.on_finish = lambda job: pinned.remove(job.id)
    enqueue = backend.enqueue
    failed = [True]

    def persist(kind, key, payload):
        if kind == "job" and payload["state"] == SUCCEEDED and failed[0]:
            raise StorageError("journal full")
        enqueue(kind, key, payload)

    backend.enqueue = persist
    try:
        job = start(jobs, hub)
        local = jobs._jobs[job.id]
        deadline = time.monotonic() + 5
        while local.state != SUCCEEDED and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pinned == [job.id]
        assert backend.get("job", job.id)["state"] != SUCCEEDED
        failed[0] = False
        jobs._retry_finishes()
        assert pinned == []
        assert backend.get("job", job.id)["state"] == SUCCEEDED
    finally:
        jobs.close()


def test_cancellation_failure_retries_without_acknowledging(tmp_path):
    listener = FakeListener(run=blocking_run)
    hub = Hub(tmp_path, {"dev/box1_windows": listener})
    backend = Backend()
    jobs = manager(hub, backend, cancel_poll_s=10)
    try:
        job = start(jobs, hub)
        assert listener.started.wait(5)
        local = jobs._jobs[job.id]
        cancel = local.client.cancel
        local.client.cancel = lambda *args: (_ for _ in ()).throw(RuntimeError("disconnected"))
        jobs.cancel(job.id)
        jobs._process_cancels()
        assert backend.get("cancel", job.id)["acknowledged"] is False
        local.client.cancel = cancel
        jobs._process_cancels()
        assert backend.get("cancel", job.id)["acknowledged"] is True
        jobs.wait(job, 5)
        assert job.done
    finally:
        listener.cancel_event.set()
        jobs.close()


def test_async_summary_does_not_move_cursor_over_uncommitted_output(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    job = HubJob(
        job_type="exec",
        target=hub.registry.resolve("dev/box1_windows"),
        spec={"language": "bash", "code": "x"},
        origin="test",
        output_limit=2,
    )
    try:
        jobs._admit(job)
        jobs._append_output(job, "stdout", b"abc")
        jobs._append_output(job, "stdout", b"def")
        backend.records.pop(("output", f"{job.id}:{0:020d}"))
        proxy = SharedJob(backend, backend.get("job", job.id))
        view = proxy.view(max_output_bytes=3)
        assert view["stdout"] == "" and view["stdout_offset"] == 0
        assert backend.records.get(("cursor", f"{job.id}:stdout"), 0) == 0
        pieces, index, complete = proxy.next_pieces(0, 0)
        assert pieces == [] and index == 0 and not complete
    finally:
        jobs.close()


def test_simultaneous_readers_reserve_output_only_once(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    try:
        job = start(jobs, hub)
        jobs.wait(job, 5)
        proxies = [jobs.get(job.id), jobs.get(job.id)]
        gate = threading.Barrier(2)
        output = []

        def read(proxy):
            gate.wait()
            output.append(proxy.view(max_output_bytes=100)["stdout"])

        threads = [threading.Thread(target=read, args=(p,)) for p in proxies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert sorted(output) == ["", "ran bash: hello\n"]
    finally:
        jobs.close()


def test_broadcast_recovery_and_history_filters(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    try:
        broadcast, children = jobs.start_broadcast(
            [(hub.registry.resolve("dev/box1_windows"), "bash")],
            patterns=["box1*"],
            skipped=[],
            code="hello",
            mode="subprocess",
            timeout_s=30,
            origin="test",
            lease_id="lease",
        )
        jobs.wait_all(children, 5)
        other = manager(hub, Backend("two", backend.records))
        try:
            recovered, recovered_jobs = other.get_broadcast(broadcast.id)
            assert recovered == broadcast
            assert recovered_jobs[0].summary()["state"] == SUCCEEDED
            assert len(other.list(target_id="DEV/BOX1_WINDOWS", lease_id="lease")) == 1
            assert other.list(lease_id="different") == []
        finally:
            other.close()
    finally:
        jobs.close()


def test_service_reports_sql_status_and_storage_errors_as_503(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    service = HubService(
        registry=hub.registry, jobs=jobs, version="test", metrics=SharedHubMetrics(backend, hub.registry)
    )
    try:
        assert service.summary()["storage"] == backend.status()
        backend.get = lambda *args: (_ for _ in ()).throw(StorageError("credential-bearing SQL error"))
        with pytest.raises(HubError) as error:
            service.invoke("get_job", {"job_id": "missing", "wait_s": 0}, origin="test")
        assert error.value.status == 503 and error.value.code == "storage_unavailable"
        assert "credential-bearing" not in str(error.value)
    finally:
        jobs.close()


@pytest.mark.parametrize("operation", ["run", "broadcast"])
@pytest.mark.parametrize("failure", ["get", "read_cursors", "output", "advance_cursor"])
def test_sql_read_failure_after_launch_returns_admitted_job_id(tmp_path, monkeypatch, operation, failure):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend, cancel_poll_s=10)
    service = HubService(registry=hub.registry, jobs=jobs, version="test")
    launch = JobManager._launch
    original = getattr(backend, failure)
    launched = []

    def fail(*args, **kwargs):
        raise StorageError("SQL unavailable after worker launch")

    def launch_then_fail(self, job, body):
        result = launch(self, job, body)
        launched.append(job.id)
        monkeypatch.setattr(backend, failure, fail)
        return result

    monkeypatch.setattr(JobManager, "_launch", launch_then_fail)
    args = {"code": "hello", "language": "bash", "wait_s": 1}
    args.update(
        {"target": "dev/box1_windows"}
        if operation == "run"
        else {"targets": ["dev/box1_windows"], "only_online": False}
    )
    try:
        response = RestApi(service).handle(
            "POST",
            "/v1/jobs" if operation == "run" else "/v1/broadcasts",
            {},
            {"Content-Type": "application/json"},
            json.dumps(args).encode(),
        )
        assert response.status == 200
        data = json.loads(response.body())
        views = [data] if operation == "run" else data["jobs"]
        assert [view["job_id"] for view in views] == launched
        assert views[0]["stdout"] == "ran bash: hello\n"
        assert len(hub.listeners["dev/box1_windows"].requests) == 1
        if failure != "get":
            assert backend.get("job", launched[0])["state"] == SUCCEEDED
    finally:
        monkeypatch.setattr(backend, failure, original)
        jobs.close()


def test_shared_history_pagination_keeps_equal_timestamp_records(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    try:
        for i in range(405):
            job = HubJob(
                job_type="exec",
                target=hub.registry.resolve("dev/box1_windows"),
                spec={"language": "bash", "code": str(i)},
                origin="test",
                output_limit=0,
            )
            job.created_at = 100.0 if i < 305 else 200.0
            jobs._admit(job)
        metrics = SharedMetrics(backend)
        assert len(metrics.entries()) == 405
        assert jobs.counts()["retained"] == 405
        assert backend.output_reads == 0
    finally:
        jobs.close()


def test_shared_purpose_history_preserves_non_job_terminal_usage():
    backend = Backend()
    at = time.time()
    metrics = SharedMetrics(backend, clock=lambda: at)
    metrics.purposes.record("dev/box1_windows", "terminal usage")
    restored = SharedMetrics(Backend("two", backend.records), clock=lambda: at + 1)
    history = restored.purposes.history("dev/box1_windows")
    assert history["latest"] == "terminal usage" and history["ephemeral"] is False
    assert history["groups"][0]["count"] == 1


def test_restart_keeps_running_owner_metadata_without_recreating_worker(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    job = HubJob(
        job_type="exec",
        target=hub.registry.resolve("dev/box1_windows"),
        spec={"language": "bash", "code": "x"},
        origin="test",
        output_limit=0,
    )
    job.state = RUNNING
    try:
        jobs._admit(job)
        restarted = manager(hub, Backend("new-incarnation", backend.records))
        try:
            view = restarted.get(job.id).summary()
            assert view["state"] == RUNNING and view["done"] is False
            assert view["owner"] == "one"
            assert not restarted._jobs and not hub.listeners
            restarted.cancel(job.id)
            restarted._process_cancels()
            assert backend.get("cancel", job.id)["acknowledged"] is False
        finally:
            restarted.close()
    finally:
        jobs.close()


def test_history_output_tail_pages_more_than_200_chunks(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    job = HubJob(
        job_type="exec",
        target=hub.registry.resolve("dev/box1_windows"),
        spec={"language": "bash", "code": "x"},
        origin="test",
        output_limit=2,
    )
    try:
        jobs._admit(job)
        for _ in range(405):
            jobs._append_output(job, "stdout", b"x")
        entry = SharedMetrics(backend).entry(job.id)
        pieces, dropped = entry.output(500)
        assert sum(len(piece.data) for piece in pieces) == 405 and dropped is False
        pieces, dropped = entry.output(250)
        assert sum(len(piece.data) for piece in pieces) == 250 and dropped is True
    finally:
        jobs.close()


def audit_fixture(path, records):
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    return path


def test_audit_import_aggregates_recent_history_and_is_idempotent(tmp_path):
    now = time.time()
    records = [
        {"ts": utc_iso(now - 8 * 86400), "event": "job.start", "job_id": "old", "target": "dev/box1_windows"},
        {
            "ts": utc_iso(now - 60),
            "event": "job.start",
            "job_id": "legacy",
            "target": "dev/box1_windows",
            "type": "exec",
            "language": "bash",
            "code": "echo imported",
            "purpose": "migration",
            "lease_id": "file-lease",
            "client_id": "file-client",
            "origin": "rest",
        },
        {
            "ts": utc_iso(now - 59),
            "event": "job.finish",
            "job_id": "legacy",
            "state": SUCCEEDED,
            "exit_code": 0,
            "duration_ms": 25,
            "stdout_bytes": 100,
        },
        {
            "ts": utc_iso(now - 50),
            "event": "job.start",
            "job_id": "unfinished",
            "target": "dev/box1_windows",
            "type": "upload",
            "local_path": r"C:\private\source",
            "remote_path": "/remote/source",
            "overwrite": True,
            "token": "SECRET",
            "lease_id": "pinned",
        },
    ]
    path = audit_fixture(tmp_path / "audit.jsonl", records)
    fixture_home = tmp_path / "fixture"
    fixture_home.mkdir()
    hub = Hub(fixture_home)
    backend = Backend()
    jobs = manager(hub, backend)
    finished = []
    jobs.on_finish = lambda job: finished.append(job.id)
    try:
        assert jobs.import_audit(path, now=now) == 2
        imported = backend.get("job", "legacy")
        assert imported["state"] == SUCCEEDED and imported["exit_code"] == 0
        assert imported["lease_id"] == "file-lease" and imported["client_id"] == "file-client"
        assert imported["duration_ms"] == 25
        assert imported["stdout_bytes"] == 0 and imported["legacy_stdout_bytes"] == 100
        assert jobs.get("legacy").read_output("stdout", 0, 100)["eof"] is True
        unfinished = backend.get("job", "unfinished")
        assert unfinished["state"] == "unknown" and unfinished["finished_at"] is None
        assert unfinished["owner"].startswith("file-import:")
        assert unfinished["spec"]["local_path"] == "<owner-local>"
        assert jobs.get("unfinished").done is False
        assert backend.get("job", "old") is None and not finished and not jobs._jobs
        assert "SECRET" not in str(backend.events) and r"C:\private\source" not in str(backend.events)
        event_count = len(backend.events)
        assert jobs.import_audit(path, now=now + 1) == 0
        assert len(backend.events) == event_count
        restarted = manager(hub, Backend("two", backend.records))
        try:
            assert restarted.import_audit(path, now=now + 2) == 0
            history = SharedMetrics(restarted.backend).entries()
            assert {e.id for e in history} == {"legacy", "unfinished"}
            assert restarted.backend.output_reads == 0
        finally:
            restarted.close()
    finally:
        jobs.close()


def test_audit_import_marks_only_after_durable_flush_and_retries(tmp_path):
    hub = Hub(tmp_path)
    now = time.time()
    path = audit_fixture(
        tmp_path / "legacy-audit.jsonl",
        [
            {
                "ts": utc_iso(now - 1),
                "event": "job.start",
                "job_id": "imported",
                "target": "dev/box1_windows",
                "type": "exec",
                "language": "bash",
                "code": "echo safe",
            },
        ],
    )
    backend = Backend()
    jobs = manager(hub, backend)
    enqueue = backend.enqueue
    failed = [True]

    def persist(kind, key, payload):
        if kind == "audit" and failed[0]:
            raise StorageError("outbox full")
        enqueue(kind, key, payload)

    backend.enqueue = persist
    try:
        with pytest.raises(StorageError, match="outbox full"):
            jobs.import_audit(path, now=now)
        assert backend.list("import") == []
        assert backend.get("job", "imported") is not None
        failed[0] = False
        assert jobs.import_audit(path, now=now) == 0
        markers = backend.list("import")
        assert len(markers) == 1
        assert markers[0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert len(backend.list("audit")) == 1
    finally:
        jobs.close()


def test_audit_import_resume_skips_already_delivered_audit_rows(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    now = time.time()
    path = audit_fixture(
        tmp_path / "legacy.jsonl",
        [
            {
                "ts": utc_iso(now - 1),
                "event": "job.start",
                "job_id": "resume",
                "target": "dev/box1_windows",
                "type": "exec",
            },
            {"ts": utc_iso(now), "event": "job.finish", "job_id": "resume", "state": SUCCEEDED},
        ],
    )
    backend.flush = lambda timeout=10: False
    try:
        with pytest.raises(StorageError, match="durable delivery"):
            jobs.import_audit(path, now=now)
        delivered = list(backend.events)
        assert len(delivered) == 3
        backend.flush = lambda timeout=10: True
        assert jobs.import_audit(path, now=now) == 0
        assert backend.events[:-1] == delivered
        assert backend.events[-1][0] == "import"
    finally:
        jobs.close()


class BulkImportBackend(Backend):
    def __init__(self):
        super().__init__()
        self.batches = []
        self.fail_after = None

    def existing(self, kind, keys):
        return {key for key in keys if (kind, key) in self.records}

    def import_records(self, records):
        if self.fail_after is not None and len(self.batches) >= self.fail_after:
            raise StorageError("interrupted SQL batch")
        self.batches.append(copy.deepcopy(records))
        inserted = set()
        for kind, key, payload in records:
            if (kind, key) not in self.records:
                self.put(kind, key, payload)
                inserted.add((kind, key))
        return inserted

    def enqueue(self, kind, key, payload):
        pytest.fail("bulk import must not enqueue on the interactive delivery journal")


def test_audit_bulk_import_resumes_committed_batches_and_marker_is_last(tmp_path):
    hub = Hub(tmp_path)
    backend = BulkImportBackend()
    jobs = manager(hub, backend)
    now = time.time()
    path = audit_fixture(
        tmp_path / "legacy.jsonl",
        [
            {
                "ts": utc_iso(now - 1),
                "event": "job.start",
                "job_id": f"job-{i}",
                "target": "dev/box1_windows",
                "type": "exec",
            }
            for i in range(600)
        ],
    )
    backend.fail_after = 1
    try:
        with pytest.raises(StorageError, match="interrupted SQL batch"):
            jobs.import_audit(path, now=now)
        committed = set(backend.records)
        assert len(committed) == 500 and backend.list("import") == []
        backend.fail_after = None
        assert jobs.import_audit(path, now=now) == 350
        resumed = backend.batches[1:]
        assert all(len(batch) <= 500 for batch in resumed)
        assert not any((kind, key) in committed for batch in resumed for kind, key, _ in batch)
        assert len(backend.list("job", limit=1000)) == 600
        assert len(backend.list("audit", limit=1000)) == 600
        assert len(resumed[-1]) == 1 and resumed[-1][0][0] == "import"
        queries, batches = backend.query_count, len(backend.batches)
        assert jobs.import_audit(path, now=now + 1) == 0
        assert backend.query_count == queries + 1 and len(backend.batches) == batches
    finally:
        jobs.close()


def test_audit_bulk_import_refuses_marker_when_source_changes_during_last_commit(tmp_path):
    hub = Hub(tmp_path)
    backend = BulkImportBackend()
    jobs = manager(hub, backend)
    now = time.time()
    path = audit_fixture(
        tmp_path / "legacy.jsonl",
        [
            {
                "ts": utc_iso(now),
                "event": "job.start",
                "job_id": "mutated",
                "target": "dev/box1_windows",
                "type": "exec",
            }
        ],
    )
    commit = backend.import_records

    def mutate(records):
        result = commit(records)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("{}\n")
        return result

    backend.import_records = mutate
    try:
        with pytest.raises(StorageError, match="changed during import"):
            jobs.import_audit(path, now=now)
        assert backend.list("import") == []
    finally:
        jobs.close()


def test_audit_import_rejects_unflushed_spool_and_never_overwrites_shared_jobs(tmp_path):
    hub = Hub(tmp_path)
    now = time.time()
    backend = Backend()
    jobs = manager(hub, backend)
    try:
        live = start(jobs, hub)
        jobs.wait(live, 5)
        before = backend.get("job", live.id)
        path = audit_fixture(
            tmp_path / "legacy.jsonl",
            [
                {
                    "ts": utc_iso(now),
                    "event": "job.start",
                    "job_id": live.id,
                    "target": live.target.id,
                    "type": "exec",
                    "language": "bash",
                    "code": "replace",
                },
            ],
        )
        backend.flush = lambda timeout=10: False
        with pytest.raises(StorageError):
            jobs.import_audit(path, now=now)
        assert backend.list("import") == []
        backend.flush = lambda timeout=10: True
        assert jobs.import_audit(path, now=now) == 0
        assert backend.get("job", live.id) == before
    finally:
        jobs.close()


def test_audit_import_skips_malformed_lines_and_finish_only_history(tmp_path):
    hub = Hub(tmp_path)
    now = time.time()
    path = tmp_path / "legacy.jsonl"
    path.write_text(
        "\n".join(
            [
                "not-json",
                "[]",
                '{"ts":"invalid","event":"job.start"}',
                json.dumps(
                    {"ts": utc_iso(now), "event": "job.finish", "job_id": "finish-only", "state": SUCCEEDED}
                ),
                json.dumps(
                    {
                        "ts": utc_iso(now),
                        "event": "job.start",
                        "job_id": "valid",
                        "target": "dev/box1_windows",
                        "type": "exec",
                        "language": "bash",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    backend = Backend()
    jobs = manager(hub, backend)
    try:
        assert jobs.import_audit(path, now=now) == 1
        assert backend.get("job", "finish-only") is None
        assert jobs.get("valid").summary()["state"] == "unknown"
    finally:
        jobs.close()


def test_shared_report_query_count_is_page_bounded_not_per_job(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    target = hub.registry.resolve("dev/box1_windows")
    at = time.time()
    try:
        for _ in range(150):
            job = HubJob(
                job_type="exec",
                target=target,
                spec={"language": "bash", "code": "x"},
                origin="test",
                output_limit=0,
            )
            job.created_at = at - 10
            jobs._admit(job)
        metrics = SharedMetrics(backend, hub.registry, clock=lambda: at)
        backend.query_count = 0
        report = metrics.report([target], {}, range_name="1h")
        assert report["jobs"]["total"] == 150 and report["jobs"]["queued"] == 150
        assert backend.query_count <= 8
        assert backend.output_reads == 0
        backend.query_count = 0
        assert jobs.counts()["retained"] == 150
        assert backend.query_count == 0
    finally:
        jobs.close()


def test_shared_report_reuses_supplied_job_counts(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    target = hub.registry.resolve("dev/box1_windows")
    count_calls = []
    original_count_jobs = backend.count_jobs

    def count_jobs():
        count_calls.append(True)
        return original_count_jobs()

    backend.count_jobs = count_jobs
    try:
        metrics = SharedMetrics(backend, hub.registry)
        report = metrics.report([target], {}, jobs_running=0, jobs_queued=0, jobs_retained=0, range_name="1h")
        assert report["kept_jobs"] == 0
        assert count_calls == []
    finally:
        jobs.close()


def test_audit_import_pending_delivery_never_commits_marker(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    now = time.time()
    path = audit_fixture(
        tmp_path / "legacy.jsonl",
        [
            {
                "ts": utc_iso(now),
                "event": "job.start",
                "job_id": "partial",
                "target": "dev/box1_windows",
                "type": "exec",
            },
        ],
    )
    calls = []

    def flush(timeout=10):
        calls.append(timeout)
        return False

    backend.flush = flush
    try:
        with pytest.raises(StorageError, match="durable delivery"):
            jobs.import_audit(path, now=now)
        assert backend.get("job", "partial") is not None
        assert backend.list("import") == []
    finally:
        backend.flush = lambda timeout=10: True
        jobs.close()


def test_audit_import_detects_source_mutation_before_marker(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    now = time.time()
    path = audit_fixture(
        tmp_path / "legacy.jsonl",
        [
            {
                "ts": utc_iso(now),
                "event": "job.start",
                "job_id": "partial",
                "target": "dev/box1_windows",
                "type": "exec",
            },
        ],
    )
    enqueue = backend.enqueue

    def changing_source(kind, key, payload):
        enqueue(kind, key, payload)
        if kind == "job":
            with path.open("a", encoding="utf-8") as stream:
                stream.write("{}\n")

    backend.enqueue = changing_source
    try:
        with pytest.raises(StorageError, match="changed during import"):
            jobs.import_audit(path, now=now)
        assert backend.list("import") == []
    finally:
        jobs.close()


def test_audit_import_uses_bounded_tail_and_skips_oversized_records(tmp_path, monkeypatch):
    import privy.hub.shared_jobs as shared

    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    now = time.time()
    path = tmp_path / "legacy.jsonl"
    valid = json.dumps(
        {
            "ts": utc_iso(now),
            "event": "job.start",
            "job_id": "tail",
            "target": "dev/box1_windows",
            "type": "exec",
        }
    )
    path.write_text("x" * 1500 + "\n" + valid + "\n", encoding="utf-8")
    monkeypatch.setattr(shared, "IMPORT_MAX_BYTES", 512)
    monkeypatch.setattr(shared, "IMPORT_LINE_BYTES", 256)
    try:
        assert jobs.import_audit(path, now=now) == 1
        assert jobs.get("tail").summary()["state"] == "unknown"
    finally:
        jobs.close()


def test_shared_window_report_uses_sql_lower_bound_and_reuses_purpose_snapshot(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    now = time.time()
    target = hub.registry.resolve("dev/box1_windows")
    try:
        for seconds in (2 * 86400, 5):
            job = HubJob(
                job_type="exec",
                target=target,
                spec={"language": "bash", "code": "x"},
                origin="test",
                output_limit=0,
                purpose="same work",
            )
            job.created_at = now - seconds
            jobs._admit(job)
        observed = []
        listing = backend.list

        def track(kind, **kwargs):
            observed.append((kind, kwargs))
            return listing(kind, **kwargs)

        backend.list = track
        report = SharedMetrics(backend, clock=lambda: now).report([target], {}, range_name="1h")
        assert report["jobs"]["total"] == 1 and report["kept_jobs"] == 2
        assert report["restored_jobs"] == 1
        assert report["targets"][0]["purpose"]["latest"] == "same work"
        job_pages = [kwargs for kind, kwargs in observed if kind == "job" and kwargs["limit"] == 200]
        assert len(job_pages) == 1 and job_pages[0]["after"] is not None
        assert all(kwargs.get("after") is not None for kind, kwargs in observed if kind in ("probe", "call"))
    finally:
        jobs.close()


def test_audit_import_redacts_command_secrets_and_finish_local_paths(tmp_path):
    hub = Hub(tmp_path)
    backend = Backend()
    jobs = manager(hub, backend)
    now = time.time()
    path = audit_fixture(
        tmp_path / "legacy.jsonl",
        [
            {
                "ts": utc_iso(now - 1),
                "event": "job.start",
                "job_id": "transfer",
                "target": "dev/box1_windows",
                "type": "download",
                "local_path": r"C:\sensitive\file",
                "remote_path": "/remote/file",
            },
            {
                "ts": utc_iso(now),
                "event": "job.finish",
                "job_id": "transfer",
                "state": "failed",
                "error": r"unable to write C:\sensitive\file",
            },
            {
                "ts": utc_iso(now),
                "event": "job.start",
                "job_id": "code",
                "target": "dev/box1_windows",
                "type": "exec",
                "code": "password='do-not-store'; token=abc123; echo SharedAccessSignature sr=secret&sig=abc",
                "connection_string": "private-database",
                "cwd": r"C:\another\private\path",
                "stdout": "do-not-store-arbitrary-output",
            },
        ],
    )
    try:
        jobs.import_audit(path, now=now)
        serialized = json.dumps(backend.events)
        assert r"C:\sensitive\file" not in str(backend.events)
        assert "do-not-store" not in serialized and "abc123" not in serialized and "sig=abc" not in serialized
        assert "private-database" not in serialized and "another" not in serialized
        assert "<owner-local>" in serialized and "<redacted>" in serialized
    finally:
        jobs.close()


class ProcessBackend(Backend):
    """In-process SQL coordination fake used by the real subprocess hub harness."""

    def __init__(self, settings, home, *, connect=None):
        super().__init__(owner=uuid.uuid4().hex)
        self.coordination = {}

    def check_config(self, name, payload):
        self.coordination["config", name] = json.dumps(payload)

    def get_config(self, name):
        raw = self.coordination.get(("config", name))
        return json.loads(raw) if raw is not None else None

    def set_migration_status(self, status):
        self.migration_status = dict(status)

    @contextlib.contextmanager
    def transaction(self, lock=None, **kwargs):
        with self.lock:
            yield ProcessCursor(self)

    def close(self):
        return self.flush()

    def stop_worker(self):
        pass

    def health(self):
        return self.status()


class ProcessConnections:
    def __init__(self, settings):
        self.unavailable = threading.Event()

    def __call__(self):
        raise AssertionError("fake backend must not open a SQL connection")

    def close(self):
        pass


class ProcessCursor:
    def __init__(self, backend):
        self.backend = backend
        self.rows = []
        self.result_sets = []
        self.result_set_index = 0

    def execute(self, sql, *args):
        self.rows = []
        self.result_sets = []
        self.result_set_index = 0
        if "SELECT CASE WHEN EXISTS" in sql:
            self.result_sets = [
                [(1,)],
                [(time.time(),)],
                [(self.backend.coordination["config", "targets"],)],
                [
                    (kind, key, raw)
                    for (kind, key), raw in self.backend.coordination.items()
                    if kind in ("client", "lease", "use")
                ],
            ]
            self.rows = self.result_sets[0]
        elif "DATEDIFF_BIG" in sql:
            self.rows = [(time.time(),)]
        elif "SELECT payload" in sql and "id='targets'" in sql:
            self.rows = [(self.backend.coordination["config", "targets"],)]
        elif "SELECT payload" in sql and "kind='import'" in sql:
            raw = self.backend.coordination.get(("import", args[0]))
            self.rows = [(raw,)] if raw is not None else []
        elif "SELECT kind,id,payload" in sql:
            self.rows = [
                (kind, key, raw)
                for (kind, key), raw in self.backend.coordination.items()
                if kind in ("client", "lease", "use")
            ]
        elif sql.startswith("INSERT dbo.privy_coordination"):
            kind, key, raw = ("import", *args) if "VALUES('import'" in sql else args
            self.backend.coordination[kind, key] = raw
        elif sql.startswith("UPDATE dbo.privy_coordination"):
            raw, kind, key = args
            self.backend.coordination[kind, key] = raw
        elif sql.startswith("DELETE dbo.privy_coordination"):
            self.backend.coordination.pop(tuple(args), None)
        else:
            raise AssertionError(f"Unexpected SQL in process harness fake: {sql}")
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows

    def nextset(self):
        if self.result_set_index + 1 >= len(self.result_sets):
            return False
        self.result_set_index += 1
        self.rows = self.result_sets[self.result_set_index]
        return True


@pytest.mark.parametrize("stale_policy", [False, True])
def test_sql_policy_restores_missing_home_and_ignores_stale_local_policy(tmp_path, stale_policy):
    import yaml

    from privy.hub.leases import DEFAULT_CONFIG, LeaseStore, effective_lease_config

    hub = Hub(tmp_path)
    policy = yaml.safe_load(DEFAULT_CONFIG)
    policy["defaults"]["ttl"] = "17m"
    policy["defaults"]["max_leases_per_client"] = 7
    backend = ProcessBackend(None, tmp_path)
    backend.coordination["config", "leases"] = json.dumps(effective_lease_config(policy))
    if stale_policy:
        (tmp_path / "leases.yaml").write_text("invalid: [", encoding="utf-8")
    leases = LeaseStore(hub.registry, home=tmp_path, sql_backend=backend)
    assert leases.default_ttl == 17 * 60 and leases.client_limit == 7
    if not stale_policy:
        assert yaml.safe_load((tmp_path / "leases.yaml").read_text())["defaults"]["ttl"] == 17 * 60
    assert json.loads(backend.coordination["config", "leases"]) == effective_lease_config(policy)


def test_sql_legacy_session_reconciliation_preserves_shared_active_owner_and_retires_file(tmp_path):
    from privy.hub.leases import LeaseStore

    hub = Hub(tmp_path)
    local = LeaseStore(hub.registry, home=tmp_path)
    hub.service.invoke("list_targets", {}, origin="test")
    client_id = local.register_client("old computer", session_id="same-session")["client_id"]
    lease_id = local.acquire(client_id, target="dev/box1")["lease_id"]
    local.release(lease_id, client_id)
    historical_id = uuid.uuid4().hex
    local._leases[historical_id] = dict(local._leases[lease_id], lease_id=historical_id)
    local._save()
    raw = local.state_path.read_bytes()
    shared_client = dict(local._clients[client_id], client_id="canonical", name="new computer", host="other")
    shared_lease = dict(
        local._leases[lease_id],
        client_id="canonical",
        state="active",
        running_jobs=["shared-job"],
        job_owners={"shared-job": "shared-owner"},
    )
    backend = ProcessBackend(None, tmp_path)
    backend.coordination["client", "canonical"] = json.dumps(shared_client)
    backend.coordination["lease", lease_id] = json.dumps(shared_lease)
    restored = LeaseStore(hub.registry, home=tmp_path, sql_backend=backend)
    assert restored._clients == {"canonical": shared_client}
    assert restored._leases[lease_id] == shared_lease
    assert restored._leases[historical_id]["client_id"] == "canonical"
    snapshot = local.state_path.with_name("leases-state.json.imported-snapshot")
    assert snapshot.read_bytes() == raw and not local.state_path.exists()
    before = copy.deepcopy(backend.coordination)
    local.state_path.write_bytes(raw)
    LeaseStore(hub.registry, home=tmp_path, sql_backend=backend)
    assert backend.coordination == before
    assert snapshot.read_bytes() == raw and not local.state_path.exists()


@pytest.mark.parametrize("duplicate_shared_session", [False, True])
def test_sql_legacy_active_session_alias_requires_explicit_owner_resolution(
    tmp_path, duplicate_shared_session
):
    from privy.hub.leases import LeaseStore

    hub = Hub(tmp_path)
    local = LeaseStore(hub.registry, home=tmp_path)
    hub.service.invoke("list_targets", {}, origin="test")
    client_id = local.register_client("old", session_id="same-session")["client_id"]
    local.acquire(client_id, target="dev/box1")
    raw = local.state_path.read_bytes()
    backend = ProcessBackend(None, tmp_path)
    canonical = dict(local._clients[client_id], client_id="canonical")
    backend.coordination["client", "canonical"] = json.dumps(canonical)
    if duplicate_shared_session:
        backend.coordination["client", "other"] = json.dumps(dict(canonical, client_id="other"))
    message = (
        "ambiguous duplicate client sessions"
        if duplicate_shared_session
        else "different shared session owner"
    )
    with pytest.raises(StorageError, match=message):
        LeaseStore(hub.registry, home=tmp_path, sql_backend=backend)
    assert local.state_path.read_bytes() == raw
    assert json.loads(backend.coordination["client", "canonical"]) == canonical
    assert not any(kind == "lease" for kind, _ in backend.coordination)


def test_sql_acquire_probes_relays_without_holding_coordination_transaction(tmp_path, monkeypatch):
    from privy.hub.leases import LeaseStore

    hub = Hub(tmp_path)
    backend = ProcessBackend(None, tmp_path)
    leases = LeaseStore(hub.registry, home=tmp_path, sql_backend=backend)
    client_id = leases.register_client("test", session_id="session")["client_id"]
    probe = hub.registry.probe
    observed = []

    def outside_transaction(target, *args, **kwargs):
        assert getattr(leases._lock._local, "depth", 0) == 0
        observed.append(target.id)
        return probe(target, *args, **kwargs)

    monkeypatch.setattr(hub.registry, "probe", outside_transaction)
    lease = leases.acquire(client_id, target="dev/box1")
    assert lease["state"] == "active" and "dev/box1_windows" in observed


@pytest.mark.parametrize("changed", [False, True])
def test_background_audit_import_retires_only_unchanged_durable_source(tmp_path, changed):
    hub = Hub(tmp_path)
    backend = ProcessBackend(None, tmp_path)
    jobs = manager(hub, backend)
    path = audit_fixture(
        tmp_path / "audit.jsonl",
        [{"ts": utc_iso(time.time()), "event": "job.start", "job_id": "old", "target": "dev/box1_windows"}],
    )
    raw = path.read_bytes()
    import_audit = jobs.import_audit

    def import_then_change(*args, **kwargs):
        result = import_audit(*args, **kwargs)
        if changed:
            path.write_bytes(raw + b"\n")
        return result

    jobs.import_audit = import_then_change
    try:
        jobs.start_audit_import(path)
        jobs._audit_import_thread.join(5)
        assert not jobs.audit_import_running
        assert backend.get("job", "old") is not None
        assert backend.migration_status["state"] == ("failed" if changed else "complete")
        if changed:
            assert path.read_bytes() == raw + b"\n"
        else:
            assert not path.exists()
            assert path.with_name("audit.jsonl.imported-snapshot").read_bytes() == raw
    finally:
        jobs.close()


@pytest.mark.parametrize("stage", ["config", "bind", "state"])
def test_failed_sql_start_stops_cancel_monitor_before_backend_close(tmp_path, monkeypatch, stage):
    from privy.hub.app import build_hub

    hub = Hub(tmp_path)
    (tmp_path / "appsettings.yaml").write_text(
        "version: 1\nstorage:\n  mode: azure_sql\n  server: example.database.windows.net\n  database: privy\n",
        encoding="utf-8",
    )
    instances = []

    class FailedBackend(ProcessBackend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed = False
            instances.append(self)

        def get_config(self, name):
            if stage == "config":
                raise StorageError("original startup failure")
            return super().get_config(name)

        def close(self):
            assert not any(t.name == "privy-shared-cancel" and t.is_alive() for t in threading.enumerate())
            self.closed = True

    monkeypatch.setattr("privy.hub.sql_store.SqlBackend", FailedBackend)
    if stage != "config":

        def fail(*args, **kwargs):
            raise StorageError("original startup failure")

        monkeypatch.setattr("privy.hub.app." + ("HubHTTPServer" if stage == "bind" else "write_state"), fail)
    with pytest.raises(StorageError, match="original startup failure"):
        build_hub(home=tmp_path, port=0, version="test", client_factory=hub._client)
    assert instances[0].closed


def test_sql_http_drain_sends_zero_pending_response_then_stops_serving(tmp_path, monkeypatch):
    import requests

    from privy.hub.app import build_hub

    hub = Hub(tmp_path)
    (tmp_path / "appsettings.yaml").write_text(
        "version: 1\nstorage:\n  mode: azure_sql\n  server: example.database.windows.net\n  database: privy\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("privy.hub.sql_store.SqlBackend", ProcessBackend)
    runtime = build_hub(home=tmp_path, port=0, version="test", client_factory=hub._client)
    runtime.storage.status = lambda: {"pending_writes": 0, "mode": "azure_sql"}
    serving = threading.Thread(target=runtime.serve_forever)
    serving.start()
    try:
        response = requests.post(runtime.url + "/v1/admin/drain", timeout=5)
        assert response.status_code == 200
        assert response.json() == {"status": "drained", "pending_writes": 0}
        serving.join(5)
        assert not serving.is_alive()
    finally:
        if serving.is_alive():
            runtime.server.shutdown()
        runtime.close()
    runtime.close()


@pytest.mark.parametrize(
    "owner,state,acknowledged", [("one", "error", True), ("one", "queued", False), ("old", "error", False)]
)
def test_absent_cancel_is_acknowledged_only_for_durable_current_owner_terminal_job(
    tmp_path, owner, state, acknowledged
):
    hub = Hub(tmp_path)
    backend = Backend(owner="one")
    jobs = manager(hub, backend, cancel_poll_s=10)
    backend.records["job", "absent"] = {"owner": owner, "state": state}
    backend.request_cancel("absent")
    backend.pending_cancels = lambda: ["absent"]
    try:
        jobs._process_cancels()
        assert backend.records["cancel", "absent"]["acknowledged"] is acknowledged
    finally:
        jobs.close()


@pytest.mark.parametrize("missing_dependency", ["sql_modules", "odbc_driver"])
def test_sql_process_harness_starts_and_serves_with_fake_backend(missing_dependency):
    import requests

    root = pathlib.Path(__file__).resolve().parents[1]
    home = root / f".test-sql-process-{uuid.uuid4().hex}"
    home.mkdir()
    settings = home / "appsettings.yaml"
    settings.write_text(
        "version: 1\nstorage: {mode: azure_sql, server: simulated.invalid, database: fake_harness_test}\n",
        encoding="utf-8",
    )
    unavailable = (
        "sys.modules['pyodbc']=None; sys.modules['azure.identity']=None; "
        if missing_dependency == "sql_modules"
        else "import types; sys.modules['pyodbc']=types.SimpleNamespace(drivers=lambda: []); "
    )
    script = (
        "import runpy,sys; from pathlib import Path; "
        "sys.path[:0]=[str(Path.cwd()/'tests'),str(Path.cwd()/'src')]; "
        + unavailable
        + "from test_shared_jobs import ProcessBackend,ProcessConnections; import privy.hub.sql_store as sql; "
        "sql.SqlBackend=ProcessBackend; "
        "harness=runpy.run_path(str(Path.cwd()/'tests'/'sql_hub_process.py')); "
        "harness['main'](connections_factory=ProcessConnections)"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, "--home", str(home), "--settings", str(settings), "--port", "0"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = queue.Queue()

    def collect():
        for line in process.stdout:
            lines.put(line)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 30
        output = []
        ready = None
        while ready is None and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            output.append(line)
            try:
                data = json.loads(line)
                if "url" in data and "owner" in data:
                    ready = data
            except ValueError:
                continue
        assert ready is not None, "".join(output)
        assert not ready["url"].endswith(":7748")
        response = requests.get(ready["url"] + "/healthz", timeout=5)
        assert response.status_code == 200
        response = requests.get(ready["url"] + "/v1", timeout=5)
        assert response.status_code == 200
        assert response.json()["status"]["storage"]["mode"] == "azure_sql"
        response = requests.post(
            ready["url"] + "/v1/jobs",
            json={
                "target": "sqltest/testbox00_windows",
                "language": "python",
                "code": "process verification",
                "wait_s": 5,
                "purpose": "SQL harness verification",
            },
            timeout=10,
        )
        assert response.status_code == 200, response.text
        job = response.json()
        assert job["state"] == SUCCEEDED, job.get("error")
        assert job["stdout"] == "simulated: process verification\n"
        assert requests.get(ready["url"] + f"/v1/jobs/{job['job_id']}", timeout=5).status_code == 200
        response = requests.get(ready["url"] + "/v1/stats", timeout=5)
        assert response.status_code == 200 and response.json()["jobs"]["total"] == 1
    finally:
        process.terminate()
        process.wait(timeout=10)
        reader.join(timeout=5)
        process.stdout.close()
        shutil.rmtree(home)


def test_shared_metrics_page_reads_are_bounded_and_filterable():
    backend = Backend()
    metrics = SharedMetrics(backend)

    assert metrics.entries("target-a", lease_id="lease-a", limit=3, states=frozenset({"failed"})) == []
    assert backend.list_calls == [
        {
            "kind": "job",
            "limit": 3,
            "target": "target-a",
            "lease_id": "lease-a",
            "states": frozenset({"failed"}),
        }
    ]

    backend.put("job", "one", {"target": "target-a", "lease_id": "lease-a", "state": "failed"})
    backend.put("job", "two", {"target": "target-a", "lease_id": "lease-b", "state": "succeeded"})
    backend.put("job", "three", {"target": "target-b", "lease_id": "lease-a", "state": "failed"})
    assert metrics.count_jobs_by_target(lease_id="lease-a") == {"target-a": 1, "target-b": 1}
    assert metrics.count_job_records(lease_id="lease-a", states=frozenset({"failed"})) == 2
