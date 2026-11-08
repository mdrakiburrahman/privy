import json
import re
import threading
import time

import pytest
from hub_fakes import FakeListener, Hub, blocking_run, exec_result

from privy.client import RelayHTTPError
from privy.hub import dashboard
from privy.hub.dashboard import fmt_bytes, fmt_ms, sparkline
from privy.hub.jobs import utc_iso
from privy.hub.metrics import HubMetrics, public_report
from privy.hub.registry import ProbeResult
from privy.transfer import TransferResult

NO_LISTENER = '{"error":{"code":"EndpointNotFound","message":"There are no listeners connected"}}'
OFFLINE = RelayHTTPError("x", status_code=404, detail=NO_LISTENER)
BOX1 = "dev/box1_windows"
BOX2 = "dev/box2_windows"
BROWSER = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


@pytest.fixture
def hub(tmp_path):
    return Hub(
        tmp_path,
        {
            BOX1: FakeListener(hostname="BOX1"),
            BOX2: FakeListener(info_error=OFFLINE),
            "contoso-lab/gpu1_linux": FakeListener(legacy=True),
        },
    )


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def run(hub, target, code, language="bash"):
    job = hub.call("POST", "/v1/jobs", {"target": target, "language": language, "code": code}).payload
    assert wait_for(lambda: hub.jobs.get(job["job_id"]).done), job
    return job["job_id"]


def html_of(response):
    assert response.status == 200, response.payload
    assert response.media_type().startswith("text/html")
    return response.text


# ---- negotiation and pages ---------------------------------------------------------


def test_gather_probes_renders_before_stale_refresh_finishes():
    started = threading.Event()
    release = threading.Event()
    target = type("Target", (), {"id": "dev/test_windows"})()

    class Registry:
        probe_ttl_s = 30

        def snapshot(self):
            return type("Snapshot", (), {"targets": [target]})()

        def cached(self, _target):
            return None

        def probe_many(self, _targets):
            started.set()
            release.wait(5)

    registry = Registry()
    service = type("Service", (), {"registry": registry})()
    begin = time.monotonic()
    try:
        assert dashboard.gather_probes(service) == {target.id: None}
        assert started.wait(1)
        assert time.monotonic() - begin < 2
    finally:
        release.set()


def test_sql_history_report_reuses_its_job_counts(monkeypatch):
    class Registry:
        def snapshot(self):
            return type("Snapshot", (), {"targets": []})()

    class Jobs:
        max_running = 3

        def counts(self):
            raise AssertionError("SQL history already provides the report counts")

    class Backend:
        def report_history(self, **_kwargs):
            raise AssertionError("The metrics report owns the history read")

    class Metrics:
        backend = Backend()

        def report(self, _targets, _probes, **kwargs):
            self.kwargs = kwargs
            return {"targets": []}

    metrics = Metrics()
    service = type(
        "Service",
        (),
        {
            "registry": Registry(),
            "jobs": Jobs(),
            "metrics": metrics,
            "listener_facts_many": lambda _self, _targets, _probes: {},
        },
    )()
    monkeypatch.setattr(dashboard, "gather_probes", lambda _service: {})

    dashboard.build_report(service)

    assert metrics.kwargs["max_running"] == 3
    assert not {"jobs_running", "jobs_queued", "jobs_retained"} & metrics.kwargs.keys()


def test_root_serves_the_dashboard_to_browsers_and_markdown_to_everyone_else(hub):
    page = hub.call("GET", "/", headers=BROWSER)
    text = html_of(page)
    assert "<title>Overview · privy hub</title>" in text
    nonce = re.search(r"script-src 'nonce-([^']+)'", page.headers["Content-Security-Policy"]).group(1)
    assert f'<script nonce="{nonce}">' in text and f'<style nonce="{nonce}">' in text
    assert page.headers["X-Frame-Options"] == "DENY"
    # Inline style attributes would be blocked by the CSP.
    assert " style=" not in text

    markdown = hub.call("GET", "/")
    assert markdown.media_type().startswith("text/plain") and markdown.text.startswith("# privy hub")
    assert markdown.headers["Vary"] == "Accept"
    assert hub.call("GET", "/llms.txt", headers=BROWSER).media_type().startswith("text/plain")


