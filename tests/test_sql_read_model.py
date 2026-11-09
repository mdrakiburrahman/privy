import contextlib
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from privy.hub.jobs import SUCCEEDED
from privy.hub.leases import LeaseError, LeaseStore
from privy.hub.metrics import ReportSelection
from privy.hub.read_model import BackgroundReads, SqlReadModel
from privy.hub.shared_jobs import SharedJob
from privy.hub.sql_store import DurableOutbox, SqlBackend, StorageError


class Backend:
    owner = "hub-a"

    def __init__(self):
        self.records = {}
        self.pending = set()
        self.outbox = SimpleNamespace(pending_keys=lambda: self.pending)
        self.chunks = []
        self.sql_reads = 0
        self.cursors = {"stdout": 0, "stderr": 0}
        self.claims = 0

    def read_changes(self, version):
        self.sql_reads += 1
        return version, [(kind, key, copy.deepcopy(row)) for (kind, key), row in self.records.items()], True

    def read_record_keys(self):
        self.sql_reads += 1
        return set(self.records)

    def get(self, kind, key):
        self.sql_reads += 1
        return copy.deepcopy(self.records.get((kind, key)))

    def get_many(self, kind, keys):
        self.sql_reads += 1
        return {key: copy.deepcopy(self.records[kind, key]) for key in keys if (kind, key) in self.records}

    def read_output_snapshot(self, job_id):
        self.sql_reads += 1
        return {
            "rows": copy.deepcopy(self.chunks),
            "sizes": {
                **{
                    stream: max((row["end"] for row in self.chunks if row["stream"] == stream), default=0)
                    for stream in ("stdout", "stderr")
                },
                "chunks": max((row["index"] + 1 for row in self.chunks), default=0),
            },
        }

    def output(self, job_id, stream, offset, limit):
        self.sql_reads += 1
        return [{"stream": stream, "start": offset, "end": offset + limit, "data": "earlier"}]

    def read_cursors(self, job_id, streams):
        self.sql_reads += 1
        return {stream: self.cursors[stream] for stream in streams}

    def advance_cursors(self, job_id, ends):
        self.claims += 1
        previous = {stream: self.cursors[stream] for stream in ends}
        for stream, end in ends.items():
            self.cursors[stream] = max(end, self.cursors[stream])
        return previous


@pytest.fixture
def model():
    value = SqlReadModel(Backend(), start=False)
    # Disable periodic refresh so foreground round-trip assertions are exact.
    value.reads.interval = 3600
    yield value
    value.close()


def job(state="running", **kwargs):
    return {"job_id": "j", "created_at": 1, "target": "box", "state": state, **kwargs}


def test_warm_metadata_reads_and_counts_do_not_acquire_sql(model):
    model.backend.records = {("job", "j"): job(lease_id="lease")}
    model.refresh_once()
    reads = model.backend.sql_reads
    assert model.get("job", "j")["state"] == "running"
    assert model.get_many("job", ["j", "missing"]) == {"j": job(lease_id="lease")}
    assert model.list("job", target="BOX")[0]["job_id"] == "j"
    assert model.count_jobs() == {"running": 1, "queued": 0, "retained": 1}
    assert model.count_jobs_by_target() == {"box": 1}
    assert model.count_jobs_by_lease() == {"lease": 1}
    assert model.count_job_records(states={"running"}) == 1
    assert model.backend.sql_reads == reads
    model.get("job", "j")["state"] = "corrupted"
    assert model.get("job", "j")["state"] == "running"


def test_startup_listener_batch_does_not_fan_out_per_target(model):
    model.backend.records = {("listener", str(i)): {"info": i} for i in range(60)}
    assert len(model.get_many("listener", [str(i) for i in range(60)])) == 60
    assert model.backend.sql_reads == 1


def test_refresh_observes_remote_changes_and_old_writer_deletions(model):
    model.backend.records = {("job", "j"): job()}
    model.refresh_once()
    model.backend.records["job", "j"] = job(SUCCEEDED)
    model.refresh_once()
    assert model.get("job", "j")["state"] == SUCCEEDED
    del model.backend.records["job", "j"]
    model._reconciled = 0
    model.refresh_once()
    assert model.get("job", "j") is None
    assert model.count_jobs()["retained"] == 0


