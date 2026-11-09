"""Opt-in real Azure SQL tests. Never use a production database or live hub."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from hub_fakes import Hub, relays_document, write_relays

from privy.client import ExecResult
from privy.hub import sql_store
from privy.hub.jobs import RUNNING
from privy.hub.leases import LeaseError, LeaseStore
from privy.hub.service import HubService
from privy.hub.settings import load_settings
from privy.hub.shared_jobs import SharedJob, SharedJobManager
from privy.hub.sql_store import AzureSqlConnections, SqlBackend, StorageError

pytestmark = pytest.mark.skipif(
    not os.environ.get("PRIVY_SQL_TEST_SETTINGS"), reason="requires isolated Azure SQL test settings"
)


class TestConnections:
    """Bounded retries for this test environment's rotating authorized egress."""

    __test__ = False

    def __init__(self, settings):
        self.pool = AzureSqlConnections(settings)

    def __call__(self):
        for attempt in range(6):
            try:
                return self.pool()
            except StorageError as exc:
                if attempt == 5 or "40615" not in str(exc.__cause__):
                    raise
                time.sleep(0.5)
        raise AssertionError("bounded connection attempts exhausted")

    def close(self):
        self.pool.close()


@pytest.fixture(scope="module")
def stores(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("isolated-sql")
    settings = load_settings(tmp_path, Path(os.environ["PRIVY_SQL_TEST_SETTINGS"]))
    assert settings is not None and settings.database.endswith("_test"), "production database is forbidden"
    hub = Hub(tmp_path)
    document = relays_document()
    document["relays"] = [
        {
            "name": "sqltest",
            "namespace": "simulated.invalid",
            "keyrule": "test-only",
            "key": "not-a-real-credential",
            "hybrid_connections": [f"testbox{i:02d}_windows" for i in range(20)],
        }
    ]
    write_relays(hub.relays_file, document)
    # Hub's fake client factory returns a listener for unknown fixture targets.
    hub.service.invoke("list_targets", {}, origin="test")
    one = SqlBackend(settings, tmp_path, connect=TestConnections(settings), start_worker=False)
    other_home = tmp_path / "other"
    other_home.mkdir()
    two = SqlBackend(settings, other_home, connect=TestConnections(settings), start_worker=False)
    try:
        yield (
            LeaseStore(hub.registry, home=tmp_path, sql_backend=one),
            LeaseStore(hub.registry, home=other_home, sql_backend=two),
            one,
            two,
        )
    finally:
        one.close()
        two.close()


def test_real_sql_racing_admission_renew_release_and_session_idempotency(stores):
    a, b, _, _ = stores
    session = uuid.uuid4().hex
    registered = a.register_client("race-owner", session_id=session)
    assert b.register_client("same-session", session_id=session)["client_id"] == registered["client_id"]
    other = b.register_client("race-other")["client_id"]
    barrier = threading.Barrier(2)

    def allocate(store, client):
        barrier.wait()
        try:
            return store.acquire(client, target="sqltest/testbox00")
        except LeaseError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda args: allocate(*args), [(a, registered["client_id"]), (b, other)]))
    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1 and "group_leased" in results
    winner = winners[0]
    assert b.renew(winner["lease_id"], winner["client_id"])["state"] == "active"
    a.release(winner["lease_id"], winner["client_id"])
    replacement = b.acquire(other, target="sqltest/testbox00")
    with pytest.raises(LeaseError) as exc:
        a.renew(winner["lease_id"], winner["client_id"])
    assert exc.value.code == "lease_released"
    b.release(replacement["lease_id"], other)