@pytest.mark.parametrize(
    "path", ["/ui", "/ui/", "/ui/hycos", "/ui/leases", "/ui/logs", "/ui/jobs", "/ui/jobs?state=failed"]
)
def test_every_page_renders_without_jobs(hub, path):
    text = html_of(hub.call("GET", path))
    assert 'id="main"' in text and 'id="nav"' in text


def test_favicon_is_referenced_and_served_for_dashboard_pages(hub):
    for path in ("/", "/ui/hycos"):
        page = hub.call("GET", path, headers=BROWSER)
        assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml">' in html_of(page)
        assert "img-src 'self' data:" in page.headers["Content-Security-Policy"]

    favicon = hub.call("GET", "/favicon.svg")
    assert favicon.status == 200
    assert favicon.media_type() == "image/svg+xml"
    assert "<text " in favicon.text and ">P</text>" in favicon.text
    assert favicon.headers["X-Content-Type-Options"] == "nosniff"


def test_dashboard_pages_are_read_only(hub):
    assert hub.call("POST", "/ui/hycos").status == 405
    assert hub.call("POST", "/v1/stats").status == 405


def test_hycos_are_ranked_by_use_with_sparklines(hub):
    run(hub, BOX1, "hostname")
    run(hub, BOX1, "whoami")
    run(hub, "contoso-lab/gpu1_linux", "uptime")

    text = html_of(hub.call("GET", "/ui/hycos?range=1h"))
    rows = re.findall(r'<tr data-href="/ui/logs\?target=([^"]+)"', text)
    assert rows[:2] == ["dev%2Fbox1_windows", "contoso-lab%2Fgpu1_linux"]
    assert text.count('<svg class="spark"') == 4
    assert 'class="seg"' in text and 'href="/ui/hycos?range=7d"' in text


def test_purpose_column_continuum_is_escaped_and_never_tagged_cells_are_blank(hub):
    purpose = 'privy: <script>alert("purpose")</script>'
    assert hub.call("POST", "/v1/jobs", {"target": BOX1, "code": "x", "purpose": purpose}).status == 200
    run(hub, BOX1, "x")
    response = hub.call("GET", "/ui/hycos")
    text = html_of(response)
    assert "Last reported purpose" in text
    assert 'class="purpose-bar"' in text and 'data-kind="unknown"' in text
    assert "Latest use: unknown" in text and "UTC" in text
    assert "privy: &lt;script&gt;" in text and '<script>alert("purpose")' not in text
    assert '<td class="purpose-cell" data-v=""></td>' in text
    assert " style=" not in text
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]


def test_logs_show_commands_and_output_as_escaped_terminals(hub):
    job_id = run(hub, BOX1, "echo '<script>alert(1)</script>'")

    text = html_of(hub.call("GET", f"/ui/logs?target={BOX1}"))
    assert f'id="j-{job_id}"' in text and 'class="term ok"' in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script>alert(1)" not in text
    assert '<span class="pr">operator@BOX1</span>' in text
    assert "[exit 0 · succeeded" in text

    everything = html_of(hub.call("GET", "/ui/logs?target=*"))
    assert f'id="j-{job_id}"' in everything