def test_pending_local_write_survives_old_snapshot_but_not_later_remote_writer(model):
    model.backend.records = {("job", "j"): job("queued")}
    model.backend.pending.add(("job", "j"))
    model.apply("job", "j", job("running"))
    assert model.get("job", "j")["state"] == "running"  # Even during startup.
    model.refresh_once()
    assert model.get("job", "j")["state"] == "running"
    model.backend.pending.clear()
    # The own-write version can be superseded before hydration ever sees it.
    model.backend.records["job", "j"] = job(SUCCEEDED)
    model.refresh_once()
    assert model.get("job", "j")["state"] == SUCCEEDED
    assert not model._pending


def test_authoritative_feed_survives_ignored_pending_write_after_watermark_advances():
    class WatermarkedBackend(Backend):
        def __init__(self):
            super().__init__()
            self.version = (1).to_bytes(8, "big")

        def read_changes(self, version):
            self.sql_reads += 1
            if version < self.version:
                return (
                    self.version,
                    [(kind, key, copy.deepcopy(row)) for (kind, key), row in self.records.items()],
                    True,
                )
            return version, [], True

    backend = WatermarkedBackend()
    backend.records["job", "j"] = job("queued")
    model = SqlReadModel(backend, start=False)
    model.reads.interval = 3600
    try:
        model.refresh_once()
        identity = ("job", "j")
        backend.pending.add(identity)
        model.apply("job", "j", job("running"))
        backend.records[identity] = job(SUCCEEDED)
        backend.version = (2).to_bytes(8, "big")
        model.refresh_once()
        assert model.get("job", "j")["state"] == "running"

        backend.pending.clear()
        model.refresh_once()
        assert model.get("job", "j")["state"] == SUCCEEDED
        assert model._version == backend.version
    finally:
        model.close()


def test_overlay_added_after_outbox_snapshot_is_not_misclassified_as_acknowledged():
    sampled_keys = threading.Event()
    release_keys = threading.Event()
    backend = Backend()
    calls = 0

    def pending_keys():
        nonlocal calls
        calls += 1
        keys = set(backend.pending)
        if calls == 2:
            sampled_keys.set()
            assert release_keys.wait(2)
        return keys

    backend.outbox = SimpleNamespace(pending_keys=pending_keys)
    model = SqlReadModel(backend, start=False)
    model.reads.interval = 3600
    try:
        model.refresh_once()
        errors = []
        refresher = threading.Thread(target=lambda: _capture_error(model.refresh_once, errors))
        refresher.start()
        assert sampled_keys.wait(1)

        identity = ("job", "j")
        backend.pending.add(identity)
        model.apply("job", "j", job("queued"))
        release_keys.set()
        refresher.join(2)

        assert not refresher.is_alive()
        assert not errors
        assert model.get("job", "j")["state"] == "queued"
        assert identity in model._pending
        assert identity not in model._records
    finally:
        release_keys.set()
        model.close()


def test_acknowledged_overlay_stays_visible_until_authoritative_reread_and_preserves_newer_overlay():
    entered_feed = threading.Event()
    release_feed = threading.Event()

    class PausedBackend(Backend):
        def __init__(self):
            super().__init__()
            self.feed_calls = 0

        def read_changes(self, version):
            self.feed_calls += 1
            if self.feed_calls == 2:
                entered_feed.set()
                assert release_feed.wait(2)
            self.sql_reads += 1
            return version, [], True

    backend = PausedBackend()
    model = SqlReadModel(backend, start=False)
    model.reads.interval = 3600
    try:
        model.refresh_once()
        identity = ("job", "j")
        model.apply("job", "j", job("queued"))
        backend.records[identity] = job(SUCCEEDED)  # A newer cross-owner write won.
        errors = []

        refresher = threading.Thread(target=lambda: _capture_error(model.refresh_once, errors))
        refresher.start()
        assert entered_feed.wait(1)

        # SQL has acknowledged the queued write, but the commit-safe change feed
        # is deliberately paused and its watermark still does not cover the row.
        assert model.get("job", "j")["state"] == "queued"
        model.apply("job", "j", job("running"))
        release_feed.set()
        refresher.join(2)
        assert not refresher.is_alive()
        assert not errors

        # The authoritative reread updates the base, but cannot retire a newer
        # overlay admitted after the refresh captured the acknowledged revision.
        assert model._records[identity]["state"] == SUCCEEDED
        assert model.get("job", "j")["state"] == "running"
        assert identity in model._pending

        model.refresh_once()
        assert model.get("job", "j")["state"] == SUCCEEDED
        assert identity not in model._pending
    finally:
        release_feed.set()
        model.close()