def test_real_sql_pins_require_executing_owner_and_survive_new_incarnation(stores):
    a, b, one, two = stores
    client = a.register_client("pin-test")["client_id"]
    lease = a.acquire(client, target="sqltest/testbox01")
    job = SimpleNamespace(
        id=uuid.uuid4().hex,
        target=a.registry.resolve("sqltest/testbox01_windows"),
        lease_id=lease["lease_id"],
        origin="test",
        purpose=None,
        state="running",
    )
    a.job_started(job)
    try:
        assert b.get_lease(lease["lease_id"])["running_jobs"] == [job.id]
        with pytest.raises(LeaseError, match="pinned"):
            b.release(lease["lease_id"], force=True)
        with pytest.raises(LeaseError, match="executing hub"):
            b.job_finished(job)
        assert a.get_lease(lease["lease_id"])["running_jobs"] == [job.id]
        # Stale heartbeat cannot unpin remote work.
        with one.transaction() as cursor:
            cursor.execute(
                "UPDATE dbo.privy_coordination SET updated=DATEADD(day,-8,SYSUTCDATETIME()) WHERE kind='hub' AND id=?",
                one.owner,
            )
        # Even terminal-looking old metadata cannot defeat an unresolved work pin.
        old = time.time() - 8 * 86400
        one.put("job", job.id, {"job_id": job.id, "state": "error", "owner": one.owner, "created_at": old})
        one.put("audit", job.id + ":audit", {"job_id": job.id, "created_at": old})
        one.put("broadcast", job.id + ":broadcast", {"job_ids": [job.id], "created_at": old})
        two.collect()
        assert b.get_lease(lease["lease_id"])["running_jobs"] == [job.id]
        assert two.get("job", job.id) is not None
        assert two.get("audit", job.id + ":audit") is not None
        assert two.get("broadcast", job.id + ":broadcast") is not None
    finally:
        job.state = "succeeded"
        a.job_finished(job)
        a.release(lease["lease_id"], client)
        one.delete("job", job.id)
        one.delete("audit", job.id + ":audit")
        one.delete("broadcast", job.id + ":broadcast")


def test_real_sql_outage_replays_without_independent_file_fallback(stores):
    a, b, one, two = stores
    saved = one._connect

    def unavailable():
        raise StorageError("test-owned connection outage")

    one._connect = unavailable
    key = uuid.uuid4().hex
    one.enqueue("audit", key, {"event": "outage-test", "created_at": 1_800_000_000})
    assert one.flush() is False
    with pytest.raises(StorageError):
        a.register_client("must-not-register-offline")
    assert one.outbox.status()["pending_writes"] == 1
    one._connect = saved
    assert one.flush(timeout=30)
    assert two.get("audit", key)["event"] == "outage-test"
    two.delete("audit", key)


def test_real_sql_committed_unacked_batch_replay_is_idempotent(stores, monkeypatch):
    _, _, one, two = stores
    key = uuid.uuid4().hex
    payload = {"job_id": key, "owner": one.owner, "state": "running", "created_at": time.time()}
    one.enqueue("job", key, payload)
    one.enqueue(
        "output",
        key + ":0",
        {
            "job_id": key,
            "index": 0,
            "stream": "stdout",
            "start": 0,
            "end": 5,
            "data": base64.b64encode(b"hello").decode(),
        },
    )
    one.enqueue("job", key, {**payload, "state": "succeeded"})
    ack = one.outbox.ack_batch

    def crash_before_ack(sequences):
        raise StorageError("test crash after SQL commit before local acknowledgment")

    monkeypatch.setattr(one.outbox, "ack_batch", crash_before_ack)
    with pytest.raises(StorageError):
        one._drain()
    assert two.get("job", key)["state"] == "succeeded"
    assert one.outbox.status()["pending_writes"] == 3
    monkeypatch.setattr(one.outbox, "ack_batch", ack)
    assert one.flush(timeout=30)
    assert len(two.output(key, "stdout", 0, 100)) == 1
    assert two.get("job", key)["state"] == "succeeded"
    with one.transaction() as cursor:
        cursor.execute("DELETE dbo.privy_output WHERE job_id=?", key)
    one.delete("job", key)


def test_real_sql_cancel_acknowledgment_and_cursors_are_owner_coordinated(stores):
    _, _, one, two = stores
    key = uuid.uuid4().hex
    one.put("job", key, {"job_id": key, "owner": one.owner, "state": "running", "created_at": time.time()})
    assert two.request_cancel(key)["acknowledged"] is False
    assert key in one.pending_cancels() and key not in two.pending_cancels()
    two.ack_cancel(key)
    assert two.request_cancel(key)["acknowledged"] is False
    one.ack_cancel(key)
    assert two.request_cancel(key)["acknowledged"] is True
    assert one.advance_cursor(key, "stdout", 10) == 0
    assert two.advance_cursor(key, "stdout", 5) == 10
    assert two.advance_cursor(key, "stdout", 20) == 10
    assert one.advance_cursor(key, "stdout", 0) == 20
    with one.transaction() as cursor:
        cursor.execute("DELETE dbo.privy_cancels WHERE job_id=?", key)
        cursor.execute("DELETE dbo.privy_cursors WHERE job_id=?", key)
    one.delete("job", key)