def test_running_jobs_are_live_terminals(tmp_path):
    hub = Hub(tmp_path, {BOX1: FakeListener(hostname="BOX1", run=blocking_run)})
    job = hub.call(
        "POST",
        "/v1/jobs",
        {"target": BOX1, "language": "powershell", "code": "Get-Date\nhostname", "wait_s": 0},
    )
    listener = hub.listeners[BOX1]
    assert listener.started.wait(5)
    try:
        text = html_of(hub.call("GET", f"/ui/logs?target={BOX1}"))
        assert f'data-job="{job.payload["job_id"]}" data-kind="exec" data-live open' in text
        assert (
            '<span class="pr">PS</span> <span class="pa">BOX1</span>&gt; <span class="cm">Get-Date</span>'
            in text
        )
        assert '<span class="pr">&gt;&gt;</span> <span class="cm">hostname</span>' in text
        assert 'class="cur"' in text
    finally:
        listener.cancel_event.set()
        assert wait_for(lambda: hub.jobs.get(job.payload["job_id"]).done)


def test_raw_log_has_the_full_output(hub):
    job_id = run(hub, BOX1, "hostname")

    response = hub.call("GET", f"/ui/jobs/{job_id}/log")
    assert response.status == 200 and response.media_type().startswith("text/plain")
    assert "$ hostname" in response.text and "ran bash: hostname" in response.text
    assert "[succeeded exit=0" in response.text
    assert hub.call("GET", "/ui/jobs/nope/log").status == 404


def test_jobs_page_filters_by_state(tmp_path):
    def fail(request, on_output, on_submit, listener):
        on_output("stderr", b"boom\n")
        return exec_result(exit_code=1, stderr=b"boom\n", error="it broke")

    hub = Hub(tmp_path, {BOX1: FakeListener(hostname="BOX1"), BOX2: FakeListener(run=fail)})
    run(hub, BOX1, "hostname")
    run(hub, BOX2, "nope")

    every = html_of(hub.call("GET", "/ui/jobs"))
    only_failed = html_of(hub.call("GET", "/ui/jobs?state=failed"))
    assert every.count('<tr data-href="/ui/logs') == 2
    assert only_failed.count('<tr data-href="/ui/logs') == 1 and "nope" in only_failed
    logs = html_of(hub.call("GET", f"/ui/logs?target={BOX2}"))
    assert 'class="term bad"' in logs and '<span class="err">boom\n</span>' in logs


def test_jobs_and_logs_pages_bound_job_history_reads(hub, monkeypatch):
    for target, command in ((BOX1, "one"), (BOX1, "two"), (BOX2, "three"), (BOX2, "four")):
        run(hub, target, command)

    metrics = hub.service.metrics
    entry_calls = []
    count_calls = []
    totals_calls = []
    original_entries = metrics.entries
    original_counts = metrics.count_jobs_by_target
    original_total = metrics.count_job_records

    def entries(*args, **kwargs):
        entry_calls.append((args, kwargs))
        return original_entries(*args, **kwargs)

    def counts(*args, **kwargs):
        count_calls.append(kwargs)
        return original_counts(*args, **kwargs)

    def total(*args, **kwargs):
        totals_calls.append(kwargs)
        return original_total(*args, **kwargs)

    monkeypatch.setattr(metrics, "entries", entries)
    monkeypatch.setattr(metrics, "count_jobs_by_target", counts)
    monkeypatch.setattr(metrics, "count_job_records", total)

    jobs = html_of(hub.call("GET", "/ui/jobs?limit=1"))
    assert len(re.findall(r'<tr data-href="/ui/logs', jobs)) == 1
    assert "4 jobs" in jobs and "Show more (3 older jobs)" in jobs
    assert any(kwargs.get("limit") == 2 and kwargs.get("states") is None for _, kwargs in entry_calls)
    assert totals_calls[-1]["states"] is None

    logs = html_of(hub.call("GET", f"/ui/logs?target={BOX1}&limit=1"))
    assert len(re.findall(r'<details class="term', logs)) == 1
    assert "2 jobs kept" in logs and "Show more (1 older job)" in logs
    assert count_calls[-1] == {"lease_id": None}
    assert any(args == (BOX1,) and kwargs.get("limit") == 2 for args, kwargs in entry_calls)


