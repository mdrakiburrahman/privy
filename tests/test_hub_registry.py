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
    language_from,
    machine_name,
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


def test_resolve_accepts_machine_and_host_names(tmp_path):
    offline = FakeListener(info_error=RelayHTTPError("x", status_code=404, detail=NO_LISTENER))
    registry = _registry(
        tmp_path, {"dev/box2_windows": FakeListener(hostname="BUILD-07"), "contoso-lab/box1_windows": offline}
    )

    assert registry.resolve("box2").id == "dev/box2_windows"
    assert registry.resolve("GPU1").id == "contoso-lab/gpu1_linux"
    assert registry.resolve("build-07").id == "dev/box2_windows"
    assert registry.resolve("contoso-lab/box1").id == "contoso-lab/box1_windows"
    # box1 exists in both relays; only one of them is online, so that one is meant.
    assert registry.resolve("box1").id == "dev/box1_windows"


def test_ambiguous_names_list_every_candidate_with_its_status(tmp_path):
    registry = _registry(tmp_path, {})

    with pytest.raises(TargetLookupError) as ambiguous:
        registry.resolve("box1")

    assert "dev/box1_windows (online)" in str(ambiguous.value)
    assert ambiguous.value.candidates == ["contoso-lab/box1_windows", "dev/box1_windows"]


@pytest.mark.parametrize("name", ["", "   ", "dev/", "dev/  "])
def test_empty_names_never_match_a_target(tmp_path, name):
    # Targets without a known host name (offline, older listeners) must not match an empty name.
    offline = FakeListener(info_error=RelayHTTPError("x", status_code=404, detail=NO_LISTENER))
    registry = _registry(
        tmp_path,
        {
            "dev/box1_windows": FakeListener(legacy=True),
            "dev/box2_windows": offline,
            "contoso-lab/gpu1_linux": offline,
            "contoso-lab/box1_windows": offline,
        },
    )

    with pytest.raises(TargetLookupError):
        registry.resolve(name)


@pytest.mark.parametrize(
    ("info", "path", "language"),
    [
        ({"platform": {"system": "Windows"}}, "box_linux", "powershell"),
        ({"platform": {"system": "Linux"}}, "box_windows", "bash"),
        (None, "box_windows", "powershell"),
        (None, "box_wsl", "bash"),
        (None, "box_devcontainer", "bash"),
        (None, "box", None),
    ],
)
def test_default_language_follows_the_listener_os(info, path, language):
    assert language_from(info, path) == language


def test_machine_names_drop_the_platform_suffix():
    assert machine_name("DevBox01_windows") == "devbox01"
    assert machine_name("gpu_box_linux") == "gpu_box"
    assert machine_name("plain") == "plain"


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
