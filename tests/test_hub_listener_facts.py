"""Listener OS/version facts, facet filters, and the last-known description store."""

import json
from types import SimpleNamespace

from hub_fakes import FakeListener, Hub

from privy.client import RelayHTTPError
from privy.hub.listener_facts import (
    FORGET_S,
    REFRESH_S,
    STATE_FILENAME,
    ListenerInfoStore,
    describe,
    matches,
)
from privy.hub.registry import OFFLINE, ONLINE, ProbeResult

BOX1 = "dev/box1_windows"
NO_LISTENER = RelayHTTPError(
    "x",
    status_code=404,
    detail='{"error":{"code":"EndpointNotFound","message":"There are no listeners connected"}}',
)
WINDOWS = {
    "kind": "listener_info",
    "hostname": "BOX1",
    "privy_version": "0.6.0",
    "platform": {"system": "Windows", "release": "11", "version": "10.0.26200", "machine": "AMD64"},
    "python": {"version": "3.12.10"},
    "capabilities": {"terminal": {"shells": ["powershell", "cmd"]}},
}
LINUX = {
    "kind": "listener_info",
    "privy_version": "0.5.0",
    "platform": {"system": "Linux", "release": "6.18.58.1-microsoft-standard-WSL2", "machine": "x86_64"},
    "python": {"version": "3.11.9"},
    "capabilities": {"terminal": {"shells": ["bash"]}},
}


def test_describe_normalizes_os_arch_versions_shells_and_environment():
    windows = describe(WINDOWS, "box1_windows")
    assert windows == {
        "os_family": "Windows",
        "os_release": "11",
        "os_build": "26200",
        "os_version": "Windows 11 · 26200",
        "arch": "x86_64",
        "privy_version": "0.6.0",
        "python_version": "3.12.10",
        "environment": "Windows host",
        "shells": ["cmd", "powershell"],
    }
    linux = describe(LINUX, "box1_devcontainer")
    assert (linux["os_version"], linux["environment"]) == (
        "Linux 6.18.58.1-microsoft-standard-WSL2",
        "Devcontainer",
    )
    mac = describe(
        {"platform": {"system": "Darwin", "description": "macOS-14.5-arm64-arm-64bit", "machine": "arm64"}},
        "m",
    )
    assert (mac["os_version"], mac["arch"], mac["environment"], mac["shells"]) == (
        "macOS 14.5",
        "arm64",
        None,
        [],
    )
    assert describe(None, "gpu1_linux")["shells"] is None


def test_filters_are_case_insensitive_globs_or_within_and_across_facets():
    windows = describe(WINDOWS, "box1_windows")
    assert matches(windows, {"os": ["WINDOWS"], "privy_version": ["0.5.*", "0.6.*"]})
    assert matches(windows, {"os": ["linux, windows"]})
    assert matches(windows, {"os_version": ["26200"]}) and matches(windows, {"os_version": ["11"]})
    assert matches(windows, {"shell": ["bash", "cmd"]})
    assert not matches(windows, {"os": ["windows"], "environment": ["wsl"]})
    unknown = describe(None, "gpu1_linux")
    assert matches(unknown, {"os": ["unknown"], "shell": ["unknown"]})
    assert matches(
        describe({"platform": {"system": "Linux"}}, "x"), {"shell": ["none"], "python_version": ["unknown"]}
    )


def test_store_keeps_last_description_without_rewriting_unchanged_ones(tmp_path):
    now = 2_000_000_000.0
    store = ListenerInfoStore(tmp_path, clock=lambda: now, save_delay_s=3600)
    box1 = SimpleNamespace(id=BOX1)
    store.record(box1, ProbeResult(ONLINE, now, info={**WINDOWS, "jobs": {"running": 1}, "uptime_s": 9}))
    store.record(SimpleNamespace(id="dev/old"), ProbeResult(ONLINE, now - FORGET_S - 1, info=LINUX))
    store.record(SimpleNamespace(id="dev/box2_windows"), ProbeResult(OFFLINE, now, error="no listener"))
    store.flush()
    state = tmp_path / STATE_FILENAME
    assert set(json.loads(state.read_text(encoding="utf-8"))["targets"]) == {BOX1}
    assert ListenerInfoStore(tmp_path, clock=lambda: now).last(BOX1) == (WINDOWS, now)

    state.unlink()
    store.record(box1, ProbeResult(ONLINE, now + 10, info=WINDOWS))
    store.flush()
    assert not state.exists()
    store.record(box1, ProbeResult(ONLINE, now + REFRESH_S + 1, info=WINDOWS))
    store.flush()
    assert state.exists()

    state.write_text("{not json", encoding="utf-8")
    assert ListenerInfoStore(tmp_path).last(BOX1) is None