def test_jobs_and_leases_pages_skip_full_dashboard_history_report(hub, monkeypatch):
    run(hub, BOX1, "one")

    def unexpected_report(*args, **kwargs):
        pytest.fail("jobs and leases pages must not load the full dashboard report")

    monkeypatch.setattr(dashboard, "build_report", unexpected_report)

    jobs = html_of(hub.call("GET", "/ui/jobs"))
    leases = html_of(hub.call("GET", "/ui/leases"))
    assert "1 job" in jobs
    assert "<title>Leases · privy hub</title>" in leases


def test_ui_calls_are_counted_but_not_echoed(hub, capsys):
    hub.app.log_call("GET", "/ui", b"", hub.call("GET", "/ui"), 0.01)
    hub.app.log_call("GET", "/v1", b"", hub.call("GET", "/v1"), 0.01)
    hub.app.log_call("GET", "/healthz", b"", hub.call("GET", "/healthz"), 0.01)

    calls = hub.call("GET", "/v1/stats").payload["calls"]
    assert calls["by_surface"] == {"rest": 1, "mcp": 0, "ui": 1, "health": 1}
    assert "GET /ui" not in capsys.readouterr().err


# ---- stats ------------------------------------------------------------------------------


def test_stats_report_counts_per_hyco(hub):
    run(hub, BOX1, "hostname")
    run(hub, BOX1, "hostname")

    stats = hub.call("GET", "/v1/stats?range=1h").payload
    assert stats["range"] == "1h" and stats["bucket_s"] == 60 and len(stats["series"]["jobs"]) == 60
    assert stats["jobs"]["total"] == 2 and stats["jobs"]["succeeded"] == 2
    top = stats["targets"][0]
    assert top["id"] == BOX1 and top["jobs"] == 2 and top["success_pct"] == 100.0 and top["host"] == "BOX1"
    assert sum(top["series"]) == 2
    assert stats["top_commands"][0] == {"command": "hostname", "count": 2, "failed": 0, "targets": [BOX1]}
    assert not any(key.endswith("_ts") for key in stats) and "last_used_ts" not in top
    json.dumps(stats)


def test_stats_report_status_since_and_last_online_from_probes(hub):
    target = next(t for t in hub.registry.snapshot().targets if t.id == BOX1)
    metrics = HubMetrics()
    start = time.time() - 600

    def row(probe):
        return public_report(metrics.report([target], {BOX1: probe}, range_name="1h"))["targets"][0]

    metrics.probe(target, ProbeResult("online", start, latency_ms=5))
    metrics.probe(target, ProbeResult("online", start + 30, latency_ms=5))
    online = row(ProbeResult("online", start + 30, latency_ms=5))
    assert online["status_since"] == utc_iso(start) and online["status_since_first_check"] is True
    assert online["last_online"] == utc_iso(start + 30)

    offline = ProbeResult("offline", start + 60, error="no listener")
    # A failed job, not a probe, marked it offline: when that happened is unknown.
    assert row(offline)["status_since"] is None and row(offline)["status_since_first_check"] is None
    metrics.probe(target, offline)
    later = row(offline)
    assert later["status_since"] == utc_iso(start + 60) and later["status_since_first_check"] is False
    assert later["last_online"] == utc_iso(start + 30)
    assert not any(key.endswith("_ts") for key in later)


def test_stats_rejects_unknown_ranges(hub):
    assert hub.call("GET", "/v1/stats?range=2y").status == 400
    assert hub.call("GET", "/v1/stats?target=x").status == 400


def test_history_is_restored_from_the_audit_log(hub):
    first = run(hub, BOX1, "hostname")
    run(hub, BOX1, "whoami")

    metrics = HubMetrics()
    assert metrics.restore(hub.audit_path) == 2
    entry = metrics.entry(first)
    assert entry.restored and entry.command == "hostname" and entry.state == "succeeded"
    report = public_report(metrics.report(hub.registry.snapshot().targets, {}, range_name="24h"))
    assert report["restored_jobs"] == 2 and report["targets"][0]["jobs"] == 2
    assert entry.output(1024) == ([], False)