def test_real_sql_probe_history_uses_covering_projection(stores):
    _, _, one, two = stores
    key = f"{uuid.uuid4().hex}:probe"
    at = time.time()
    target = "sqltest/testbox02"
    one.put(
        "probe",
        key,
        {"target": target, "created_at": at, "status": "online", "latency_ms": 123},
    )
    try:
        with two.transaction() as cursor:
            cursor.execute(
                "SELECT probe_status,probe_latency_ms FROM dbo.privy_records WHERE kind='probe' AND id=?",
                key,
            )
            assert cursor.fetchone() == ("online", 123)
        rows = [row for row in two.probe_history(after=at - 1, before=at + 1) if row["target"] == target]
        assert len(rows) == 1
        assert rows[0]["status"] == "online"
        assert rows[0]["latency_ms"] == 123
    finally:
        one.delete("probe", key)


def test_real_sql_summary_scan_has_no_gaps_at_equal_timestamp_boundaries(stores):
    _, _, one, two = stores
    key = uuid.uuid4().hex
    at = time.time()
    for index in range(1103):
        one.enqueue(
            "audit", f"{key}:{index:04d}", {"id": f"{key}:{index:04d}", "created_at": at, "target": key}
        )
    try:
        assert one.flush(timeout=60)
        rows = list(two.scan("audit", target=key, after=at, before=at + 1))
        assert [row["id"] for row in rows] == [f"{key}:{index:04d}" for index in reversed(range(1103))]
        assert list(two.scan("audit", target=key, before=at)) == []
    finally:
        for _ in range(4):
            with one.transaction() as cursor:
                cursor.execute("DELETE TOP(300) dbo.privy_records WHERE kind='audit' AND target=?", key)


