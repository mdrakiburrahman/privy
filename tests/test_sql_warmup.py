import threading
import time
from types import SimpleNamespace

from privy.hub.app import _Warmup
from privy.hub.metrics import RANGES


def test_warmup_populates_common_reads_without_requests_and_reports_failures(monkeypatch):
    ready = threading.Event()
    ready.set()
    calls = []
    errors = []
    registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(targets=[]),
        probe_many=lambda targets: calls.append("probe"),
        cached=lambda target: None,
        probe_ttl_s=30,
    )
    metrics = SimpleNamespace(
        count_jobs_by_lease=lambda: None,
        count_jobs_by_target=lambda: None,
        entries=lambda **kwargs: None,
        report=lambda *args, **kwargs: calls.append(kwargs["selection"].range_name),
    )
    leases = SimpleNamespace(
        target_summaries=lambda targets: None,
        groups=lambda: None,
        list_leases=lambda **kwargs: None,
        list_clients=lambda **kwargs: None,
    )
    service = SimpleNamespace(
        registry=registry,
        metrics=metrics,
        leases=leases,
        jobs=SimpleNamespace(list=lambda **kwargs: None, max_running=3),
        listener_facts_many=lambda *args: None,
    )
    model = SimpleNamespace(
        _ready=ready,
        reads=SimpleNamespace(core_error=lambda: errors[0] if errors else None),
        health=lambda: {"last_error": None},
    )
    monkeypatch.setattr("privy.hub.dashboard.build_shell_report", lambda *args: None)
    warmup = _Warmup(SimpleNamespace(service=service), model)
    assert warmup.status()["status"] == "warming"
    try:
        warmup.start()
        deadline = time.monotonic() + 3
        while not warmup.status()["warm"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert warmup.status()["status"] == "ready"
        assert warmup.status()["warm_seconds"] is not None
        assert calls == ["probe", *RANGES, "all"]
        errors.append("StorageError")
        assert warmup.status()["status"] == "degraded"
        assert not warmup.status()["warm"]
        errors.clear()
        assert warmup.status()["status"] == "ready"
    finally:
        warmup.close()