def test_last_many_batches_shared_reads_and_preserves_newest_and_expiry():
    now = 2_000_000_000.0

    class Backend:
        owner = "hub-test"

        def __init__(self):
            self.calls = []
            self.rows = {
                "dev/a": {"seen_at": now - 2, "info": LINUX},
                "dev/b": {"seen_at": now - 1, "info": LINUX},
                "dev/c": {"seen_at": now - FORGET_S - 2, "info": LINUX},
            }

        def enqueue(self, *_args):
            pass

        def get_many(self, kind, target_ids):
            self.calls.append((kind, target_ids))
            return {target_id: self.rows[target_id] for target_id in target_ids if target_id in self.rows}

    backend = Backend()
    store = ListenerInfoStore(clock=lambda: now, backend=backend)
    store.record(SimpleNamespace(id="dev/a"), ProbeResult(ONLINE, now - 1, info=WINDOWS))
    store.record(SimpleNamespace(id="dev/c"), ProbeResult(ONLINE, now - FORGET_S - 1, info=WINDOWS))

    result = store.last_many(["dev/a", "dev/a", "dev/b", "dev/c"])

    assert backend.calls == [("listener", ["dev/a", "dev/b", "dev/c"])]
    assert result["dev/a"] == (WINDOWS, now - 1)
    assert result["dev/b"] == (LINUX, now - 1)
    assert result["dev/c"] is None


def test_bulk_fact_readers_use_batched_fallback(tmp_path):
    from privy.hub.dashboard import build_report

    class TrackingStore(ListenerInfoStore):
        def __init__(self, home):
            super().__init__(home, save_delay_s=3600)
            self.calls = []

        def last(self, _target_id):
            raise AssertionError("bulk readers must not perform per-target lookups")

        def last_many(self, target_ids):
            keys = list(target_ids)
            self.calls.append(keys)
            return super().last_many(keys)

    store = TrackingStore(tmp_path)
    hub = Hub(
        tmp_path,
        {
            "dev/box1_windows": FakeListener(info_error=NO_LISTENER),
            "dev/box2_linux": FakeListener(info_error=NO_LISTENER),
        },
        listener_infos=store,
    )

    hub.service.invoke("list_targets", {"probe": False}, origin="test")
    assert len(store.calls) == 1
    store.calls.clear()

    build_report(hub.service)
    assert len(store.calls) == 1
    hub.service.listener_infos.flush()


def test_list_targets_filters_by_facets_and_shows_last_known_details_offline(tmp_path):
    hub = Hub(
        tmp_path,
        {
            BOX1: FakeListener(info=WINDOWS),
            "dev/box2_windows": FakeListener(info_error=NO_LISTENER),
            "contoso-lab/gpu1_linux": FakeListener(info=LINUX),
        },
        listener_infos=ListenerInfoStore(tmp_path),
    )

    def ids(query):
        payload = hub.call("GET", "/v1/targets?" + query).payload
        assert payload["counts"]["targets"] == 4
        return sorted(target["id"] for target in payload["targets"])

    assert ids("os=windows") == ["contoso-lab/box1_windows", BOX1]
    assert ids("os_version=26200&privy_version=0.6.*") == [BOX1]
    assert ids("shell=cmd&shell=bash") == ids("shell=cmd,bash") == ["contoso-lab/gpu1_linux", BOX1]
    assert ids("os=unknown") == ["dev/box2_windows"]
    called = hub.service.invoke("list_targets", {"os": "Linux"}, origin="test")
    assert [target["id"] for target in called["targets"]] == ["contoso-lab/gpu1_linux"]
    box1 = next(
        target for target in hub.call("GET", "/v1/targets").payload["targets"] if target["id"] == BOX1
    )
    assert (box1["os_version"], box1["python_version"], box1["info_stale"]) == (
        "Windows 11 · 26200",
        "3.12.10",
        False,
    )
    hub.service.listener_infos.flush()

    restarted = Hub(
        tmp_path, {BOX1: FakeListener(info_error=NO_LISTENER)}, listener_infos=ListenerInfoStore(tmp_path)
    )
    targets = restarted.call("GET", "/v1/targets?os=windows&environment=windows*").payload["targets"]
    box1 = next(target for target in targets if target["id"] == BOX1)
    assert box1["status"] == "offline" and box1["info_stale"] is True and box1["info_seen_at"]
    assert (box1["os_version"], box1["shells"]) == ("Windows 11 · 26200", ["cmd", "powershell"])
    online = restarted.call("GET", "/v1/targets?os=windows&online_only=true").payload["targets"]
    assert BOX1 not in [target["id"] for target in online]