def test_real_sql_reconcile_requires_inactive_owner_and_blocks_delayed_job_replay(stores, monkeypatch):
    a, b, one, two = stores
    target = a.registry.resolve("sqltest/testbox00_windows")
    client_id = a.register_client("reconcile-test")["client_id"]
    lease = a.acquire(client_id, target="sqltest/testbox00")
    job_id = uuid.uuid4().hex
    remote_job_id = uuid.uuid4().hex
    job = SimpleNamespace(
        id=job_id,
        target=target,
        lease_id=lease["lease_id"],
        origin="test",
        purpose="isolated SQL reconciliation test",
        state=RUNNING,
    )
    record = {
        "job_id": job_id,
        "owner": one.owner,
        "target": target.id,
        "type": "exec",
        "spec": {
            "language": "powershell",
            "code": "Get-Date",
            "mode": "subprocess",
            "timeout_s": 600,
        },
        "origin": "test",
        "purpose": job.purpose,
        "lease_id": lease["lease_id"],
        "client_id": client_id,
        "state": RUNNING,
        "remote_job_id": remote_job_id,
        "cancel_requested": False,
        "created_at": time.time(),
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "chunk_count": 0,
    }
    listener = a.registry.client(target).listener

    def poll(_request, requested_id, *, wait_s):
        assert wait_s == 0
        # Simulate newer shared output counters arriving while the status request is in flight.
        with two.transaction() as cursor:
            cursor.execute(
                "SELECT payload,revision FROM dbo.privy_records WHERE kind='job' AND id=?",
                job_id,
            )
            latest_payload, revision = cursor.fetchone()
            latest_payload = json.loads(latest_payload)
            latest_payload.update(stdout_bytes=41, stderr_bytes=9, chunk_count=3)
            cursor.execute(
                "UPDATE dbo.privy_records SET payload=?,revision=? WHERE kind='job' AND id=?",
                json.dumps(latest_payload),
                revision + 1,
                job_id,
            )
        return "done", ExecResult(
            exit_code=0,
            stdout="",
            stderr="",
            stdout_bytes=b"",
            stderr_bytes=b"",
            duration_ms=25,
            timed_out=False,
            error=None,
            job_id=requested_id,
        )

    listener.poll = poll
    jobs = SharedJobManager(a.registry, backend=two, cancel_poll_s=60)
    service = HubService(registry=a.registry, jobs=jobs, leases=b, version="test")
    try:
        a.job_started(job)
        one.put("job", job_id, record)
        assert one.flush(timeout=30)
        assert two.flush(timeout=30)
        assert two.request_cancel(job_id)["acknowledged"] is False
        imported = two.import_records(
            [("audit", job_id + ":ordinary-import", {"created_at": time.time(), "job_id": job_id})]
        )
        assert imported == {("audit", job_id + ":ordinary-import")}
        assert two.get("audit", job_id + ":ordinary-import")["job_id"] == job_id
        one.mark_stopped()
        assert two.owner_is_inactive(one.owner)
        result = service.reconcile_job(
            job_id=job_id,
            remote_job_id=remote_job_id,
            lease_id=lease["lease_id"],
            client_id=client_id,
            target=target.id,
        )
        assert result["job"]["state"] == "succeeded"
        assert result["job"]["reconciliation"]["remote_job_id"] == remote_job_id
        assert b.get_lease(lease["lease_id"])["running_jobs"] == []
        assert two.request_cancel(job_id)["acknowledged"] is True

        # A delayed pre-recovery journal record cannot replace the verified terminal result.
        candidate = two.reconciliation_candidate(job_id)
        superseded = []
        for _ in range(candidate["revision"] + 2):
            replay_revision = two.outbox.append("job", job_id, record, one.owner)
            if replay_revision > candidate["revision"]:
                break
            superseded.append(replay_revision)
        two.outbox.ack_batch(superseded)
        assert replay_revision > candidate["revision"]
        selected = two.outbox.pending(limit=500)
        assert selected[0][0] == replay_revision
        assert all(row[2] == job_id and row[0] > candidate["revision"] for row in selected)
        legacy_flush = (Path(__file__).parent / "fixtures" / "legacy_sql_flush_ef946.sql").read_text()
        with monkeypatch.context() as legacy:
            legacy.setattr(sql_store, "_FLUSH_BATCH", legacy_flush)
            with pytest.raises(StorageError) as old_update:
                two._drain()
        assert "51002" in str(old_update.value.__cause__)
        assert two.outbox.status()["pending_writes"] > 0
        assert two.flush(timeout=30)
        persisted = two.get("job", job_id)
        assert persisted["state"] == "succeeded"
        assert persisted["reconciled_at"] == result["job"]["reconciled_at"]
        assert (persisted["stdout_bytes"], persisted["stderr_bytes"], persisted["chunk_count"]) == (41, 9, 3)
        assert b.get_lease(lease["lease_id"])["running_jobs"] == []

        late = {
            "job_id": job_id,
            "target": job_id,
            "index": 3,
            "stream": "stdout",
            "start": 41,
            "end": 45,
            "data": "bGF0ZQ==",
        }
        two.outbox.append("output", job_id + ":3", late, one.owner)
        two.outbox.append("job", job_id, record, one.owner)
        assert two.flush(timeout=30)
        public = SharedJob(two, two.get("job", job_id))
        assert public.read_output("stdout", 41, 100)["data"] == "late"
        assert public.view(max_output_bytes=100, offsets={"stdout": 41, "stderr": 9})["stdout"] == "late"
        pieces, index, complete = public.next_pieces(3, 0)
        assert [(piece.stream, piece.data) for piece in pieces] == [("stdout", b"late")]
        assert (index, complete) == (4, True)
        assert two.get("job", job_id) == persisted
        assert two.read_output_snapshot(job_id)["sizes"] == {"stdout": 45, "stderr": 9, "chunks": 4}
        with two.transaction() as cursor:
            cursor.execute("DELETE dbo.privy_output WHERE job_id=? AND chunk_index=3", job_id)
        assert two.output_watermarks(job_id)["stdout_bytes"] == 45
        assert two.output_watermarks(job_id)["chunk_count"] == 4

        two.delete("job", job_id)  # Exact fixture history removal, as retention would do.
        two.outbox.append("job", job_id, record, one.owner)
        with monkeypatch.context() as legacy:
            legacy.setattr(sql_store, "_FLUSH_BATCH", legacy_flush)
            with pytest.raises(StorageError) as old_insert:
                two._drain()
        assert "51002" in str(old_insert.value.__cause__)
        assert two.outbox.status()["pending_writes"] > 0
        assert two.flush(timeout=30)
        assert two.get("job", job_id) is None
    finally:
        jobs.close()
        with one.transaction() as cursor:
            cursor.execute(
                "SELECT payload FROM dbo.privy_coordination WHERE kind='hub' AND id=?",
                one.owner,
            )
            owner_payload = json.loads(cursor.fetchone()[0])
            owner_payload["active"] = True
            cursor.execute(
                "UPDATE dbo.privy_coordination SET payload=?,updated=SYSUTCDATETIME() "
                "WHERE kind='hub' AND id=?",
                json.dumps(owner_payload),
                one.owner,
            )
        if job_id in b.get_lease(lease["lease_id"])["running_jobs"]:
            # This fixture never starts remote execution; clean its own synthetic pin on failure.
            job.state = "succeeded"
            a.job_finished(job)
        b.release(lease["lease_id"], client_id)
        two.delete("job", job_id)
        two.delete("audit", job_id + ":ordinary-import")
        with two.transaction() as cursor:
            cursor.execute("DELETE dbo.privy_output WHERE job_id=?", job_id)
            cursor.execute("DELETE dbo.privy_cancels WHERE job_id=?", job_id)
            cursor.execute("DELETE dbo.privy_job_fences WHERE job_id=?", job_id)
