import datetime as dt

import pytest
from hub_fakes import Hub

from privy.hub.jobs import HubJob
from privy.hub.metrics import HubMetrics, ReportSelection
from privy.hub.registry import ProbeResult


def iso(at):
    return dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat()


@pytest.fixture
def history(tmp_path):
    hub = Hub(tmp_path)
    target = hub.registry.snapshot().targets[0]
    now = [1_800_000_000.0]
    metrics = HubMetrics(clock=lambda: now[0])
    return metrics, target, now


def add_job(metrics, target, at):
    job = HubJob(job_type="exec", target=target, spec={"code": "echo test"}, origin="rest", output_limit=10)
    job.created_at = at
    job.state = "succeeded"
    metrics.job_added(job)


def test_full_lifetime_samples_and_restart(history):
    metrics, target, now = history
    metrics.call("rest", 200, 0.1)
    metrics.probe(target, ProbeResult("online", now[0], latency_ms=20))
    add_job(metrics, target, now[0])
    now[0] += 40 * 86400
    metrics.call("rest", 500, 0.2)
    metrics.probe(target, ProbeResult("online", now[0], latency_ms=40))
    report = metrics.report([target], {}, selection=ReportSelection(range_name="all"))
    assert report["calls"]["total"] == 2
    assert report["probe_latency_ms"]["p50"] == 20
    assert report["jobs"]["total"] == 1
    assert HubMetrics(clock=lambda: now[0]).report([target], {})["calls"]["total"] == 0


def test_custom_window_filters_both_ends_and_preserves_totals(history):
    metrics, target, now = history
    start = now[0]
    for offset in (-1, 0, 60, 119, 120):
        add_job(metrics, target, start + offset)
    now[0] += 180
    selection = ReportSelection.parse(
        {"range": ["custom"], "start": [iso(start)], "end": [iso(start + 120)], "resolution": ["1m"]}
    )
    report = metrics.report([target], {}, selection=selection)
    assert report["series"]["jobs"] == [1, 2]
    assert report["jobs"]["total"] == sum(report["targets"][0]["series"]) == 3


def test_coarsening_caps_buckets_without_deleting_samples(history):
    metrics, target, now = history
    add_job(metrics, target, now[0])
    now[0] += 400 * 86400
    add_job(metrics, target, now[0] - 1)
    report = metrics.report([target], {}, selection=ReportSelection(range_name="all", resolution="1m"))
    assert 1 <= len(report["series"]["jobs"]) <= 1000
    assert report["resolution_adjusted"] and report["bucket_s"] > 60
    assert sum(report["series"]["jobs"]) == len(metrics.entries()) == 2


@pytest.mark.parametrize("span, adjusted", [(60000, False), (60001, True)])
def test_coarsening_exact_bucket_limit(history, span, adjusted):
    metrics, target, now = history
    selection = ReportSelection(range_name="custom", start=now[0] - span, end=now[0], resolution="1m")
    report = metrics.report([target], {}, selection=selection)
    assert len(report["series"]["jobs"]) <= 1000 and report["resolution_adjusted"] is adjusted


@pytest.mark.parametrize(
    "query",
    [
        {"range": ["nope"]},
        {"range": ["custom"]},
        {"resolution": ["0m"]},
        {"range": ["custom"], "start": ["invalid"], "end": ["2026-01-02T00:00:00Z"]},
        {"range": ["custom"], "start": ["2026-01-02T00:00:00Z"], "end": ["2026-01-01T00:00:00Z"]},
        {"start": ["2026-01-02T00:00:00Z"]},
    ],
)
def test_invalid_selections_are_rejected(query):
    with pytest.raises(ValueError):
        ReportSelection.parse(query)


def test_empty_all_history_and_legacy_presets(history):
    metrics, target, _ = history
    report = metrics.report([target], {}, selection=ReportSelection(range_name="all"))
    assert len(report["series"]["jobs"]) >= 1 and sum(report["series"]["jobs"]) == 0
    for name, bucket, count in (("1h", 60, 60), ("24h", 900, 96), ("7d", 7200, 84)):
        report = metrics.report([target], {}, range_name=name)
        assert report["bucket_s"] == bucket and len(report["series"]["jobs"]) == count


def test_resolution_larger_than_window_still_includes_recent_jobs(history):
    metrics, target, now = history
    add_job(metrics, target, now[0] - 1800)
    report = metrics.report([target], {}, selection=ReportSelection(range_name="1h", resolution="1d"))
    assert sum(report["series"]["jobs"]) == 1


def test_stats_and_overview_selection_contract(tmp_path):
    hub = Hub(tmp_path)
    for path in ("/v1/stats?range=all&resolution=1m", "/ui?range=all&resolution=1m"):
        assert hub.call("GET", path).status == 200
    response = hub.call("GET", "/v1/stats?range=30d&resolution=1m")
    assert response.status == 200 and response.payload["resolution_adjusted"]
    assert len(response.payload["series"]["jobs"]) <= 1000
    assert hub.call("GET", "/v1/stats?range=custom&start=nope&end=nope").status == 400
    assert hub.call("GET", "/ui?range=custom&start=nope&end=nope").status == 400
    assert hub.call("GET", "/v1/stats?resolution=nope").status == 400
    assert hub.call("GET", "/v1/stats?target=nope").status == 400