def _capture_error(action, errors):
    try:
        action()
    except Exception as exc:
        errors.append(exc)


def test_raw_probe_history_is_not_retained(model):
    model.apply("probe", "probe", {"data": "large"})
    model.apply("call", "call", {"data": "large"})
    assert not model._records


def test_purpose_timestamp_ties_use_sql_identity_order_even_without_payload_id(model):
    model.backend.records = {
        ("purpose", "a"): {"created_at": 1, "purpose": "first"},
        ("purpose", "z"): {"created_at": 1, "purpose": "last"},
    }
    model.refresh_once()
    assert [row["purpose"] for row in model.list("purpose")] == ["last", "first"]
    assert [row["purpose"] for row in model.scan("purpose")] == ["last", "first"]


def test_stale_metadata_remains_available_and_exposes_health(model):
    model.backend.records["job", "j"] = job()
    model.refresh_once()
    model._updated = time.monotonic() - 60
    model._error = RuntimeError("refresh failed")
    assert model.get("job", "j")["state"] == "running"
    status = model.health()
    assert status["ready"]
    assert status["age_seconds"] >= 60
    assert status["last_refresh_seconds"] >= 0
    assert status["last_error"] == "refresh failed"


def test_progressive_startup_serves_exact_record_before_full_hydration():
    entered, release = threading.Event(), threading.Event()
    backend = Backend()
    backend.records["job", "j"] = job()
    original = backend.read_changes

    def slow_hydration(version):
        entered.set()
        assert release.wait(2)
        return original(version)

    backend.read_changes = slow_hydration
    model = SqlReadModel(backend)
    try:
        assert entered.wait(1)
        assert model.get("job", "j")["state"] == "running"
        assert not model._ready.is_set()
    finally:
        release.set()
        model.close()


def test_periodic_refresh_converges_within_five_seconds():
    backend = Backend()
    backend.records["job", "j"] = job()
    model = SqlReadModel(backend)
    try:
        assert model._ready.wait(2)
        backend.records["job", "j"] = job(SUCCEEDED)
        deadline = time.monotonic() + 5
        while model.get("job", "j")["state"] != SUCCEEDED and time.monotonic() < deadline:
            time.sleep(0.05)
        assert model.get("job", "j")["state"] == SUCCEEDED
        assert time.monotonic() < deadline
    finally:
        model.close()


def test_warm_explicit_output_is_sql_free_but_cursor_claims_stay_authoritative(model):
    model.backend.chunks = [{"index": 0, "stream": "stdout", "start": 0, "end": 4, "data": "YWJjZA=="}]
    ranges = {"stdout": (0, 4, 4), "stderr": (0, 0, 4)}
    offsets, chunks = model.read_view_output("j", ranges)
    reads = model.backend.sql_reads
    assert offsets == {"stdout": 0, "stderr": 0}
    assert chunks["stdout"][0]["end"] == 4
    model.read_view_output("j", ranges)
    assert model.backend.sql_reads == reads
    model.read_view_output("j", {"stdout": (None, 4, 4)})
    assert model.backend.sql_reads == reads + 1
    assert model.advance_cursors("j", {"stdout": 4}) == {"stdout": 0}
    assert model.advance_cursors("j", {"stdout": 2}) == {"stdout": 4}
    assert model.backend.cursors["stdout"] == 4


def test_truncated_output_tail_fetches_earlier_range_instead_of_inventing_gap(model):
    model.backend.chunks = [{"index": 10, "stream": "stdout", "start": 100, "end": 104, "data": "YWJjZA=="}]
    assert model.output("j", "stdout", 0, 4)[0]["data"] == "earlier"
    reads = model.backend.sql_reads
    assert model.output("j", "stdout", 100, 4)[0]["end"] == 104
    assert model.backend.sql_reads == reads