def test_restore_marks_unfinished_jobs(tmp_path):
    audit = tmp_path / "audit.jsonl"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start = {
        "ts": now,
        "event": "job.start",
        "job_id": "j1",
        "type": "exec",
        "target": BOX1,
        "code": "sleep 1",
    }
    audit.write_text(json.dumps(start) + "\nnot json\n", encoding="utf-8")

    metrics = HubMetrics()
    assert metrics.restore(audit) == 1
    assert metrics.entry("j1").error == "the hub stopped before this job finished"
    assert HubMetrics().restore(tmp_path / "missing.jsonl") == 0


# ---- helpers ----------------------------------------------------------------------------


def test_formatting_helpers():
    assert (
        fmt_ms(None) == "—"
        and fmt_ms(250) == "250 ms"
        and fmt_ms(1500) == "1.5 s"
        and fmt_ms(61_000) == "1m 01s"
    )
    assert fmt_bytes(0) == "0 B" and fmt_bytes(2048) == "2.0 KiB"
    assert 'class="z"' in sparkline([0, 0, 0])
    assert 'class="e"' in sparkline([1, 2, 3], [0, 1, 0])


def test_hycos_are_labelled_by_hybrid_connection(hub):
    run(hub, BOX1, "hostname")
    stats = hub.call("GET", "/v1/stats").payload
    labels = {row["id"]: row["label"] for row in stats["targets"]}
    assert labels[BOX1] == BOX1  # box1_windows also exists on another relay
    assert labels[BOX2] == "box2_windows"
    assert '<b class="nw">box2_windows</b>' in html_of(hub.call("GET", "/ui/hycos"))


# ---- live transfers (regression: /ui/logs 500ed while a transfer was mid-flight) ---------


def test_live_transfer_with_progress_renders_on_every_page(tmp_path):
    started, release = threading.Event(), threading.Event()

    def transfer(direction, source, destination, overwrite, progress):
        progress(1024, 4096)
        started.set()
        release.wait(5)
        return TransferResult(direction, str(source), str(destination), 4096, "b" * 64, "t")

    hub = Hub(tmp_path, {BOX1: FakeListener(transfer=transfer)})
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * 4096)
    try:
        job = hub.call(
            "POST",
            "/v1/files/upload",
            {"target": BOX1, "local_path": str(source), "remote_path": "/tmp/source.bin", "wait_s": 0},
        ).payload
        assert started.wait(5)
        assert wait_for(lambda: hub.jobs.get(job["job_id"]).progress == (1024, 4096))
        for path in ("/ui/logs", f"/ui/logs?target={BOX1}", "/ui/logs?target=*", "/ui/jobs", "/ui"):
            text = html_of(hub.call("GET", path, headers=BROWSER))
            if path.startswith("/ui/logs"):
                assert "1.0 KiB of 4.0 KiB" in text
        assert hub.call("GET", "/v1/stats").status == 200
    finally:
        release.set()
    assert wait_for(lambda: hub.jobs.get(job["job_id"]).done)
    assert "could not render" not in html_of(hub.call("GET", "/ui/logs?target=*", headers=BROWSER))


def test_one_unrenderable_job_does_not_break_the_logs_page(hub, monkeypatch):
    job_id = run(hub, BOX1, "hostname")

    def boom(*args, **kwargs):
        raise ValueError("bad job")

    monkeypatch.setattr(dashboard, "terminal", boom)
    text = html_of(hub.call("GET", f"/ui/logs?target={BOX1}", headers=BROWSER))
    assert "could not render this job: ValueError" in text
    assert f"/ui/jobs/{job_id}/log" in text


