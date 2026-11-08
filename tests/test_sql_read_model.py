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


def test_stale_metadata_fails_explicitly(model):
    model.refresh_once()
    model._updated = time.monotonic() - 5
    with pytest.raises(StorageError, match="stale"):
        model.get("job", "missing")


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


def test_failed_refresh_does_not_return_expired_success(caplog):
    now = [0]
    cache = BackgroundReads(interval=3600, clock=lambda: now[0])

    def fail():
        raise RuntimeError("database unavailable")

    try:
        assert cache.get(("report",), lambda: {"ok": True}) == {"ok": True}
        now[0] = 5
        with pytest.raises(StorageError, match="refresh failed"):
            cache.get(("report",), fail)
        assert "Background SQL read refresh failed" in caplog.text
    finally:
        cache.close()


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
        fetchone=lambda: (4, 0, 1),
        fetchall=lambda: [(json.dumps(chunk),)],
        nextset=lambda: next(sets),
    )
    backend = object.__new__(SqlBackend)

    @contextlib.contextmanager
    def transaction():
        yield cursor

    backend.transaction = transaction
    snapshot = backend.read_output_snapshot("j")
    assert snapshot == {"rows": [chunk], "sizes": {"stdout": 4, "stderr": 0, "chunks": 1}}
    assert len(executes) == 1 and executes[0][1:] == ("j", "j")
    assert executes[0][0].startswith("SELECT COALESCE")
    assert "bytes<=8388608" in executes[0][0]


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