def test_advanced_metadata_refreshes_older_output_snapshot_instead_of_returning_incomplete_data(model):
    assert model._output_snapshot("j")["sizes"]["stdout"] == 0
    model.backend.chunks = [{"index": 0, "stream": "stdout", "start": 0, "end": 4, "data": "YWJjZA=="}]
    assert model.output("j", "stdout", 0, 4)[0]["end"] == 4
    assert model.backend.sql_reads == 2


def test_genuine_missing_chunk_is_not_mistaken_for_cache_lag(model):
    model.backend.read_output_snapshot = lambda job_id: {
        "rows": [],
        "sizes": {"stdout": 4, "stderr": 0, "chunks": 1},
    }
    assert model.output("j", "stdout", 0, 4) == []


def test_between_read_delivery_refreshes_snapshot_before_public_terminal_view(model):
    delivered = False
    chunk = {"index": 0, "stream": "stdout", "start": 0, "end": 4, "data": "bGF0ZQ=="}
    backend = object.__new__(SqlBackend)

    def watermarks(job_id):
        return {"stdout_bytes": 4 if delivered else 0, "stderr_bytes": 0, "chunk_count": int(delivered)}

    @contextlib.contextmanager
    def transaction():
        sets = iter([True, False])
        captured = [chunk] if delivered else []

        def fetchall():
            nonlocal delivered
            delivered = True  # Delivery commits after rows were selected, before a later watermark read.
            return [(json.dumps(row),) for row in captured]

        yield SimpleNamespace(
            execute=lambda *args: None,
            fetchone=lambda: (0, 0, 0, "reconciled"),
            fetchall=fetchall,
            nextset=lambda: next(sets),
        )

    backend.transaction = transaction
    backend.output_watermarks = watermarks
    model.backend.read_output_snapshot = backend.read_output_snapshot
    model.backend.output_watermarks = watermarks
    old = model._output_snapshot("j")
    assert old["rows"] == []
    assert old["sizes"] == {"stdout": 0, "stderr": 0, "chunks": 0}
    projection = SharedJob(
        model,
        job(
            SUCCEEDED,
            type="exec",
            origin="rest",
            stdout_bytes=0,
            stderr_bytes=0,
            chunk_count=0,
            reconciled_at="reconciled",
            spec={"language": "bash", "code": "test", "mode": "subprocess", "timeout_s": 30},
        ),
        snapshot=True,
    )
    result = projection.view(max_output_bytes=100, offsets={"stdout": 0, "stderr": 0})
    assert result["done"] is True
    assert result["stdout"] == "late"
    assert result["stdout_offset"] == 4
    assert projection.read_output("stdout", 0, 100)["data"] == "late"


def test_streaming_projection_carries_chunk_count_during_progressive_startup(model):
    assert model._output_snapshot("j")["sizes"]["chunks"] == 0
    model.backend.chunks = [{"index": 0, "stream": "stdout", "start": 0, "end": 4, "data": "YWJjZA=="}]
    projection = SharedJob(
        model,
        job(SUCCEEDED, type="exec", spec={}, origin="rest", stdout_bytes=4, stderr_bytes=0, chunk_count=1),
        snapshot=True,
    )
    pieces, index, complete = projection.next_pieces(0, 0)
    assert len(pieces) == 1 and pieces[0].stream == "stdout"
    assert index == 1 and complete


def test_cold_reads_coalesce_parallel_requests():
    entered, release = threading.Event(), threading.Event()
    count = 0

    def load():
        nonlocal count
        count += 1
        entered.set()
        assert release.wait(2)
        return {"answer": 42}

    cache = BackgroundReads(interval=3600)
    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(cache.get, ("report",), load) for _ in range(4)]
            assert entered.wait(1)
            release.set()
            assert all(f.result()["answer"] == 42 for f in futures)
        assert count == 1
    finally:
        cache.close()


def test_cold_read_times_out_without_cancelling_inflight_hydration():
    entered, release = threading.Event(), threading.Event()
    cache = BackgroundReads(interval=3600, cold_timeout=0.01)

    def load():
        entered.set()
        assert release.wait(2)
        return "ready"

    try:
        with pytest.raises(StorageError, match="still hydrating"):
            cache.get(("cold",), load)
        assert entered.is_set()
        release.set()
        cache._reads[("cold",)].future.result(timeout=2)
        assert cache.get(("cold",), load) == "ready"
    finally:
        release.set()
        cache.close()


