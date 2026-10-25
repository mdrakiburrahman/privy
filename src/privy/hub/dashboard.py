"""The hub's control-plane website: server-rendered HTML over :class:`~privy.hub.metrics.HubMetrics`.

Every page is one self-contained document (inline CSS and JS under a per-response CSP nonce, no
external requests), readable without JavaScript; the script only adds sorting, filtering,
auto-refresh, and live log following.
"""

from __future__ import annotations

import html
import logging
import os
import secrets
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode

from privy.hub.dashboard_assets import CSS, JS
from privy.hub.metrics import BAD_STATES, DEFAULT_RANGE, RANGES, JobEntry
from privy.hub.registry import ONLINE, UNKNOWN, ProbeResult
from privy.hub.responses import HTML_TYPE, TEXT_TYPE, HttpResponse, error_response

log = logging.getLogger(__name__)

#: How long a page waits for listeners whose status is stale before rendering what it has.
PROBE_WAIT_S = 3.0
#: Output shown inline per terminal; the rest is one click away at ``/ui/jobs/{id}/log``.
TERMINAL_BYTES = 64 * 1024
#: Terminals per page on the Logs tab, and rows on the Jobs tab, unless ``?limit=`` says otherwise.
DEFAULT_LOG_LIMIT = 50
DEFAULT_JOB_LIMIT = 200
MAX_LIMIT = 5000
#: Terminals opened by default (besides running ones).
OPEN_NEWEST = 3

PAGES = {
    "overview": ("/ui", "Overview"),
    "hycos": ("/ui/hycos", "Relay HYCOs"),
    "logs": ("/ui/logs", "Logs"),
    "jobs": ("/ui/jobs", "Jobs"),
}
RUNNING_STATES = ("running", "queued")
ON = ' class="on"'

esc = html.escape


# ---- formatting -----------------------------------------------------------------


def fmt_int(value: int | float | None) -> str:
    return "—" if value is None else f"{int(value):,}"


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 1000:
        return f"{int(value)} ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def fmt_bytes(value: int | None) -> str:
    if not value:
        return "—" if value is None else "0 B"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def fmt_ago(at: float | None, now: float) -> str:
    if at is None:
        return "—"
    delta = max(0, int(now - at))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h {delta % 3600 // 60}m ago"
    return f"{delta // 86400}d {delta % 86400 // 3600}h ago"


def fmt_span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def local_time(at: float | None, *, short: bool = False) -> str:
    if at is None:
        return "—"
    return time.strftime("%H:%M:%S" if short else "%Y-%m-%d %H:%M:%S", time.localtime(at))


def fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:g}%"


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


# ---- SVG ------------------------------------------------------------------------