def test_hycos_page_has_a_search_box_with_per_row_search_keys(hub):
    text = html_of(hub.call("GET", "/ui/hycos", headers=BROWSER))
    assert 'id="hycoq"' in text and 'data-s="up"' in text and 'data-s="down"' in text
    keys = dict(re.findall(r'data-href="[^"]*target=([^"&]+)[^"]*" data-q="([^"]*)" data-up="[01]"', text))
    offline = keys["dev%2Fbox2_windows"].split()  # offline HYCOs are listed and searchable too
    assert {"box2_windows", "offline", "down"} <= set(offline)
    assert "up" in keys["dev%2Fbox1_windows"].split()
    assert "gpu1_linux" in keys["contoso-lab%2Fgpu1_linux"]
    assert re.search(r'target=dev%2Fbox2_windows" data-q="[^"]*" data-up="0"', text)


def test_hycos_page_leads_with_colored_status_last_used_and_last_seen_online(hub):
    text = html_of(hub.call("GET", "/ui/hycos", headers=BROWSER))
    table = text.split('<table id="hycos">', 1)[1]
    head = re.findall(r"<th[^>]*>([^<]+)", table.split("</thead>", 1)[0])
    assert head[:12] == [
        "#",
        "Relay HYCO",
        "Status",
        "Last used",
        "Last seen online",
        "OS",
        "Env",
        "Arch",
        "Privy",
        "Python",
        "Shell",
        "Lease",
    ]
    assert re.findall(r'<th class="col-(\w+)" data-sort hidden>', table) == ["arch", "shell"]

    def row(target):
        return re.search(rf'<tr data-href="/ui/logs\?target={target}".*?</tr>', table, re.S).group(0)

    box1, box2 = row("dev%2Fbox1_windows"), row("dev%2Fbox2_windows")
    assert '<span class="hyco-status online">online</span>' in box1 and "for at least" in box1
    assert "operator@BOX1 · Windows-11 · privy 0.2.0" in box1 and ">now</td>" in box1
    assert re.search(r'<td class="col-os nw fact-clip"[^>]*>Windows</td>', box1)
    assert re.search(r'<td class="col-environment nw"[^>]*>Windows host</td>', box1)
    assert re.search(r'<td class="col-arch nw"[^>]*hidden>x86_64</td>', box1)
    assert re.search(r'<td class="col-privy nw"[^>]*>0.2.0</td>', box1)
    assert '<td class="col-python faint" data-v="">—</td>' in box1
    assert '<span class="hyco-status offline">offline</span>' in box2
    assert "no listener is connected" in box2 and "not since hub start" in box2
    assert '<td class="col-os faint" data-v="">—</td>' in box2
    assert 'class="dot' not in table


@pytest.mark.parametrize("path", ["/ui", "/ui/hycos", "/ui/leases", "/ui/exec", "/ui/logs", "/ui/jobs"])
def test_all_sidebar_links_have_consistent_decorative_icons(hub, path):
    text = html_of(hub.call("GET", path, headers=BROWSER))
    sidebar = re.search(r'<aside class="sidebar">([\s\S]*?)</aside>', text).group(1)
    links = re.findall(r'<a href="([^"]*)"[^>]*>([\s\S]*?)</a>', sidebar)
    assert len(links) == 10
    for _, content in links:
        assert content.count("<svg ") == 1
        assert 'viewBox="0 0 24 24"' in content
        assert 'aria-hidden="true"' in content
        assert 'stroke="currentColor"' in content
        assert re.sub(r"<[^>]*>", "", content).strip()
    assert {href.split("?")[0] for href, _ in links} == {
        "/ui",
        "/ui/hycos",
        "/ui/leases",
        "/ui/exec",
        "/ui/logs",
        "/ui/jobs",
        "/llms.txt",
        "/v1/stats",
        "/openapi.json",
        "/v1/targets",
    }
    assert "<span>4</span>" in sidebar
    assert re.search(r'<a href="[^"]*" class="on"><svg', sidebar)