def test_local_commit_publication_cannot_be_overwritten_by_older_inflight_read():
    entered, release = threading.Event(), threading.Event()
    cache = BackgroundReads(interval=3600)

    def old_load():
        entered.set()
        assert release.wait(2)
        return {"lease": "old"}

    try:
        cache.get(("coordination",), old_load, wait=False)
        assert entered.wait(1)
        cache.publish(("coordination",), {"lease": "new"}, lambda: {"lease": "new"})
        release.set()
        cache._reads[("coordination",)].future.result()
        assert cache.get(("coordination",), old_load) == {"lease": "new"}
    finally:
        release.set()
        cache.close()


def test_slow_or_failed_refresh_serves_last_good_value(caplog):
    now = [0]
    cache = BackgroundReads(interval=1, clock=lambda: now[0])
    entered, release = threading.Event(), threading.Event()

    def fail():
        entered.set()
        assert release.wait(2)
        raise RuntimeError("database unavailable")

    try:
        assert cache.get(("report",), lambda: {"ok": True}) == {"ok": True}
        now[0] = 5
        assert cache.get(("report",), fail) == {"ok": True}
        assert entered.wait(1)
        assert cache.get(("report",), fail) == {"ok": True}
        release.set()
        with pytest.raises(RuntimeError, match="database unavailable"):
            cache._reads[("report",)].future.result(timeout=2)
        assert cache.get(("report",), fail) == {"ok": True}
        assert "Background SQL read refresh failed" in caplog.text
    finally:
        release.set()
        cache.close()