def sparkline(
    values: Sequence[int], failed: Sequence[int] = (), *, width: int = 132, height: int = 26
) -> str:
    """Line-and-area sparkline; red ticks along the bottom mark buckets with failures."""
    count = len(values)
    top = max(values, default=0)
    label = f"{sum(values)} jobs, peak {top} per bucket"
    if count < 2 or top == 0:
        return (
            f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{esc(label)}"><line class="z" x1="0" y1="{height - 2}" x2="{width}" y2="{height - 2}"/></svg>'
        )
    step = width / (count - 1)
    usable = height - 4
    points = [(i * step, height - 2 - usable * value / top) for i, value in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f"0,{height - 1} {line} {width},{height - 1}"
    ticks = "".join(
        f'<rect class="e" x="{max(0.0, i * step - 1.5):.1f}" y="{height - 2}" width="3" height="2"/>'
        for i, value in enumerate(failed)
        if value
    )
    return (
        f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{esc(label)}"><polygon class="a" points="{area}"/><polyline class="l" points="{line}"/>'
        f"{ticks}</svg>"
    )


def hbar(fraction: float, *, width: int = 150, hot: bool = False) -> str:
    filled = max(0.0, min(1.0, fraction)) * width
    return (
        f'<svg class="bar" width="{width}" height="6" viewBox="0 0 {width} 6" aria-hidden="true">'
        f'<rect class="t" width="{width}" height="6" rx="3"/>'
        f'<rect class="f{" hot" if hot else ""}" width="{filled:.1f}" height="6" rx="3"/></svg>'
    )


def activity_chart(report: dict[str, Any]) -> str:
    """Stacked columns of exec, transfer, and sync jobs per bucket, failures overlaid in red."""
    series = report["series"]
    count = len(series["jobs"])
    width, height, left, bottom = 1000, 170, 34, 22
    plot_w, plot_h = width - left - 6, height - bottom - 8
    top = max(series["jobs"], default=0) or 1
    slot = plot_w / count
    bar = max(1.0, slot * 0.72)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{esc(plural(sum(series["jobs"]), "job"))} in the last {esc(report["range"])}">'
    ]
    for fraction in (0, 0.5, 1):
        y = 8 + plot_h * (1 - fraction)
        parts.append(f'<line class="grid" x1="{left}" x2="{width}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(
            f'<text class="axis" x="{left - 6}" y="{y + 3:.1f}" text-anchor="end">{round(top * fraction):g}</text>'
        )
    for i in range(count):
        x = left + i * slot + (slot - bar) / 2
        base = 8 + plot_h
        for name in ("exec", "transfer", "sync"):
            value = series[name][i]
            if not value:
                continue
            h = plot_h * value / top
            base -= h
            parts.append(
                f'<rect class="{name}" x="{x:.1f}" y="{base:.1f}" width="{bar:.1f}" height="{h:.1f}"/>'
            )
        failed = series["failed"][i]
        if failed:
            h = plot_h * failed / top
            parts.append(
                f'<rect class="fail" x="{x:.1f}" y="{8 + plot_h - h:.1f}" width="{max(1.0, bar / 3):.1f}" '
                f'height="{h:.1f}"/>'
            )
    start, step = report["start_ts"], report["bucket_s"]
    short = report["window_s"] <= 86400
    for i, anchor in ((0, "start"), (count // 2, "middle"), (count, "end")):
        label = time.strftime("%H:%M" if short else "%a %H:%M", time.localtime(start + i * step))
        parts.append(
            f'<text class="axis" x="{left + i * slot:.1f}" y="{height - 6}" text-anchor="{anchor}">{label}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---- data -------------------------------------------------------------------------


def gather_probes(service: Any) -> dict[str, ProbeResult | None]:
    """Cached listener status, refreshing stale entries for at most :data:`PROBE_WAIT_S`.

    A slow or unreachable listener never blocks a page: its probe keeps running in the background
    and its last known status (or ``unknown``) is shown meanwhile.
    """
    registry = service.registry
    targets = list(registry.snapshot().targets)
    ttl = getattr(registry, "probe_ttl_s", 30.0)
    now = time.time()
    stale = [t for t in targets if (cached := registry.cached(t)) is None or now - cached.checked_at >= ttl]
    if stale:
        worker = threading.Thread(
            target=registry.probe_many, args=(stale,), name="privy-hub-dashboard-probe", daemon=True
        )
        worker.start()
        worker.join(PROBE_WAIT_S)
    return {target.id: registry.cached(target) for target in targets}


def build_report(service: Any, range_name: str) -> dict[str, Any]:
    targets = list(service.registry.snapshot().targets)
    probes = gather_probes(service)
    counts = service.jobs.counts()
    return service.metrics.report(
        targets,
        probes,
        range_name=range_name,
        jobs_running=counts["running"],
        jobs_queued=counts["queued"],
        max_running=service.jobs.max_running,
    )


def _last(query: Mapping[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name)
    return values[-1] if values else default


def range_of(query: Mapping[str, list[str]]) -> str:
    value = _last(query, "range", DEFAULT_RANGE)
    return value if value in RANGES else DEFAULT_RANGE


def limit_of(query: Mapping[str, list[str]], default: int) -> int:
    try:
        return max(1, min(MAX_LIMIT, int(_last(query, "limit", str(default)))))
    except ValueError:
        return default


def link(path: str, **params: str | None) -> str:
    kept = {name: value for name, value in params.items() if value}
    return path + ("?" + urlencode(kept) if kept else "")


# ---- page shell -------------------------------------------------------------------


def shell(
    service: Any,
    page: str,
    *,
    title: str,
    main: str,
    report: dict[str, Any],
    nonce: str,
    base_url: str,
    params: dict[str, str | None],
) -> str:
    now = time.time()
    rng = report["range"]
    nav_counts = {
        "hycos": report["targets_total"],
        "logs": report["kept_jobs"],
        "jobs": report["jobs"]["total"],
    }
    nav = "".join(
        f'<a href="{esc(link(path, range=rng if rng != DEFAULT_RANGE else None))}"'
        f"{ON if name == page else ''}>{esc(label)}"
        + (f"<span>{fmt_int(nav_counts[name])}</span>" if name in nav_counts else "")
        + "</a>"
        for name, (path, label) in PAGES.items()
    )
    api = (
        '<a href="/llms.txt">Markdown guide</a>'
        f'<a href="{esc(link("/v1/stats", range=rng))}">Stats JSON</a>'
        '<a href="/openapi.json">OpenAPI</a>'
        '<a href="/v1/targets">Targets JSON</a>'
    )
    ranges = "".join(
        f'<a href="{esc(link(PAGES[page][0], **{**params, "range": name}))}"'
        f"{ON if name == rng else ''}>{name}</a>"
        for name in RANGES
    )
    jobs = report["jobs"]
    online, total = report["targets_online"], report["targets_total"]
    online_class = "ok" if total and online == total else "warn" if online else "bad"
    chips = (
        f'<span class="seg">{ranges}</span>'
        f'<span class="chip {online_class}">● {online}/{total} online</span>'
        f'<span class="chip{" warn" if jobs["running"] or jobs["queued"] else ""}">'
        f"{jobs['running']} running · {jobs['queued']} queued</span>"
    )
    app = (
        f"privy {esc(str(service.version))}<br>pid {os.getpid()}<br>{esc(base_url)}<br>"
        f"up {fmt_span(now - service.started_at)}<br>rendered {local_time(now, short=True)}"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{esc(title)} · privy hub</title>
<style nonce="{nonce}">{CSS}</style></head>
<body>
<aside class="sidebar"><div class="brand"><b>privy hub</b><small>CONTROL PLANE</small></div>
<div class="nav-label">VIEWS</div><nav id="nav">{nav}</nav>
<div class="nav-label">MACHINE-READABLE</div><nav>{api}</nav>
<div class="app" id="appinfo">{app}</div></aside>
<div class="shell">
<header class="topbar"><div class="crumbs">privy hub / <b>{esc(PAGES[page][1])}</b></div>
<div class="chips"><span class="chips" id="topchips">{chips}</span>
<label class="tg"><input type="checkbox" id="autorefresh" checked> auto-refresh 10s</label></div></header>
<main class="main" id="main">
{main}
</main></div>
<script nonce="{nonce}">{JS}</script>
</body></html>
"""


def html_response(text: str, nonce: str, status: int = 200) -> HttpResponse:
    csp = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    headers = {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
    }
    return HttpResponse(status, headers=headers, text=text, content_type=HTML_TYPE)


def hero(eyebrow: str, title: str, chips: str = "") -> str:
    return (
        f'<section class="hero"><div class="eyebrow">{esc(eyebrow)}</div><h1>{esc(title)}</h1>'
        f'<div class="chips">{chips}</div></section>'
    )


def stat(label: str, value: str, note: str = "") -> str:
    return (
        f'<div class="stat"><div class="stat-label">{esc(label)}</div><div class="stat-value">{value}</div>'
        f'<div class="stat-note">{note}</div></div>'
    )


def section(key: str, number: str, title: str, subtitle: str, body: str, *, open_: bool = True) -> str:
    return (
        f'<details class="section" id="s-{key}"{" open" if open_ else ""}><summary>'
        f'<span class="num-label">{esc(number)}</span><span class="title">{esc(title)}</span>'
        f'<span class="subtitle">{esc(subtitle)}</span><span class="caret">›</span></summary>'
        f'<div class="body">{body}</div></details>'
    )


def status_dot(status: str) -> str:
    cls = "" if status == ONLINE else "off" if status == UNKNOWN else "bad"
    return f'<span class="dot {cls}"></span>'


def state_label(state: str) -> str:
    cls = (
        "run"
        if state in RUNNING_STATES
        else "bad"
        if state in BAD_STATES
        else "off"
        if state != "succeeded"
        else ""
    )
    return f'<span class="st {cls}">{esc(state)}</span>'


def sort_th(label: str, *, right: bool = False, sort: bool = True) -> str:
    cls = ' class="r"' if right else ""
    attr = " data-sort" if sort else ""
    return f'<th{cls}{attr}>{esc(label)}<span class="s"></span></th>'


def job_anchor(entry_or_view: JobEntry | dict[str, Any]) -> str:
    if isinstance(entry_or_view, JobEntry):
        target, job_id = entry_or_view.target_id, entry_or_view.id
    else:
        target, job_id = entry_or_view["target"], entry_or_view["job_id"]
    return f"/ui/logs?target={quote(target, safe='')}#j-{quote(job_id, safe='')}"


def hyco_cell(row: dict[str, Any]) -> str:
    return (
        f'{status_dot(row["status"])}<b class="nw">{esc(row["label"])}</b>'
        f'<span class="sub">relay {esc(row["relay"])}{"" if row["configured"] else " · no longer configured"}</span>'
    )


# ---- overview -----------------------------------------------------------------------


def overview_page(service: Any, report: dict[str, Any], now: float) -> str:
    jobs = report["jobs"]
    rows = report["targets"]
    finished = jobs["succeeded"] + jobs["failed"]
    success = round(100 * jobs["succeeded"] / finished, 1) if finished else None
    since = report["history_since"]
    chips = (
        f'<span class="chip mono">privy {esc(str(service.version))}</span>'
        f'<span class="chip">last {esc(report["range"])}</span>'
        f'<span class="chip">hub up {fmt_span(now - service.started_at)}</span>'
        f'<span class="chip">{plural(report["kept_jobs"], "job")} kept in memory</span>'
    )
    if report["restored_jobs"]:
        chips += (
            f'<span class="chip">{plural(report["restored_jobs"], "job")} restored from audit.jsonl</span>'
        )
    if since:
        chips += f'<span class="chip mono">history since {esc(since)}</span>'
    calls = report["calls"]
    surfaces = " · ".join(f"{count:,} {name}" for name, count in calls["by_surface"].items() if count)
    moved = report["bytes_moved"]
    moved_total = sum(moved.values())
    stats = (
        '<div class="stats">'
        + stat(
            "Jobs",
            fmt_int(jobs["total"]),
            f"{jobs['succeeded']:,} ok · {jobs['failed']:,} failed · {jobs['cancelled']:,} cancelled",
        )
        + stat(
            "Relay HYCOs in use",
            f"{report['targets_used']}<small> / {report['targets_total']}</small>",
            f"{report['targets_online']} online now",
        )
        + stat("Success rate", esc(fmt_pct(success)), f"of {finished:,} finished jobs")
        + stat(
            "Running now",
            f"{jobs['running']}<small> / {jobs['max_running']}</small>",
            f"{jobs['queued']} queued",
        )
        + '</div><div class="stats">'
        + stat(
            "Job duration p50",
            esc(fmt_ms(jobs["duration_p50_ms"])),
            f"p95 {esc(fmt_ms(jobs['duration_p95_ms']))} · max {esc(fmt_ms(jobs['duration_max_ms']))}",
        )
        + stat(
            "Bytes moved",
            esc(fmt_bytes(moved_total)),
            " · ".join(f"{esc(fmt_bytes(value))} {name}" for name, value in moved.items() if value)
            or "no transfers",
        )
        + stat(
            "Hub API calls",
            fmt_int(calls["total"]),
            f"{esc(surfaces or 'none')} · {calls['errors']:,} errors",
        )
        + stat(
            "Relay latency p50",
            esc(fmt_ms(report["probe_latency_ms"]["p50"])),
            f"p95 {esc(fmt_ms(report['probe_latency_ms']['p95']))} · listener probes",
        )
        + "</div>"
    )
    offline = [row for row in rows if row["configured"] and row["status"] not in (ONLINE, UNKNOWN)]
    warn = ""
    if offline:
        names = ", ".join(f"{row['label']} ({row['status']})" for row in offline[:12])
        more = f" and {len(offline) - 12} more" if len(offline) > 12 else ""
        warn = (
            f'<div class="status warn"><b>{plural(len(offline), "relay HYCO")} not reachable</b>'
            f"<p>{esc(names + more)}</p></div>"
        )
    legend = (
        '<div class="legend"><span><i class="exec"></i>exec</span><span><i class="transfer"></i>upload / download</span>'
        '<span><i class="sync"></i>sync</span><span><i class="fail"></i>failed</span>'
        f"<span>{esc(fmt_span(report['bucket_s']))} per column · local time</span></div>"
    )
    activity = section(
        "activity",
        "01",
        "Activity",
        f"jobs started per {fmt_span(report['bucket_s'])}",
        f'<div class="chart">{activity_chart(report)}</div>{legend}',
    )
    busiest = [row for row in rows if row["jobs"]][:8]
    peak = busiest[0]["jobs"] if busiest else 1
    ranks = (
        "".join(
            f'<a class="rank" href="{esc(link("/ui/logs", target=row["id"]))}"><span class="i">{i:02d}</span>'
            f'<span><span class="n">{esc(row["label"])}</span>{hbar(row["jobs"] / peak, hot=bool(row["failed"]))}</span>'
            f'<span class="v">{plural(row["jobs"], "job")}'
            f"{' · ' + str(row['failed']) + ' failed' if row['failed'] else ''}</span></a>"
            for i, row in enumerate(busiest, 1)
        )
        or '<div class="empty">No jobs in this range.</div>'
    )
    top = report["top_commands"]
    top_peak = top[0]["count"] if top else 1
    commands = (
        "".join(
            f'<div class="rank"><span class="i">{i:02d}</span><span><span class="n" title="{esc(item["command"])}">'
            f"{esc(item['command'])}</span>{hbar(item['count'] / top_peak, hot=bool(item['failed']))}</span>"
            f'<span class="v">{item["count"]:,}× · {plural(len(item["targets"]), "HYCO")}</span></div>'
            for i, item in enumerate(top, 1)
        )
        or '<div class="empty">No commands in this range.</div>'
    )
    usage = section(
        "usage",
        "02",
        "Who is busy",
        "busiest relay HYCOs and most repeated commands",
        f'<div class="two"><div><h3>Busiest relay HYCOs</h3>{ranks}</div>'
        f"<div><h3>Top commands</h3>{commands}</div></div>",
    )
    failures = report["recent_failures"]
    failure_rows = "".join(
        f'<tr data-href="{esc(job_anchor(view))}"><td class="m">{esc(view["created_at"] or "")}</td>'
        f'<td>{esc(view["target"])}</td><td class="cmd">{esc(view["command"])}</td>'
        f'<td>{state_label(view["state"])}</td><td class="r">{fmt_int(view["exit_code"])}</td>'
        f"<td>{esc(view['error'] or '')}</td></tr>"
        for view in failures
    )
    failures_body = (
        '<div class="table"><table><thead><tr><th>Started (UTC)</th><th>HYCO</th><th>Command</th><th>State</th>'
        f'<th class="r">Exit</th><th>Error</th></tr></thead><tbody>{failure_rows}</tbody></table></div>'
        if failures
        else '<div class="empty">No failures in this range. 🎉</div>'
    )
    recent = section(
        "failures",
        "03",
        "Recent failures",
        "newest first · click a row for its log",
        failures_body,
        open_=bool(failures),
    )
    return (
        hero("PRIVY HUB · CONTROL PLANE", "What's running through the relays", chips)
        + stats
        + warn
        + activity
        + usage
        + recent
    )


# ---- relay HYCOs ----------------------------------------------------------------------


def hycos_page(report: dict[str, Any], now: float) -> str:
    rows = report["targets"]
    head = (
        '<tr><th class="r">#</th>'
        + sort_th("Relay HYCO")
        + sort_th("Status")
        + sort_th(f"Jobs over {report['range']}", sort=False)
        + sort_th("Jobs", right=True)
        + sort_th("Failed", right=True)
        + sort_th("Success", right=True)
        + sort_th("p50 duration", right=True)
        + sort_th("Moved", right=True)
        + sort_th("Latency", right=True)
        + sort_th("Uptime", right=True)
        + sort_th("Top origin")
        + sort_th("Last used", right=True)
        + "</tr>"
    )
    body = []
    for i, row in enumerate(rows, 1):
        origin = row["top_origin"]
        origin_text = f"{origin['origin']} ({origin['share_pct']}%)" if origin else "—"
        os_line = " · ".join(
            part for part in (row["user"] and f"{row['user']}@{row['host']}", row["os"]) if part
        )
        status_sub = (
            row["reason"] or os_line or (row["listener_version"] and f"privy {row['listener_version']}")
        )
        up = row["status"] == ONLINE
        terms = (
            row["label"],
            row["id"],
            row["status"],
            "up" if up else "down",
            row["host"],
            row["user"],
            row["os"],
            status_sub,
        )
        body.append(
            f'<tr data-href="{esc(link("/ui/logs", target=row["id"]))}" '
            f'data-q="{esc(" ".join(str(term) for term in terms if term).lower())}" '
            f'data-up="{1 if up else 0}">'
            f'<td class="idx">{i:02d}</td>'
            f'<td data-v="{esc(row["label"].lower())}">{hyco_cell(row)}</td>'
            f'<td data-v="{esc(row["status"])}">{state_label(row["status"])}'
            f'<span class="sub">{esc(status_sub or "")}</span></td>'
            f"<td>{sparkline(row['series'], row['failed_series'])}</td>"
            f'<td class="r" data-v="{row["jobs"]}">{fmt_int(row["jobs"])}</td>'
            f'<td class="r{" hot" if row["failed"] else ""}" data-v="{row["failed"]}">{fmt_int(row["failed"])}</td>'
            f'<td class="r" data-v="{row["success_pct"] if row["success_pct"] is not None else -1}">'
            f"{esc(fmt_pct(row['success_pct']))}</td>"
            f'<td class="r" data-v="{row["duration_p50_ms"] if row["duration_p50_ms"] is not None else -1}">'
            f"{esc(fmt_ms(row['duration_p50_ms']))}</td>"
            f'<td class="r" data-v="{row["bytes_moved"]}">{esc(fmt_bytes(row["bytes_moved"]))}</td>'
            f'<td class="r" data-v="{row["latency_ms"] if row["latency_ms"] is not None else -1}">'
            f"{esc(fmt_ms(row['latency_ms']))}</td>"
            f'<td class="r" data-v="{row["uptime_pct"] if row["uptime_pct"] is not None else -1}">'
            f"{esc(fmt_pct(row['uptime_pct']))}</td>"
            f"<td>{esc(origin_text)}</td>"
            f'<td class="r" data-v="{row["last_used_ts"] or 0}">{esc(fmt_ago(row["last_used_ts"], now))}</td></tr>'
        )
    toolbar = (
        '<div class="toolbar"><input type="search" id="hycoq" placeholder="search HYCOs, e.g. _windows or '
        'raki- linux (all terms must match)…" autocomplete="off"><span class="seg st"><a class="on" data-s="all">'
        'all</a><a data-s="up">up</a><a data-s="down">down</a></span><span class="chip" id="hycocount"></span></div>'
    )
    table = (
        f'{toolbar}<div class="table"><table id="hycos"><thead>{head}</thead><tbody>{"".join(body)}</tbody></table>'
        '</div><div class="empty" id="hyconone" hidden>No relay HYCOs match this search.</div>'
        if rows
        else '<div class="empty">No relays are hydrated yet. Run Sync-PrivyHubRelays.ps1 or edit relays.json.</div>'
    )
    note = (
        '<p class="fine">Most used first; click a header to re-sort and a row for its logs. Sparklines show jobs '
        f"started per {esc(fmt_span(report['bucket_s']))} (red ticks: failures). Latency and uptime come from the hub's "
        "own listener probes, not from Azure.</p>"
    )
    chips = (
        f'<span class="chip">{plural(report["targets_total"], "HYCO")}</span>'
        f'<span class="chip ok">{report["targets_online"]} online</span>'
        f'<span class="chip">{report["targets_used"]} used in the last {esc(report["range"])}</span>'
    )
    return hero("RELAY HYBRID CONNECTIONS", "Which relay HYCOs are in use", chips) + section(
        "hycos", "01", "Relay HYCOs", f"jobs over the last {report['range']}", table + note
    )


# ---- logs -------------------------------------------------------------------------------


def _prompt(entry: JobEntry, host: str, user: str) -> str:
    """The command as it would look typed into the target's shell."""
    lines = entry.command.splitlines() or [""]
    bash = f'<span class="pr">{esc(user)}@{esc(host)}</span>:<span class="pa">~</span>$ '
    if entry.type != "exec":
        return f'{bash}<span class="cm">privy {esc(entry.type)} {esc(entry.command)}</span>\n'
    if entry.language == "powershell":
        prompt, more = (
            f'<span class="pr">PS</span> <span class="pa">{esc(host)}</span>&gt; ',
            '<span class="pr">&gt;&gt;</span> ',
        )
    elif entry.language == "python":
        body = "".join(f'<span class="cm">{esc(line)}</span>\n' for line in lines)
        return f'{bash}<span class="cm">python3 - &lt;&lt;\'EOF\'</span>\n{body}<span class="cm">EOF</span>\n'
    else:
        prompt, more = bash, '<span class="pr">&gt;</span> '
    return "".join(
        f'{prompt if i == 0 else more}<span class="cm">{esc(line)}</span>\n' for i, line in enumerate(lines)
    )


def safe_terminal(entry: JobEntry, host: str | None, user: str | None, *, open_: bool, now: float) -> str:
    """A job's terminal, or an error card for it, so one bad job can't break the whole page."""
    try:
        return terminal(entry, host, user, open_=open_, now=now)
    except Exception as error:
        log.exception("Could not render the terminal for job %s", entry.id)
        log_url = f"/ui/jobs/{quote(entry.id, safe='')}/log"
        return (
            f'<details class="term bad" id="j-{esc(entry.id)}" data-job="{esc(entry.id)}" data-kind="'
            f'{"exec" if entry.type == "exec" else "files"}"><summary><span class="lights"><i></i><i></i><i></i></span>'
            f'<span class="ttl">{esc(entry.type)}</span><span class="cmdp">$ {esc(" ".join(entry.command.split())[:200])}'
            f'</span><span class="meta-r"><a href="{esc(log_url)}" title="raw log">raw</a></span></summary>'
            f'<pre class="screen"><span class="err">could not render this job: {esc(type(error).__name__)}'
            "</span>\n</pre></details>"
        )


def terminal(entry: JobEntry, host: str | None, user: str | None, *, open_: bool, now: float) -> str:
    view = entry.view()
    state = view["state"]
    running = state in RUNNING_STATES
    classes = ["term"]
    classes.append(
        "run" if running else "bad" if state in BAD_STATES else "ok" if state == "succeeded" else ""
    )
    if entry.restored:
        classes.append("old")
    kind = "exec" if entry.type == "exec" else "files"
    host = host or entry.target_id.rsplit("/", 1)[-1]
    user = user or "privy"
    pill = "run" if running else "bad" if state in BAD_STATES else "ok" if state == "succeeded" else ""
    title = f"{entry.type}{f' · {entry.language}' if entry.language else ''} · {entry.origin}"
    preview = " ".join(entry.command.split())[:200]
    screen = [_prompt(entry, host, user)]
    if entry.command_truncated:
        screen.append('<span class="ex"># (command truncated in the audit log)</span>\n')
    if entry.restored:
        screen.append(
            '<span class="ex">(output not kept — ran before the hub restarted; command restored from audit.jsonl)'
            "</span>\n"
        )
    pieces, dropped = entry.output(TERMINAL_BYTES)
    log_url = f"/ui/jobs/{quote(entry.id, safe='')}/log"
    if dropped:
        screen.append(
            f'<span class="ex">… earlier output trimmed — <a href="{esc(log_url)}">open the full log</a></span>\n'
        )
    out = "".join(
        f'<span class="err">{esc(piece.data.decode("utf-8", "replace"))}</span>'
        if piece.stream == "stderr"
        else esc(piece.data.decode("utf-8", "replace"))
        for piece in pieces
    )
    screen.append(f'<span class="out">{out}</span>')
    if pieces and not pieces[-1].data.endswith(b"\n"):
        screen.append("\n")
    if entry.type != "exec":
        sync = view.get("sync")
        progress = view.get("progress")
        if sync:
            detail = ", ".join(
                f"{name.replace('_', ' ')} {value:,}"
                if isinstance(value, int)
                else f"{name.replace('_', ' ')} {value}"
                for name, value in sync.items()
            )
        elif progress:
            detail = f"{fmt_bytes(progress.get('bytes_done'))} of {fmt_bytes(progress.get('bytes_total'))}"
        else:
            detail = fmt_bytes(view["bytes_moved"]) + " moved"
        screen.append(f'<span class="ex">⇅ {esc(detail)}</span>\n')
    if running:
        screen.append('<span class="cur"></span>')
    else:
        parts = [
            part
            for part in (
                view["exit_code"] is not None and f"exit {view['exit_code']}",
                state,
                view["duration_ms"] is not None and fmt_ms(view["duration_ms"]),
            )
            if part
        ]
        screen.append(f'<span class="ex">[{esc(" · ".join(parts))}]</span>\n')
        if view["error"]:
            screen.append(f'<span class="err">{esc(view["error"])}</span>\n')
    attrs = f' data-job="{esc(entry.id)}" data-kind="{kind}"' + (" data-live" if running else "")
    return (
        f'<details class="{" ".join(c for c in classes if c)}" id="j-{esc(entry.id)}"{attrs}'
        f"{' open' if open_ or running else ''}><summary>"
        '<span class="lights"><i></i><i></i><i></i></span>'
        f'<span class="ttl">{esc(title)}</span><span class="cmdp">$ {esc(preview)}</span>'
        f'<span class="meta-r"><span class="pill {pill}">{esc(state)}</span>'
        f"<span>{esc(fmt_ms(view['duration_ms']))}</span>"
        f'<span title="{esc(local_time(entry.created_at))}">{esc(fmt_ago(entry.created_at, now))}</span>'
        f'<a href="{esc(log_url)}" title="raw log">raw</a></span></summary>'
        f'<pre class="screen">{"".join(screen)}</pre></details>'
    )


def logs_page(
    service: Any, report: dict[str, Any], query: Mapping[str, list[str]], now: float
) -> tuple[str, str]:
    rows = report["targets"]
    by_id = {row["id"]: row for row in rows}
    every = service.metrics.entries()
    kept: dict[str, int] = {}
    for entry in every:
        kept[entry.target_id] = kept.get(entry.target_id, 0) + 1
    selected = _last(query, "target")
    if selected not in by_id and selected != "*":
        used = sorted((row for row in rows if row["last_used_ts"]), key=lambda row: -row["last_used_ts"])
        selected = used[0]["id"] if used else (rows[0]["id"] if rows else "*")
    limit = limit_of(query, DEFAULT_LOG_LIMIT)
    pods = [
        f'<a href="{esc(link("/ui/logs", target="*"))}"{ON if selected == "*" else ""}>'
        f"<b>All relay HYCOs</b><span>{len(every):,}</span><small>every job, newest first</small></a>"
    ]
    for row in sorted(rows, key=lambda row: (-(row["last_used_ts"] or 0), row["label"].lower())):
        who = row["user"] and row["host"] and f"{row['user']}@{row['host']}"
        pods.append(
            f'<a href="{esc(link("/ui/logs", target=row["id"]))}"{ON if row["id"] == selected else ""}>'
            f"<b>{status_dot(row['status'])}{esc(row['label'])}</b><span>{kept.get(row['id'], 0):,}</span>"
            f"<small>{esc(who or row['status'])} · {esc(fmt_ago(row['last_used_ts'], now))}</small></a>"
        )
    entries = every if selected == "*" else [entry for entry in every if entry.target_id == selected]
    entries = sorted(entries, key=lambda entry: entry.created_at, reverse=True)
    shown = entries[:limit]
    terms = "".join(
        safe_terminal(
            entry,
            (by_id.get(entry.target_id) or {}).get("host"),
            (by_id.get(entry.target_id) or {}).get("user"),
            open_=i < OPEN_NEWEST,
            now=now,
        )
        for i, entry in enumerate(shown)
    )
    if not shown:
        terms = (
            '<div class="empty">No jobs have run here since the hub started (and none in audit.jsonl).</div>'
        )
    elif len(entries) > limit:
        terms += (
            f'<a class="more" href="{esc(link("/ui/logs", target=selected, limit=str(min(MAX_LIMIT, limit * 4))))}">'
            f"Show more ({len(entries) - limit:,} older jobs)</a>"
        )
    toolbar = (
        '<div class="toolbar"><input type="search" id="grep" placeholder="grep commands and output…" '
        'autocomplete="off"><span class="seg kind"><a class="on" data-k="all">all</a><a data-k="exec">exec</a>'
        '<a data-k="files">files</a><a data-k="failed">failed</a></span>'
        '<label class="tg"><input type="checkbox" id="expand"> expand all</label>'
        '<label class="tg"><input type="checkbox" id="follow" checked> follow running</label></div>'
    )
    row = by_id.get(selected)
    if row is None:
        heading, chips = "All relay HYCOs", f'<span class="chip">{plural(len(entries), "job")}</span>'
    else:
        heading = row["label"]
        chips = (
            f'<span class="chip mono">{esc(row["id"])}</span>{_status_chip(row)}'
            f'<span class="chip">{plural(len(entries), "job")} kept</span>'
        )
        if row["os"]:
            chips += f'<span class="chip">{esc(row["os"])}</span>'
    main = (
        hero("LOGS · LIKE kubectl logs, PER RELAY HYCO", heading, chips)
        + '<div class="logs"><div class="pods"><div class="hd">RELAY HYCOs'
        + '<input type="search" id="podq" placeholder="filter…" autocomplete="off"></div>'
        + "".join(pods)
        + f"</div><div>{toolbar}{terms}"
        + '<p class="fine">Commands and output live in the hub\'s memory until it restarts (output is capped per job; '
        "older output is trimmed). Jobs from before a restart are rebuilt from audit.jsonl without their output.</p>"
        + "</div></div>"
    )
    return heading, main


def _status_chip(row: dict[str, Any]) -> str:
    status = row["status"]
    cls = "ok" if status == ONLINE else "" if status == UNKNOWN else "bad"
    return f'<span class="chip {cls}">● {esc(status)}</span>'


# ---- jobs -------------------------------------------------------------------------------


def jobs_page(service: Any, report: dict[str, Any], query: Mapping[str, list[str]], now: float) -> str:
    wanted = _last(query, "state").strip().lower()
    entries = sorted(service.metrics.entries(), key=lambda entry: entry.created_at, reverse=True)
    views = [entry.view() for entry in entries]
    if wanted == "failed":
        views = [view for view in views if view["state"] in BAD_STATES]
    elif wanted:
        views = [view for view in views if view["state"] == wanted]
    limit = limit_of(query, DEFAULT_JOB_LIMIT)
    names = {row["id"]: row["label"] for row in report["targets"]}
    head = (
        "<tr>"
        + sort_th("Started", right=True)
        + sort_th("Relay HYCO")
        + sort_th("Type")
        + sort_th("Command")
        + sort_th("Origin")
        + sort_th("State")
        + sort_th("Exit", right=True)
        + sort_th("Duration", right=True)
        + sort_th("Moved", right=True)
        + "</tr>"
    )
    body = "".join(
        f'<tr data-href="{esc(job_anchor(view))}">'
        f'<td class="r" data-v="{view["created_at"]}" title="{esc(local_time(view["created_at"]))}">'
        f"{esc(fmt_ago(view['created_at'], now))}</td>"
        f'<td data-v="{esc(view["target"].lower())}"><b>{esc(names.get(view["target"], view["target"]))}</b>'
        f'<span class="sub">{esc(view["target"])}</span></td>'
        f"<td>{esc(view['type'])}{' · ' + esc(view['language']) if view['language'] else ''}</td>"
        f'<td class="cmd" title="{esc(view["command"][:2000])}">{esc(" ".join(view["command"].split())[:300])}</td>'
        f'<td>{esc(view["origin"])}</td><td data-v="{esc(view["state"])}">{state_label(view["state"])}</td>'
        f'<td class="r" data-v="{view["exit_code"] if view["exit_code"] is not None else -1}">'
        f"{fmt_int(view['exit_code'])}</td>"
        f'<td class="r" data-v="{view["duration_ms"] if view["duration_ms"] is not None else -1}">'
        f"{esc(fmt_ms(view['duration_ms']))}</td>"
        f'<td class="r" data-v="{view["bytes_moved"]}">{esc(fmt_bytes(view["bytes_moved"]) if view["bytes_moved"] else "—")}</td>'
        "</tr>"
        for view in views[:limit]
    )
    filters = "".join(
        f'<a href="{esc(link("/ui/jobs", state=name or None))}"{ON if wanted == name else ""}>{label}</a>'
        for name, label in (("", "all"), ("running", "running"), ("failed", "failed"), ("succeeded", "ok"))
    )
    table = (
        f'<div class="table"><table id="jobs"><thead>{head}</thead><tbody>{body}</tbody></table></div>'
        if views
        else '<div class="empty">No jobs match.</div>'
    )
    if len(views) > limit:
        table += (
            f'<a class="more" href="{esc(link("/ui/jobs", state=wanted or None, limit=str(min(MAX_LIMIT, limit * 4))))}">'
            f"Show more ({len(views) - limit:,} older jobs)</a>"
        )
    chips = f'<span class="seg">{filters}</span><span class="chip">{plural(len(views), "job")}</span>'
    return hero("EVERY JOB THE HUB REMEMBERS", "Jobs", chips) + section(
        "jobs", "01", "Jobs", "newest first · click a row for its terminal", table
    )


# ---- entry points -----------------------------------------------------------------------


def render(service: Any, page: str, query: Mapping[str, list[str]], base_url: str) -> HttpResponse:
    """One dashboard page (``overview``, ``hycos``, ``logs``, or ``jobs``) as an HTML response."""
    rng = range_of(query)
    report = build_report(service, rng)
    now = time.time()
    nonce = secrets.token_urlsafe(18)
    params: dict[str, str | None] = {}
    if page == "overview":
        title, main = "Overview", overview_page(service, report, now)
    elif page == "hycos":
        title, main = "Relay HYCOs", hycos_page(report, now)
    elif page == "logs":
        target, main = logs_page(service, report, query, now)
        title = f"Logs · {target}"
        params = {"target": _last(query, "target") or None, "limit": _last(query, "limit") or None}
    elif page == "jobs":
        title, main = "Jobs", jobs_page(service, report, query, now)
        params = {"state": _last(query, "state") or None, "limit": _last(query, "limit") or None}
    else:
        return error_response(404, "not_found", f"no dashboard page {page!r}")
    document = shell(
        service, page, title=title, main=main, report=report, nonce=nonce, base_url=base_url, params=params
    )
    return html_response(document, nonce)


def raw_log(service: Any, job_id: str) -> HttpResponse:
    """A job's command and every byte of output the hub still holds, as plain text."""
    entry = service.metrics.entry(job_id)
    if entry is None:
        return error_response(404, "job_not_found", f"the hub has no record of job {job_id!r}")
    view = entry.view()
    pieces, dropped = entry.output(1 << 62)
    lines = [
        f"# job {entry.id} on {entry.target_id} ({entry.type}"
        + (f", {entry.language}" if entry.language else "")
        + f", {view['state']}, origin {entry.origin}, started {local_time(entry.created_at)})",
        f"$ {entry.command}",
        "",
    ]
    if entry.restored:
        lines.append("(output not kept: the job ran before the hub restarted)")
    if dropped:
        lines.append("[privy] earlier output is no longer kept by the hub")
    text = "\n".join(lines) + "\n" + b"".join(piece.data for piece in pieces).decode("utf-8", "replace")
    trailer = [f"[{view['state']}"]
    if view["exit_code"] is not None:
        trailer.append(f" exit={view['exit_code']}")
    if view["duration_ms"] is not None:
        trailer.append(f" duration={fmt_ms(view['duration_ms'])}")
    trailer.append("]")
    if view["error"]:
        trailer.append(f" {view['error']}")
    if not text.endswith("\n"):
        text += "\n"
    text += "".join(trailer) + "\n"
    headers = {"X-Content-Type-Options": "nosniff"}
    return HttpResponse(200, headers=headers, text=text, content_type=TEXT_TYPE)
