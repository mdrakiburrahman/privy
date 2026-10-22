import dataclasses

import pytest
from hub_fakes import FakeListener, FakeRelayClient, write_relays

from privy._relay import RelayTokenExpiredError
from privy.client import RelayHTTPError
from privy.hub.config import RelaysStore
from privy.hub.registry import (
    ERROR,
    NOT_FOUND,
    OFFLINE,
    ONLINE,
    UNAUTHORIZED,
    UNREACHABLE,
    TargetLookupError,
    TargetRegistry,
    describe_relay_failure,
)

NO_LISTENER = (
    '{"error":{"code":"EndpointNotFound","message":"There are no listeners connected for the endpoint."}}'
)
NO_SERVICE = "<Error><Code>404</Code><Detail>No service is hosted at the specified address.</Detail></Error>"


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (RelayHTTPError("x", status_code=404, detail=NO_LISTENER), OFFLINE),
        (RelayHTTPError("x", status_code=404, detail=NO_SERVICE), NOT_FOUND),
        (RelayHTTPError("x", status_code=503), OFFLINE),
        (RelayHTTPError("x", status_code=504), UNREACHABLE),
        (RelayHTTPError("x", status_code=401), UNAUTHORIZED),
        (RelayHTTPError("x", status_code=403), UNAUTHORIZED),
        (RelayHTTPError("x", timed_out=True), UNREACHABLE),
        (RelayHTTPError("connection refused"), ERROR),
        (RelayHTTPError("x", status_code=500), ERROR),
        (RelayTokenExpiredError(1), UNAUTHORIZED),
        (ValueError("boom"), ERROR),
    ],
)
def test_relay_failures_map_to_target_status(exc, status):
    assert describe_relay_failure(exc)[0] == status


def _registry(tmp_path, listeners, **options):
    store = RelaysStore(write_relays(tmp_path / "relays.json"), check_interval_s=0)
    return TargetRegistry(
        store,
        client_factory=lambda target: FakeRelayClient(listeners.setdefault(target.id, FakeListener())),
        **options,
    )


def test_resolve_accepts_ids_and_unique_hybrid_connection_names(tmp_path):
    registry = _registry(tmp_path, {})

    assert registry.resolve("DEV/Box2_Windows").id == "dev/box2_windows"
    assert registry.resolve("gpu1_linux").id == "contoso-lab/gpu1_linux"
    with pytest.raises(TargetLookupError, match="ambiguous") as ambiguous:
        registry.resolve("box1_windows")
    assert ambiguous.value.candidates == ["contoso-lab/box1_windows", "dev/box1_windows"]
    with pytest.raises(TargetLookupError, match="did you mean") as unknown:
        registry.resolve("dev/box3_windows")
    assert "dev/box2_windows" in unknown.value.candidates


def test_resolve_explains_an_empty_registry(tmp_path):
    registry = TargetRegistry(RelaysStore(tmp_path / "missing.json"))

    with pytest.raises(TargetLookupError, match="no relays are hydrated"):
        registry.resolve("anything")


def test_select_matches_ids_or_hybrid_connection_names(tmp_path):
    registry = _registry(tmp_path, {})

    assert [target.id for target in registry.select(["dev/*"])] == ["dev/box1_windows", "dev/box2_windows"]
    assert [target.id for target in registry.select(["*_linux"])] == ["contoso-lab/gpu1_linux"]
    assert len(registry.select(["*"])) == 4
    assert [target.id for target in registry.select(["gpu*", "dev/box2*"])] == [
        "contoso-lab/gpu1_linux",
        "dev/box2_windows",
    ]
    assert registry.select(["", "  "]) == []


def test_probes_are_cached_until_refreshed_or_rehydrated(tmp_path):
    listeners = {}
    registry = _registry(tmp_path, listeners, probe_ttl_s=60)
    target = registry.resolve("dev/box1_windows")

    first = registry.probe(target)
    second = registry.probe(target)
    assert first is second and first.status == ONLINE and first.info["hostname"] == "box"
    assert listeners[target.id].info_calls == 1

    registry.probe(target, refresh=True)
    assert listeners[target.id].info_calls == 2

    rehydrated = dataclasses.replace(target, key="bmV3")
    assert registry.cached(rehydrated) is None


def test_probe_reports_legacy_and_failing_listeners(tmp_path):
    listeners = {
        "dev/box1_windows": FakeListener(legacy=True),
        "dev/box2_windows": FakeListener(info_error=RelayHTTPError("x", status_code=404, detail=NO_LISTENER)),
    }
    registry = _registry(tmp_path, listeners)

    results = registry.probe_many(registry.select(["dev/*"]))

    legacy = results["dev/box1_windows"]
    assert legacy.status == ONLINE and legacy.legacy and legacy.info is None
    offline = results["dev/box2_windows"]
    assert offline.status == OFFLINE
    assert "no listener" in offline.error
    assert offline.latency_ms is not None


def test_job_failures_update_the_cached_status(tmp_path):
    registry = _registry(tmp_path, {})
    target = registry.resolve("dev/box1_windows")
    assert registry.probe(target).status == ONLINE

    status, _ = registry.note_failure(target, RelayHTTPError("x", status_code=404, detail=NO_LISTENER))

    assert status == OFFLINE
    assert registry.cached(target).status == OFFLINE