def test_idle_reads_do_not_refresh_and_write_bursts_coalesce():
    now = [0.0]
    cache = BackgroundReads(interval=0.01, max_age=5, idle_after=60, clock=lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(now[0])
        if len(calls) == 2:
            entered.set()
            assert release.wait(2)
        return len(calls)

    try:
        assert cache.get(("report",), load) == 1
        now[0] = 61
        time.sleep(0.04)
        cache.invalidate(lambda key: True)
        time.sleep(0.04)
        assert calls == [0]
        assert cache.get(("report",), load) == 1
        assert entered.wait(1)
        for _ in range(20):
            cache.invalidate(lambda key: True)
        assert calls == [0, 61]
        release.set()
        cache._reads[("report",)].future.result(timeout=2)
        assert cache.get(("report",), load) == 2
        assert calls == [0, 61]
    finally:
        release.set()
        cache.close()


def test_default_report_stays_warm_after_idle():
    now = [0.0]
    cache = BackgroundReads(interval=0.01, max_age=5, idle_after=60, clock=lambda: now[0])
    refreshed = threading.Event()
    calls = []

    def load():
        calls.append(now[0])
        if len(calls) > 1:
            refreshed.set()
        return len(calls)

    try:
        key = ("report", ReportSelection(), ())
        assert cache.get(key, load) == 1
        now[0] = 61
        assert refreshed.wait(1)
        assert cache.get(key, load) == 2
    finally:
        cache.close()


def test_standard_ranges_stay_warm_but_custom_reports_do_not():
    cache = BackgroundReads(interval=3600, idle_after=60)
    try:
        standard = ("report", ReportSelection("7d"), (("max_running", 20),))
        custom = ("report", ReportSelection("custom", start=1, end=2), (("max_running", 20),))
        cache.get(standard, lambda: "standard")
        cache.get(custom, lambda: "custom")
        now = cache.clock() + 120
        assert cache._active(cache._reads[standard], now)
        assert not cache._active(cache._reads[custom], now)
        assert cache._core_read(("coordination",))
        assert cache._core_read(("latest-probes",))
    finally:
        cache.close()


def test_default_report_survives_cache_eviction():
    cache = BackgroundReads(capacity=1, interval=3600)
    key = ("report", ReportSelection(), ())
    try:
        assert cache.get(key, lambda: "default") == "default"
        assert cache.get(("other",), lambda: "other") == "other"
        assert key in cache._reads
        assert cache.get(key, lambda: "unexpected") == "default"
    finally:
        cache.close()


def test_unchanged_refresh_defers_expensive_key_reconciliation(model):
    model.backend.records["job", "j"] = job()
    model.refresh_once()
    reads = model.backend.sql_reads
    model.refresh_once()
    assert model.backend.sql_reads == reads + 1
    assert model.get("job", "j") == job()


def test_refresh_preserves_last_good_snapshot_until_full_feed_commits(model):
    model.backend.records["job", "j"] = job("queued")
    model.refresh_once()
    entered, release = threading.Event(), threading.Event()

    def feed(version):
        if version == bytes(8):
            return (1).to_bytes(8, "big"), [("job", "j", job("running"))], False
        entered.set()
        assert release.wait(2)
        raise StorageError("feed interrupted")

    model.backend.read_changes = feed
    errors = []
    thread = threading.Thread(target=lambda: _capture_error(model.refresh_once, errors))
    thread.start()
    try:
        assert entered.wait(1)
        assert model.get("job", "j")["state"] == "queued"
        release.set()
        thread.join(2)
        assert len(errors) == 1
        assert model.get("job", "j")["state"] == "queued"
        assert model._version == bytes(8)
    finally:
        release.set()
        thread.join(2)


def test_unchanged_refresh_keeps_job_snapshot_and_local_write_invalidates_it(model):
    model.backend.records["job", "j"] = job("queued")
    model.refresh_once()
    assert model.list("job")[0]["state"] == "queued"
    snapshot = model._snapshots["job"]
    model.refresh_once()
    assert model._snapshots["job"] is snapshot
    model.backend.pending.add(("job", "j"))
    model.apply("job", "j", job("running"))
    assert model.list("job")[0]["state"] == "running"
    assert model._snapshots["job"] is not snapshot


def test_background_reads_bounds_running_and_queued_work():
    cache = BackgroundReads(workers=1, capacity=3, queue_capacity=1, interval=3600)
    started = threading.Event()
    release = threading.Event()

    def blocked():
        started.set()
        assert release.wait(2)
        return "first"

    try:
        cache.get(("first",), blocked, wait=False)
        assert started.wait(1)
        cache.get(("second",), lambda: "second", wait=False)
        with pytest.raises(StorageError, match="saturated"):
            cache.get(("third",), lambda: "third", wait=False)
        release.set()
        assert cache.get(("first",), blocked) == "first"
        assert cache.get(("second",), lambda: "second") == "second"
    finally:
        release.set()
        cache.close()


def test_background_reads_deduplicate_inflight_load_after_cache_eviction():
    cache = BackgroundReads(workers=1, capacity=1, queue_capacity=1, interval=3600)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def first():
        calls.append("first")
        started.set()
        assert release.wait(2)
        return "first"

    try:
        cache.get(("first",), first, wait=False)
        assert started.wait(1)
        cache.get(("second",), lambda: "second", wait=False)
        assert ("first",) not in cache._reads
        cache.get(("first",), first, wait=False)
        release.set()
        assert cache.get(("first",), first) == "first"
        assert calls == ["first"]
    finally:
        release.set()
        cache.close()


def test_background_reads_cancel_discarded_queued_load_and_reuse_capacity():
    cache = BackgroundReads(workers=1, capacity=3, queue_capacity=1, interval=3600)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def blocked():
        started.set()
        assert release.wait(2)
        return "running"

    def replacement():
        calls.append("replacement")
        return "replacement"

    try:
        cache.get(("running",), blocked, wait=False)
        assert started.wait(1)
        cache.get(("discarded",), lambda: calls.append("discarded"), wait=False)
        queued = cache._inflight[("discarded",)].future
        cache.discard(lambda key: key == ("discarded",))
        assert queued.cancelled()
        cache.get(("replacement",), replacement, wait=False)
        release.set()
        assert cache.get(("replacement",), replacement) == "replacement"
        assert calls == ["replacement"]
    finally:
        release.set()
        cache.close()


def test_read_model_close_reports_accounting_failure_after_stopping_workers():
    model = SqlReadModel(Backend(), start=False)

    def fail():
        raise RuntimeError("lease accounting failed")

    model._uses[("test",)] = fail
    with pytest.raises(StorageError, match="accounting or read shutdown failed"):
        model.close()
    assert model.closed
    assert not model._accounting_thread.is_alive()
    assert model.reads._closed


def test_backend_close_cleans_all_resources_after_read_model_error():
    calls = []
    backend = object.__new__(SqlBackend)
    backend._closed = False
    backend._stop = threading.Event()
    backend._wake = threading.Event()
    backend._thread = None
    backend.read_model = SimpleNamespace(
        closed=True,
        close=lambda: (_ for _ in ()).throw(RuntimeError("deferred use failed")),
    )
    backend.flush = lambda timeout=10: False
    backend.mark_stopped = lambda: calls.append("stopped")
    backend.outbox = SimpleNamespace(close=lambda: calls.append("outbox"))
    backend._connect = SimpleNamespace(close=lambda: calls.append("pool"))
    backend._home_lock = SimpleNamespace(close=lambda: calls.append("home lock"))

    with pytest.raises(StorageError, match="read model: RuntimeError.*journal flush: StorageError"):
        backend.close()
    assert calls == ["stopped", "outbox", "pool", "home lock"]
    assert backend._closed


def test_backend_close_keeps_resources_open_while_delivery_worker_is_alive():
    calls = []
    backend = object.__new__(SqlBackend)
    backend._closed = False
    backend._stop = threading.Event()
    backend._wake = threading.Event()
    backend._thread = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: True)
    backend.read_model = SimpleNamespace(close=lambda: calls.append("read model"))
    backend.outbox = SimpleNamespace(close=lambda: calls.append("outbox"))
    backend._connect = SimpleNamespace(close=lambda: calls.append("pool"))
    backend._home_lock = SimpleNamespace(close=lambda: calls.append("home lock"))

    with pytest.raises(StorageError, match="resources remain in use"):
        backend.close()
    assert calls == []
    assert not backend._closed


