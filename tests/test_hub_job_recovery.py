import copy

import pytest
from hub_fakes import FakeListener, Hub

from privy.client import ExecResult
from privy.hub.jobs import FAILED, RUNNING, SUCCEEDED
from privy.hub.service import HubError
from privy.hub.shared_jobs import SharedJobManager
from privy.hub.sql_store import StorageError

JOB_ID = "e4d4545d803c49bb9bde53d2231001c1"
REMOTE_JOB_ID = "cd4fbfa6e8f74a6d887922c155c07f2b"
LEASE_ID = "ls-601946809b224411883fcb6e1516796b"
CLIENT_ID = "cl-f0e63535ab5a41efad6e9fc771f480c2"
TARGET = "dev/box1_windows"
OLD_OWNER = "5a2a3405344348a0969537fa503b5d0e"


class RecoveryBackend:
    owner = "new-hub-owner"

    def __init__(self, record, *, inactive=True):
        self.record = copy.deepcopy(record)
        self.inactive = inactive
        self.lease = {
            "lease_id": LEASE_ID,
            "client_id": CLIENT_ID,
            "state": "active",
            "running_jobs": [JOB_ID],
            "job_owners": {JOB_ID: OLD_OWNER},
        }
        self.cancel = {"job_id": JOB_ID, "owner": OLD_OWNER, "acknowledged": False}
        self.reconciliation_calls = []

    def reconciliation_candidate(self, job_id):
        assert job_id == JOB_ID
        return {
            "payload": copy.deepcopy(self.record),
            "owner": self.record["owner"],
            "revision": 7,
        }

    def owner_is_inactive(self, owner):
        return owner == OLD_OWNER and self.inactive

    def reconcile_job(self, **kwargs):
        self.reconciliation_calls.append(kwargs)
        assert kwargs["expected_owner"] == OLD_OWNER
        assert self.inactive
        assert self.record["state"] == RUNNING
        assert self.lease["state"] == "active"
        assert self.lease["job_owners"].get(JOB_ID) == OLD_OWNER
        self.record = copy.deepcopy(kwargs["record"])
        self.lease["running_jobs"].remove(JOB_ID)
        self.lease["job_owners"].pop(JOB_ID)
        if self.cancel["owner"] == OLD_OWNER and not self.cancel["acknowledged"]:
            self.cancel["acknowledged"] = True
        return copy.deepcopy(self.record)

    def flush(self, timeout=10):
        return True


def _record():
    return {
        "job_id": JOB_ID,
        "owner": OLD_OWNER,
        "target": TARGET,
        "type": "exec",
        "spec": {"language": "powershell", "code": "Get-Date", "mode": "subprocess", "timeout_s": 600},
        "origin": "test",
        "lease_id": LEASE_ID,
        "client_id": CLIENT_ID,
        "state": RUNNING,
        "remote_job_id": REMOTE_JOB_ID,
        "cancel_requested": False,
        "created_at": 1.0,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "chunk_count": 0,
    }


def _service(tmp_path, listener, record=None, *, inactive=True):
    hub = Hub(tmp_path, {"dev/box1_windows": listener})
    backend = RecoveryBackend(record or _record(), inactive=inactive)
    jobs = SharedJobManager(hub.registry, backend=backend, cancel_poll_s=60)
    hub.service.jobs = jobs
    hub.service.leases.sql_backend = backend
    hub.service.leases._clients[CLIENT_ID] = {
        "client_id": CLIENT_ID,
        "name": "W9 owner",
        "kind": "test",
        "registered_ts": 1.0,
        "last_seen_ts": 1.0,
    }
    hub.service.leases._leases[LEASE_ID] = {
        **backend.lease,
        "group": "dev/box1",
        "members": {"windows": TARGET},
        "ttl_s": 1800,
        "expires_ts": 9999999999,
        "renewed_ts": 1.0,
        "acquired_ts": 1.0,
        "running_jobs": [JOB_ID],
        "job_owners": {JOB_ID: OLD_OWNER},
        "events": [],
    }
    return hub, backend, jobs


def _call(service, **overrides):
    args = {
        "job_id": JOB_ID,
        "remote_job_id": REMOTE_JOB_ID,
        "lease_id": LEASE_ID,
        "client_id": CLIENT_ID,
        "target": TARGET,
    }
    args.update(overrides)
    return service.reconcile_job(**args)


def _result(*, exit_code=0, error=None, timed_out=False, job_id=REMOTE_JOB_ID):
    return ExecResult(
        exit_code=exit_code,
        stdout="",
        stderr="",
        stdout_bytes=b"",
        stderr_bytes=b"",
        duration_ms=4200,
        timed_out=timed_out,
        error=error,
        job_id=job_id,
    )


