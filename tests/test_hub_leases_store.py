"""Lease lifecycle and admission tests use a fake clock, not timer sleeps."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from hub_fakes import FakeListener, Hub, blocking_run, relays_document, write_relays

from privy.hub.app import build_hub
from privy.hub.leases import DEFAULT_CONFIG, LeaseError, LeaseStore
from privy.hub.service import HubError


@pytest.fixture
def leasing(tmp_path):
    hub = Hub(tmp_path)
    document = relays_document()
    document["relays"] = [document["relays"][0]]
    document["relays"][0]["hybrid_connections"] = [
        {"path": name, "keyrule": "all", "key": "a2V5MQ=="}
        for name in (
            "raki-windows-big_windows",
            "raki-windows-big_wsl",
            "raki-windows-big_devcontainer",
            "raki-windows-big-3_windows",
            "raki-windows-big-3_wsl",
            "raki-windows-big-3_devcontainer",
            "single",
        )
    ]
    write_relays(hub.relays_file, document)
    now = [1_800_000_000.0]
    store = LeaseStore(hub.registry, home=tmp_path, audit=hub.jobs.audit, clock=lambda: now[0])
    hub.service.leases = store
    hub.jobs.on_finish = store.job_finished
    hub.service.invoke("list_targets", {}, origin="test")
    return hub, store, now


def client(store, name="chat", **kwargs):
    return store.register_client(name, session_id=name, **kwargs)["client_id"]


def acquire(store, client_id, target="raki-windows-big", **kwargs):
    return store.acquire(client_id, target=target, **kwargs)


def test_manual_duration_survives_restart_and_excludes_other_clients(leasing, tmp_path):
    hub, store, now = leasing
    owner, other = client(store), client(store, "other")
    lease = acquire(store, owner, ttl="36h", manual=True)
    restored = LeaseStore(hub.registry, home=tmp_path, clock=lambda: now[0])
    assert restored.get_lease(lease["lease_id"])["manual"] is True
    assert restored.get_lease(lease["lease_id"])["expires_at"] == lease["expires_at"]
    with pytest.raises(LeaseError) as error:
        acquire(restored, other)
    assert error.value.code == "group_leased"
    before = restored.get_lease(lease["lease_id"])["expires_at"]
    now[0] += 60
    restored.groups()
    assert restored.get_lease(lease["lease_id"])["expires_at"] == before
    restored.release(lease["lease_id"], owner)
    assert acquire(restored, other)["created"]


def test_manual_and_agent_duration_limits_are_separate(leasing):
    _, store, now = leasing
    owner = client(store)
    with pytest.raises(LeaseError, match="ttl"):
        acquire(store, owner, ttl="5h")
    for ttl in ("0m", "-1h", "366d"):
        with pytest.raises(LeaseError):
            acquire(store, owner, ttl=ttl, manual=True)
    lease = acquire(store, owner, ttl="365d", manual=True)
    with pytest.raises(LeaseError):
        store.renew(lease["lease_id"], owner, ttl="5h")
    assert store.renew(lease["lease_id"], owner, ttl="8h", manual=True)["ttl_s"] == 28800
    now[0] += 28801
    assert store.get_lease(lease["lease_id"])["state"] == "expired"


def test_legacy_config_and_state_remain_readable(leasing, tmp_path):
    hub, store, now = leasing
    lease = acquire(store, client(store))
    state = json.loads(store.state_path.read_text())
    state["leases"][lease["lease_id"]].pop("manual", None)
    store.state_path.write_text(json.dumps(state), encoding="utf-8")
    restored = LeaseStore(hub.registry, home=tmp_path, clock=lambda: now[0])
    assert restored.get_lease(lease["lease_id"])["ttl_s"] == 1800
    state["leases"][lease["lease_id"]]["manual"] = "yes"
    store.state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="manual"):
        LeaseStore(hub.registry, home=tmp_path)


def test_defaults_persistence_and_client_session_idempotency(leasing, tmp_path):
    hub, store, now = leasing
    first = store.register_client(
        "chat", kind="copilot-app", host="host", session_id="session", labels={"a": "b"}
    )
    again = store.register_client("different name", session_id="session")
    assert again["client_id"] == first["client_id"]
    assert again["created"] is False
    lease = acquire(store, first["client_id"], labels={"any": "value=with=equals"}, purpose="privy: Leasing.")
    assert lease["ttl"] == "30m"
    assert len(lease["members"]) == 3
    restored = LeaseStore(hub.registry, home=tmp_path, clock=lambda: now[0])
    assert restored.get_lease(lease["lease_id"])["expires_at"] == lease["expires_at"]
    assert restored.get_client(first["client_id"])["labels"] == {"a": "b"}
    assert (tmp_path / "leases.yaml").read_text() == DEFAULT_CONFIG
    assert json.loads((tmp_path / "leases-state.json").read_text())["version"] == 1
    assert '"event": "lease.acquired"' in hub.audit_path.read_text()


def test_effective_sql_config_applies_omitted_manual_ttl_default(leasing, tmp_path):
    hub, first, _ = leasing
    second_home = tmp_path / "second"
    second_home.mkdir()
    (second_home / "leases.yaml").write_text(
        DEFAULT_CONFIG.replace("  manual_max_ttl: 365d\n", ""), encoding="utf-8"
    )
    second = LeaseStore(hub.registry, home=second_home)
    assert first.effective_config == second.effective_config


def test_sql_force_release_reconciles_only_stopped_or_unresolved_pins(leasing):
    _, store, _ = leasing
    owner = client(store)
    lease = acquire(store, owner)
    record = store._leases[lease["lease_id"]]
    record["running_jobs"] = ["stopped-job", "live-job"]
    record["job_owners"] = {"stopped-job": "stopped-owner", "live-job": "live-owner"}
    store.sql_backend = SimpleNamespace(
        owner_is_stale=lambda value: value in {"stopped-owner", "unresolved-file-import"}
    )
    with pytest.raises(LeaseError) as error:
        store.release(lease["lease_id"], force=True)
    assert error.value.code == "lease_pinned"
    assert record["running_jobs"] == ["stopped-job", "live-job"]

    record["running_jobs"] = ["stopped-job", "file-job"]
    record["job_owners"] = {"stopped-job": "stopped-owner", "file-job": "unresolved-file-import"}
    store.release(lease["lease_id"], force=True)
    assert record["running_jobs"] == []
    assert record["job_owners"] == {}
    assert record["state"] == "released"
    assert record["events"][-2]["event"] == "pins_reconciled"
    assert "may still be running" in record["events"][-2]["warning"]


def test_first_leasing_start_restores_cooldown_from_existing_hub_audit(tmp_path):
    hub = Hub(tmp_path)
    result = hub.call(
        "POST",
        "/v1/jobs",
        {"target": "dev/box1_windows", "code": "echo human"},
        headers={"X-Privy-UI": "1"},
    )
    assert result.status == 200 and result.payload["state"] == "succeeded"
    store = LeaseStore(hub.registry, home=tmp_path, audit=hub.jobs.audit, clock=lambda: time.time() + 30)
    owner = client(store)
    with pytest.raises(LeaseError) as error:
        store.acquire(owner, target="dev/box1_windows")
    assert error.value.code == "cooling_down"
    assert error.value.details["last_used_by"] == {"origin": "ui"}


def test_sql_group_snapshot_displays_expiry_without_persisting_sweep(leasing, monkeypatch):
    _, store, now = leasing
    owner = client(store)
    lease = acquire(store, owner, ttl="1m")
    now[0] += 61
    before = json.loads(json.dumps(store._leases[lease["lease_id"]]))

    class SnapshotLock:
        @contextmanager
        def read_snapshot(self):
            yield {"now": now[0], "clients": store._clients, "leases": store._leases, "uses": store._uses}

    store._lock = SnapshotLock()
    store.sql_backend = object()
    monkeypatch.setattr(store, "_save", lambda: pytest.fail("dashboard snapshot persisted sweep"))

    groups = {item["group"]: item for item in store.groups()}
    group = groups["dev/raki-windows-big"]
    assert group["state"] == "cooling"
    assert group["lease"] is None
    assert store._leases[lease["lease_id"]] == before


@pytest.mark.parametrize("local_cache", ["empty", "stale"])
def test_sql_group_snapshot_uses_detached_holder_data_with_sql_time(leasing, local_cache):
    _, store, now = leasing
    owner = client(store)
    lease = acquire(store, owner, ttl="1m")
    now[0] += 10
    fresh_client = json.loads(json.dumps(store._clients[owner]))
    fresh_client["name"] = "peer-created-client"
    fresh_client["host"] = "peer-host"
    active = json.loads(json.dumps(store._leases[lease["lease_id"]]))
    expired = json.loads(json.dumps(active))
    expired["lease_id"] = "expired-peer-lease"
    expired["expires_ts"] = now[0] - 1
    expired["running_jobs"] = []
    snapshot_leases = {expired["lease_id"]: expired, lease["lease_id"]: active}
    snapshot_reads = 0

    if local_cache == "empty":
        store._clients = {}
        store._leases = {}
    else:
        stale_client = json.loads(json.dumps(fresh_client))
        stale_client["name"] = "stale-client"
        stale_lease = json.loads(json.dumps(active))
        stale_lease["lease_id"] = "stale-lease"
        store._clients = {owner: stale_client}
        store._leases = {stale_lease["lease_id"]: stale_lease}

    class SnapshotLock:
        @contextmanager
        def read_snapshot(self):
            nonlocal snapshot_reads
            snapshot_reads += 1
            yield {
                "now": now[0],
                "clients": {owner: fresh_client},
                "leases": snapshot_leases,
                "uses": {},
            }

    store._lock = SnapshotLock()
    store.sql_backend = object()
    local_clients = json.loads(json.dumps(store._clients))
    local_leases = json.loads(json.dumps(store._leases))
    assert store.target_summaries([]) == {}
    assert snapshot_reads == 0
    groups = {item["group"]: item for item in store.groups()}
    group = groups["dev/raki-windows-big"]
    assert snapshot_reads == 1

    assert group["lease"]["lease_id"] == lease["lease_id"]
    assert group["lease"]["holder"]["name"] == "peer-created-client"
    assert group["lease"]["holder"]["host"] == "peer-host"
    assert group["lease"]["holder"]["leases"] == [lease["lease_id"]]

    target_ids = [
        "dev/raki-windows-big_windows",
        "dev/raki-windows-big_wsl",
        "dev/raki-windows-big_devcontainer",
    ]
    summaries = store.target_summaries(target_ids)
    assert snapshot_reads == 2
    for target_id in target_ids:
        summary = summaries[target_id]
        assert summary["group"] == "dev/raki-windows-big"
        assert summary["lease"]["lease_id"] == lease["lease_id"]
        assert summary["lease"]["holder"]["name"] == "peer-created-client"
        assert summary["lease"]["holder"]["host"] == "peer-host"
        assert summary["lease"]["holder"]["leases"] == [lease["lease_id"]]
    assert store._clients == local_clients
    assert store._leases == local_leases


def test_group_regex_boundaries_unmatched_and_only_required_layer_online(leasing):
    hub, store, _ = leasing
    hub.listeners["dev/raki-windows-big_wsl"] = FakeListener(info_error=RuntimeError("offline"))
    hub.registry.probe(hub.registry.resolve("dev/raki-windows-big_wsl"), refresh=True)
    groups = {g["group"]: g for g in store.groups()}
    assert set(groups) == {"dev/raki-windows-big", "dev/raki-windows-big-3", "dev/single"}
    assert groups["dev/raki-windows-big"]["state"] == "free"
    assert groups["dev/single"]["members"] == {"target": "dev/single"}
    lease = acquire(store, client(store), target="raki-windows-big_wsl")
    assert lease["group"] == "dev/raki-windows-big"
    assert store.target_summary("dev/raki-windows-big_devcontainer")["lease"]["lease_id"] == lease["lease_id"]


def test_any_allocates_distinct_groups_explicit_acquire_idempotent(leasing):
    _, store, _ = leasing
    owner = client(store)
    first = store.acquire(owner)
    second = store.acquire(owner)
    assert first["group"] != second["group"]
    again = store.acquire(owner, target=first["group"])
    assert again["lease_id"] == first["lease_id"] and not again["created"]


def test_atomic_concurrent_acquire_never_double_grants(leasing):
    _, store, _ = leasing
    owners = [client(store, str(i)) for i in range(12)]
    barrier = threading.Barrier(len(owners))

    def attempt(owner):
        barrier.wait()
        try:
            return acquire(store, owner)["lease_id"]
        except LeaseError as exc:
            assert exc.code == "group_leased" and exc.status == 409
            return None

    with ThreadPoolExecutor(max_workers=len(owners)) as pool:
        results = list(pool.map(attempt, owners))
    assert len([r for r in results if r]) == 1
    assert len(store.list_leases()) == 1


def test_cooldown_unleased_usage_expiry_reclaim_and_explicit_release(leasing):
    _, store, now = leasing
    owner, other = client(store), client(store, "other")
    store.use("dev/raki-windows-big_wsl", origin="ui")
    with pytest.raises(LeaseError) as error:
        acquire(store, owner)
    assert error.value.code == "cooling_down"
    assert error.value.details["last_used_by"] == {"origin": "ui"}
    now[0] += 901
    lease = acquire(store, owner)
    now[0] += 1801
    assert store.get_lease(lease["lease_id"])["state"] == "expired"
    with pytest.raises(LeaseError, match="cooling"):
        acquire(store, other)
    reclaimed = acquire(store, owner)
    assert reclaimed["state"] == "active"
    store.release(reclaimed["lease_id"], owner)
    assert acquire(store, other)["state"] == "active"


def test_expired_token_revives_without_cooldown_but_loses_after_takeover(leasing):
    _, store, now = leasing
    owner, other = client(store), client(store, "other")
    lease = acquire(store, owner)
    now[0] += 1801
    usage = store.use("dev/raki-windows-big_wsl", lease["lease_id"])
    assert usage["purpose"] is None
    revived = store.get_lease(lease["lease_id"])
    assert revived["state"] == "active"
    assert any(e["event"] == "revived" for e in revived["events"])
    now[0] += 2701
    takeover = acquire(store, other)
    with pytest.raises(LeaseError) as error:
        store.use("dev/raki-windows-big_wsl", lease["lease_id"])
    assert error.value.code == "lease_lost"
    assert store.get_lease(takeover["lease_id"])["state"] == "active"


@pytest.mark.parametrize("ending", ["expired", "released"])
@pytest.mark.parametrize("restart", [False, True])
def test_superseded_token_stays_lost_after_new_holder_expires_or_releases(leasing, tmp_path, ending, restart):
    hub, store, now = leasing
    owner, other = client(store), client(store, "other")
    old = acquire(store, owner)
    now[0] += 2701
    newer = acquire(store, other)
    if ending == "expired":
        now[0] += 1801
        assert store.get_lease(newer["lease_id"])["state"] == "expired"
    else:
        store.release(newer["lease_id"], other)
    if restart:
        store = LeaseStore(hub.registry, home=tmp_path, clock=lambda: now[0])
    with pytest.raises(LeaseError) as error:
        store.use("dev/raki-windows-big_wsl", old["lease_id"])
    assert error.value.code == "lease_lost"
    assert store.get_lease(newer["lease_id"])["state"] == ending


@pytest.mark.parametrize("restart", [False, True])
def test_stale_release_cannot_erase_new_holders_expiry_cooldown(leasing, tmp_path, restart):
    hub, store, now = leasing
    owner, other, third = client(store), client(store, "other"), client(store, "third")
    old = acquire(store, owner)
    now[0] += 2701
    newer = acquire(store, other)
    store.use("dev/raki-windows-big_wsl", newer["lease_id"])
    now[0] += 1801
    before = next(g for g in store.groups() if g["group"] == old["group"])
    assert before["state"] == "cooling"
    store.release(old["lease_id"], owner)
    if restart:
        store = LeaseStore(hub.registry, home=tmp_path, clock=lambda: now[0])
    after = next(g for g in store.groups() if g["group"] == old["group"])
    assert after["state"] == "cooling" and after["leasable_at"] == before["leasable_at"]
    with pytest.raises(LeaseError) as error:
        acquire(store, third)
    assert error.value.code == "cooling_down"
    with pytest.raises(LeaseError):
        acquire(store, owner)
    assert acquire(store, other)["state"] == "active"


@pytest.mark.parametrize("restart", [False, True])
def test_unregister_permanently_releases_expired_tokens(leasing, tmp_path, restart):
    hub, store, now = leasing
    owner = client(store)
    lease = acquire(store, owner)
    now[0] += 1801
    assert store.get_lease(lease["lease_id"])["state"] == "expired"
    store.delete_client(owner)
    if restart:
        store = LeaseStore(hub.registry, home=tmp_path, clock=lambda: now[0])
    with pytest.raises(LeaseError) as error:
        store.use("dev/raki-windows-big_wsl", lease["lease_id"])
    assert error.value.code == "lease_released"
    with pytest.raises(LeaseError) as error:
        store.get_client(owner)
    assert error.value.code == "unknown_client"


def test_idle_ageout_still_allows_latest_expired_token_revival(leasing):
    _, store, now = leasing
    owner = client(store)
    lease = acquire(store, owner)
    now[0] += 86401
    assert store.list_clients() == []
    store.use("dev/raki-windows-big_wsl", lease["lease_id"])
    assert store.get_client(owner)["session_id"] == "chat"
    assert store.get_lease(lease["lease_id"])["state"] == "active"


def test_holder_only_release_renew_and_max_ttl(leasing):
    _, store, _ = leasing
    owner, other = client(store), client(store, "other")
    lease = acquire(store, owner)
    for operation in (store.renew, store.release):
        with pytest.raises(LeaseError) as error:
            operation(lease["lease_id"], other)
        assert error.value.code == "not_lease_holder" and error.value.status == 403
    assert store.renew(lease["lease_id"], owner, "4h")["ttl_s"] == 14400
    for ttl in ("0m", "-1h", "5h", "bad", "nan", ""):
        with pytest.raises(LeaseError):
            store.renew(lease["lease_id"], owner, ttl)
    store.release(lease["lease_id"], owner)
    with pytest.raises(LeaseError) as error:
        store.use("dev/raki-windows-big_wsl", lease["lease_id"])
    assert error.value.code == "lease_released"


def test_client_limits_also_apply_to_reviving_expired_token(leasing):
    _, store, now = leasing
    store.client_limit = 1
    owner = client(store)
    old = acquire(store, owner)
    now[0] += 1801
    acquire(store, owner, target="raki-windows-big-3")
    with pytest.raises(LeaseError) as error:
        store.use("dev/raki-windows-big_wsl", old["lease_id"])
    assert error.value.code == "client_lease_limit"


def test_readonly_peek_does_not_renew_and_labels_have_no_special_keys(leasing):
    _, store, now = leasing
    owner = client(store)
    lease = acquire(store, owner, labels={"arbitrary": "a=b", "repo": "anything"})
    expiry = lease["expires_at"]
    now[0] += 120
    assert store.list_leases(label=["arbitrary=a=b", "repo=anything"])[0]["expires_at"] == expiry
    assert store.list_clients()[0]["client_id"] == owner
    assert store.get_lease(lease["lease_id"])["expires_at"] == expiry
    assert store.list_leases(label="arbitrary=wrong") == []


def test_jobs_pin_until_last_job_finishes_and_unleased_commands_still_work(leasing):
    hub, store, now = leasing
    owner = client(store)
    lease = acquire(store, owner, purpose="privy: Tests.")
    target = "dev/raki-windows-big_windows"
    listener = FakeListener(run=blocking_run)
    hub.listeners[target] = listener
    job = hub.service.start_run(
        {"target": target, "code": "hold", "lease_id": lease["lease_id"]}, origin="rest"
    )
    assert listener.started.wait(5)
    now[0] += 1801
    assert store.get_lease(lease["lease_id"])["state"] == "active"
    assert job.id in store.get_lease(lease["lease_id"])["running_jobs"]
    assert job.purpose == "privy: Tests." and job.client_id == owner
    unleased = hub.service.invoke(
        "run", {"target": "dev/raki-windows-big_wsl", "code": "echo human"}, origin="ui"
    )
    assert unleased["state"] == "succeeded"
    assert unleased["lease_warning"]["lease_id"] == lease["lease_id"]
    listener.cancel_event.set()
    hub.jobs.wait(job, 5)
    assert job.done
    assert store.get_lease(lease["lease_id"])["running_jobs"] == []
    now[0] += 1799
    assert store.get_lease(lease["lease_id"])["state"] == "active"
    now[0] += 2
    assert store.get_lease(lease["lease_id"])["state"] == "expired"


def test_wrong_target_broadcast_has_no_partial_submission(leasing):
    hub, store, _ = leasing
    lease = acquire(store, client(store))
    with pytest.raises(HubError) as error:
        hub.service.invoke(
            "broadcast",
            {
                "targets": ["dev/*"],
                "code": "echo should-not-run",
                "lease_id": lease["lease_id"],
            },
            origin="rest",
        )
    assert error.value.code == "target_not_in_lease"
    assert hub.jobs.list() == []


def test_force_release_keeps_running_job_and_prevents_old_token(leasing):
    hub, store, _ = leasing
    lease = acquire(store, client(store))
    target = "dev/raki-windows-big_windows"
    listener = FakeListener(run=blocking_run)
    hub.listeners[target] = listener
    job = hub.service.start_run(
        {"target": target, "code": "hold", "lease_id": lease["lease_id"]}, origin="rest"
    )
    assert listener.started.wait(5)
    store.release(lease["lease_id"], force=True)
    assert not job.done and not job.cancel_requested
    with pytest.raises(LeaseError) as error:
        store.use(target, lease["lease_id"])
    assert error.value.code == "lease_released"
    listener.cancel_event.set()
    hub.jobs.wait(job, 5)


def test_client_idle_ageout_and_unregister_release(leasing):
    _, store, now = leasing
    owner = client(store)
    idle = client(store, "idle")
    lease = acquire(store, owner, ttl="4h")
    now[0] += 86401
    assert store.list_clients() == []
    with pytest.raises(LeaseError) as error:
        store.get_client(idle)
    assert error.value.code == "unknown_client"
    owner = client(store, "new")
    lease = acquire(store, owner)
    store.delete_client(owner)
    assert store.get_lease(lease["lease_id"])["state"] == "released"
    assert store.groups()[0]["state"] == "free"


@pytest.mark.parametrize(
    "replacement",
    [
        "version: 2",
        "version: 1\ndefaults: []",
        DEFAULT_CONFIG.replace("30m", "0m"),
        DEFAULT_CONFIG.replace("4h", "5h"),
        DEFAULT_CONFIG.replace("match: '^", "match: '"),
        DEFAULT_CONFIG.replace("{relay}/{group}", "{missing}"),
        DEFAULT_CONFIG.replace("[windows]", "windows"),
        "groups: [",
    ],
)
def test_invalid_yaml_refuses_start_with_clear_error(tmp_path, replacement):
    (tmp_path / "leases.yaml").write_text(replacement)
    with pytest.raises(ValueError, match="Invalid leases.yaml"):
        build_hub(home=tmp_path, port=0, version="test")


def test_unavailable_unknown_client_and_unknown_target_errors(leasing):
    hub, store, _ = leasing
    owner = client(store)
    with pytest.raises(LeaseError) as error:
        store.acquire("missing")
    assert error.value.code == "unknown_client"
    with pytest.raises(LeaseError) as error:
        acquire(store, owner, "not-a-target")
    assert error.value.code == "unknown_target"
    target = hub.registry.resolve("dev/raki-windows-big_windows")
    hub.listeners[target.id] = FakeListener(info_error=RuntimeError("offline"))
    hub.registry.probe(target, refresh=True)
    with pytest.raises(LeaseError) as error:
        acquire(store, owner)
    assert error.value.code == "unavailable"


def test_multiple_job_pins_and_restart_do_not_leave_permanent_pin(leasing, tmp_path):
    hub, store, now = leasing
    lease = acquire(store, client(store))
    target = hub.registry.resolve("dev/raki-windows-big_windows")
    jobs = [
        SimpleNamespace(
            id=str(i),
            lease_id=lease["lease_id"],
            target=target,
            origin="rest",
            purpose=None,
            state="succeeded",
        )
        for i in range(2)
    ]
    for job in jobs:
        store.job_started(job)
    now[0] += 3600
    store.job_finished(jobs[0])
    assert store.get_lease(lease["lease_id"])["running_jobs"] == ["1"]
    restored = LeaseStore(hub.registry, home=tmp_path, clock=lambda: now[0])
    assert restored.get_lease(lease["lease_id"])["running_jobs"] == []
    assert restored.get_lease(lease["lease_id"])["state"] == "active"