def test_deferred_advisory_accounting_runs_once_off_thread_not_forever(model):
    delivered = threading.Event()
    calls = []
    foreground_thread = threading.get_ident()

    def use(target, lease_id, *, origin):
        assert threading.get_ident() != foreground_thread
        calls.append((target, lease_id, origin))
        delivered.set()

    model.defer_use(SimpleNamespace(use=use), "box", None, "rest")
    assert delivered.wait(2)
    time.sleep(0.35)
    assert calls == [("box", None, "rest")]


def test_outbox_pending_keys_are_not_limited_to_one_flush_batch(tmp_path):
    outbox = DurableOutbox(tmp_path / "pending.sqlite", max_bytes=1024 * 1024)
    try:
        sequences = [outbox.append("job", str(i), job(), "hub-a") for i in range(501)]
        assert len(outbox.pending_keys()) == 501
        outbox.ack_batch(sequences)
        assert outbox.pending_keys() == set()
    finally:
        outbox.close()


def test_change_watermark_stops_before_uncommitted_transaction():
    cursor = SimpleNamespace()
    executes = []
    cursor.execute = lambda *args: executes.append(args)
    cursor.fetchone = lambda: ((12).to_bytes(8, "big"),)
    cursor.fetchall = lambda: [(bytes(8), "job", "j", json.dumps(job()))]
    backend = object.__new__(SqlBackend)

    @contextlib.contextmanager
    def transaction():
        yield cursor

    backend.transaction = transaction
    watermark, rows, complete = backend.read_changes(bytes(8))
    assert complete and int.from_bytes(watermark, "big") == 11
    assert rows[0][:2] == ("job", "j")
    assert executes[0] == ("SELECT MIN_ACTIVE_ROWVERSION()",)
    assert "change_version<?" in executes[1][0]
    assert executes[1][-1] == (12).to_bytes(8, "big")