def test_reconcile_polls_exact_remote_job_and_atomically_clears_only_its_pin(tmp_path):
    poll_calls = []

    def poll(request, remote_job_id, *, wait_s):
        poll_calls.append((request, remote_job_id, wait_s))
        return "done", _result()

    listener = FakeListener(poll=poll)
    hub, backend, jobs = _service(tmp_path, listener)
    try:
        response = hub.call(
            "POST",
            f"/v1/jobs/{JOB_ID}/reconcile?remote_job_id={REMOTE_JOB_ID}"
            f"&lease_id={LEASE_ID}&client_id={CLIENT_ID}&target={TARGET}",
        )
        assert response.status == 200
        result = response.payload
        assert result["reconciled"] is True
        assert result["job"]["state"] == SUCCEEDED
        assert result["job"]["reconciliation"]["listener_state"] == "done"
        assert result["job"]["reconciliation"]["remote_job_id"] == REMOTE_JOB_ID
        assert len(poll_calls) == 1
        request, remote_job_id, wait_s = poll_calls[0]
        assert request.code == "Get-Date" and remote_job_id == REMOTE_JOB_ID and wait_s == 0
        assert listener.requests == [] and listener.cancelled == []
        assert backend.lease["running_jobs"] == []
        assert backend.lease["job_owners"] == {}
        assert backend.cancel["acknowledged"] is True
        replay = _call(hub.service)
        assert replay["already_reconciled"] is True
        assert replay["job"]["reconciliation"]["remote_job_id"] == REMOTE_JOB_ID
        assert len(poll_calls) == 1
    finally:
        jobs.close()


@pytest.mark.parametrize(
    ("listener_state", "error", "exit_code", "timed_out", "expected"),
    [
        ("done", None, 17, False, FAILED),
        ("done", "timed out", 124, True, "timed_out"),
        ("cancelled", "cancelled", 130, False, "cancelled"),
    ],
)
def test_reconcile_maps_only_terminal_listener_results(
    tmp_path, listener_state, error, exit_code, timed_out, expected
):
    listener = FakeListener(
        poll=lambda _request, remote_job_id, *, wait_s: (
            listener_state,
            _result(exit_code=exit_code, error=error, timed_out=timed_out, job_id=remote_job_id),
        )
    )
    hub, backend, jobs = _service(tmp_path, listener)
    try:
        assert _call(hub.service)["job"]["state"] == expected
        assert backend.lease["running_jobs"] == []
    finally:
        jobs.close()


@pytest.mark.parametrize(
    ("state", "error", "remote_id"),
    [
        ("running", None, REMOTE_JOB_ID),
        ("missing", "unknown_job", REMOTE_JOB_ID),
        ("done", None, "different-listener-job"),
    ],
)
def test_reconcile_keeps_pin_when_listener_does_not_confirm_exact_terminal_job(
    tmp_path, state, error, remote_id
):
    listener = FakeListener(
        poll=lambda _request, _remote_job_id, *, wait_s: (
            state,
            _result(error=error, job_id=remote_id),
        )
    )
    hub, backend, jobs = _service(tmp_path, listener)
    try:
        with pytest.raises(HubError) as error:
            _call(hub.service)
        assert error.value.code == "job_reconciliation_unconfirmed"
        assert backend.reconciliation_calls == []
        assert backend.lease["running_jobs"] == [JOB_ID]
        assert backend.lease["job_owners"] == {JOB_ID: OLD_OWNER}
        assert backend.cancel["acknowledged"] is False
        assert listener.requests == [] and listener.cancelled == []
    finally:
        jobs.close()


@pytest.mark.parametrize(
    "override",
    [
        {"lease_id": "another-lease"},
        {"client_id": "another-client"},
        {"target": "dev/another_windows"},
        {"remote_job_id": "another-remote-job"},
    ],
)
def test_reconcile_rejects_mismatched_job_identity_before_listener_poll(tmp_path, override):
    listener = FakeListener(
        poll=lambda *_args, **_kwargs: pytest.fail("identity mismatch must not contact listener")
    )
    hub, backend, jobs = _service(tmp_path, listener)
    try:
        with pytest.raises(HubError) as error:
            _call(hub.service, **override)
        assert error.value.code == "job_identity_mismatch"
        assert backend.reconciliation_calls == []
        assert backend.lease["running_jobs"] == [JOB_ID]
    finally:
        jobs.close()


def test_reconcile_requires_explicitly_inactive_controller_not_expired_heartbeat(tmp_path):
    listener = FakeListener(poll=lambda *_args, **_kwargs: pytest.fail("active owner must not be polled"))
    hub, backend, jobs = _service(tmp_path, listener, inactive=False)
    try:
        with pytest.raises(HubError) as error:
            _call(hub.service)
        assert error.value.code == "job_owner_active"
        assert backend.reconciliation_calls == []
        assert backend.lease["running_jobs"] == [JOB_ID]
    finally:
        jobs.close()


def test_reconcile_reports_listener_transport_failure_without_changing_shared_state(tmp_path):
    listener = FakeListener(
        poll=lambda *_args, **_kwargs: (_ for _ in ()).throw(StorageError("listener unavailable"))
    )
    hub, backend, jobs = _service(tmp_path, listener)
    try:
        with pytest.raises(HubError) as error:
            _call(hub.service)
        assert error.value.code == "listener_status_unavailable"
        assert backend.reconciliation_calls == []
        assert backend.lease["running_jobs"] == [JOB_ID]
        assert backend.cancel["acknowledged"] is False
    finally:
        jobs.close()


def test_reconcile_requires_sql_shared_job_backend(tmp_path):
    listener = FakeListener(poll=lambda *_args, **_kwargs: pytest.fail("file mode cannot reconcile"))
    hub, backend, jobs = _service(tmp_path, listener)
    try:
        hub.service.jobs.backend = None
        with pytest.raises(HubError) as error:
            _call(hub.service)
        assert error.value.code == "sql_reconciliation_required"
    finally:
        hub.service.jobs.backend = backend
        jobs.close()