def test_output_snapshot_reads_metadata_before_bounded_chunks_in_one_execute():
    executes = []
    sets = iter([True, False])
    chunk = {"index": 0, "stream": "stdout", "start": 0, "end": 4, "data": "YWJjZA=="}
    cursor = SimpleNamespace(
        execute=lambda *args: executes.append(args),
        fetchone=lambda: (4, 0, 1, None),
        fetchall=lambda: [(json.dumps(chunk),)],
        nextset=lambda: next(sets),
    )
    backend = object.__new__(SqlBackend)

    @contextlib.contextmanager
    def transaction():
        yield cursor

    backend.transaction = transaction
    backend.output_watermarks = lambda job_id: {}
    snapshot = backend.read_output_snapshot("j")
    assert snapshot == {"rows": [chunk], "sizes": {"stdout": 4, "stderr": 0, "chunks": 1}}
    assert len(executes) == 1 and executes[0][1:] == ("j", "j")
    assert executes[0][0].startswith("SELECT COALESCE")
    assert "bytes<=8388608" in executes[0][0]


def test_reconciled_output_snapshot_uses_accepted_watermarks():
    sets = iter([True, False])
    cursor = SimpleNamespace(
        execute=lambda *args: None,
        fetchone=lambda: (4, 2, 1, "reconciled"),
        fetchall=lambda: [],
        nextset=lambda: next(sets),
    )
    backend = object.__new__(SqlBackend)

    @contextlib.contextmanager
    def transaction():
        yield cursor

    backend.transaction = transaction
    backend.output_watermarks = lambda job_id: {"stdout_bytes": 9, "stderr_bytes": 0, "chunk_count": 3}
    assert backend.read_output_snapshot("j")["sizes"] == {"stdout": 9, "stderr": 2, "chunks": 3}


def advisory_store():
    calls = []
    lease = {
        "lease_id": "lease",
        "group": "group",
        "client_id": "client",
        "holder_name": "holder",
        "purpose": "work",
        "state": "active",
        "members": {"windows": "box"},
        "running_jobs": [],
        "expires_ts": 100,
        "acquired_ts": 1,
        "renewed_ts": 1,
        "labels": {},
    }
    snapshot = {"now": 10, "leases": {"lease": lease}, "clients": {}, "uses": {}}

    @contextlib.contextmanager
    def read_snapshot():
        yield copy.deepcopy(snapshot)

    store = object.__new__(LeaseStore)
    store.sql_backend = SimpleNamespace(
        read_model=SimpleNamespace(
            defer_use=lambda *args: calls.append(args), defer_touch=lambda *args: calls.append(args)
        )
    )
    store._lock = SimpleNamespace(read_snapshot=read_snapshot)
    store._membership = lambda: ({}, {"box": ("group", "windows")})
    return store, snapshot, calls


def test_read_use_keeps_lease_renewal_authoritative_and_defers_only_advisory_accounting():
    store, _, calls = advisory_store()
    store.use = lambda target, lease_id=None, *, origin="rest": (
        calls.append(("authoritative-use", target, lease_id, origin)) or {"purpose": "work"}
    )
    assert store.read_use("box", "lease") == {"purpose": "work"}
    assert calls == [("authoritative-use", "box", "lease", "rest")]
    assert store.read_use("box")["lease_warning"]["lease_id"] == "lease"
    assert calls[-1] == (store, "box", None, "rest")


def test_read_header_touch_renews_authoritatively():
    store, _, calls = advisory_store()
    store.touch = lambda lease_id: calls.append(("authoritative-touch", lease_id))
    store.read_touch("lease")
    assert calls == [("authoritative-touch", "lease")]


@pytest.mark.parametrize("state,code", [("released", "lease_released"), ("superseded", "lease_lost")])
def test_known_released_or_superseded_lease_is_rejected_by_authoritative_use(state, code):
    store, snapshot, calls = advisory_store()
    store.use = lambda target, lease_id=None, *, origin="rest": store._read_lease(snapshot, lease_id)
    if state == "released":
        snapshot["leases"]["lease"]["state"] = "released"
    else:
        snapshot["leases"]["new"] = {**snapshot["leases"]["lease"], "lease_id": "new"}
    with pytest.raises(LeaseError) as error:
        store.read_use("box", "lease")
    assert error.value.code == code
    assert not calls


def test_expired_lease_view_does_not_mutate_authoritative_snapshot():
    store, snapshot, _ = advisory_store()
    snapshot["now"] = 101
    assert store.list_leases(state="active") == []
    assert store.get_lease("lease")["state"] == "expired"
    assert snapshot["leases"]["lease"]["state"] == "active"
