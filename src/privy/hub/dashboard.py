"""The hub's control-plane website: server-rendered HTML over :class:`~privy.hub.metrics.HubMetrics`.

Every page is one self-contained document (inline CSS and JS under a per-response CSP nonce, no
external requests), readable without JavaScript; the script only adds sorting, filtering,
auto-refresh, and live log following.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

import yaml

from privy.hub.dashboard_assets import CSS, JS
from privy.hub.leases import LeaseError
from privy.hub.listener_facts import FACETS, NO_SHELL, UNKNOWN_VALUE, facet_aliases, facet_values
from privy.hub.metrics import (
    BAD_STATES,
    DEFAULT_RANGE,
    RANGES,
    REPORT_RANGES,
    RESOLUTIONS,
    JobEntry,
    ReportSelection,
)
from privy.hub.registry import ONLINE, UNKNOWN, ProbeResult
from privy.hub.remote_exec import EXEC_CSS, EXEC_JS, TERMINAL_ICON, workspace
from privy.hub.responses import HTML_TYPE, TEXT_TYPE, HttpResponse, error_response
from privy.hub.terminals import terminal_languages

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
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect x="2" y="2" width="60" height="60" rx="14" fill="#132c31"/>
<text x="32" y="47" fill="#67c7b7" font-family="Arial,sans-serif" font-size="46" font-weight="700"
text-anchor="middle">P</text>
</svg>"""

PAGES = {
    "overview": ("/ui", "Overview"),
    "hycos": ("/ui/hycos", "Relay HYCOs"),
    "leases": ("/ui/leases", "Leases"),
    "exec": ("/ui/exec", "Remote Exec"),
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


def utc_time(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(at))


def title_attr(text: str | None) -> str:
    return f' title="{esc(text)}"' if text else ""


def fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:g}%"


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


# ---- SVG ------------------------------------------------------------------------


def nav_icon(name: str) -> str:
    if name == "exec":
        return TERMINAL_ICON
    shapes = {
        "overview": '<rect x="3" y="3" width="7" height="7" rx="2"/>'
        '<rect x="14" y="3" width="7" height="7" rx="2"/>'
        '<rect x="3" y="14" width="7" height="7" rx="2"/>'
        '<rect x="14" y="14" width="7" height="7" rx="2"/>',
        "hycos": '<rect x="8" y="3" width="8" height="5" rx="1.5"/>'
        '<rect x="2" y="16" width="8" height="5" rx="1.5"/>'
        '<rect x="14" y="16" width="8" height="5" rx="1.5"/><path d="M12 8v4H6v4m6-4h6v4"/>',
        "leases": '<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 8.7-8.7M16 7l2.5 2.5M18.5 4.5 21 7"/>',
        "logs": '<path d="M7 3h8l4 4v14H5V5a2 2 0 0 1 2-2Z"/><path d="M14 3v5h5M8 12h8m-8 4h6"/>',
        "jobs": '<rect x="3" y="7" width="18" height="14" rx="3"/>'
        '<path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12h18m-11 0v3h4v-3"/>',
        "guide": '<path d="M12 5v15M3 4c4-1 6 0 9 2 3-2 5-3 9-2v15c-4-1-6 0-9 2-3-2-5-3-9-2Z"/>',
        "stats": '<path d="M4 3v17h17M8 14l4-5 4 3 5-7"/>',
        "openapi": '<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18"/>',
        "targets": '<rect x="3" y="3" width="18" height="7" rx="2"/>'
        '<rect x="3" y="14" width="18" height="7" rx="2"/>'
        '<path d="M7 6.5h.01M7 17.5h.01M11 6.5h6m-6 11h6"/>',
    }
    return (
        '<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true" focusable="false">{shapes[name]}</svg>'
    )


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
        f'aria-label="{esc(plural(sum(series["jobs"]), "job"))} over {esc(report["range_label"])}">'
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
    start = report["start_ts"]
    short = report["window_s"] <= 86400
    for fraction, anchor in ((0, "start"), (0.5, "middle"), (1, "end")):
        moment = start + fraction * (report["end_ts"] - start)
        label = time.strftime("%H:%M" if short else "%Y-%m-%d %H:%M", time.localtime(moment))
        parts.append(
            f'<text class="axis" x="{left + fraction * plot_w:.1f}" y="{height - 6}" text-anchor="{anchor}">{label}</text>'
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


def build_report(
    service: Any, range_name: str = DEFAULT_RANGE, *, selection: ReportSelection | None = None
) -> dict[str, Any]:
    targets = list(service.registry.snapshot().targets)
    probes = gather_probes(service)
    counts = service.jobs.counts()
    report = service.metrics.report(
        targets,
        probes,
        range_name=range_name,
        selection=selection,
        jobs_running=counts["running"],
        jobs_queued=counts["queued"],
        max_running=service.jobs.max_running,
    )
    for row in report["targets"]:
        probe = probes.get(row["id"])
        row["terminal_shells"] = terminal_languages(probe.info if probe else None)
        row.update(service.listener_facts(row["id"], row["hybrid_connection"], probe))
    return report


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
    if getattr(service, "leases", None) is not None:
        nav_counts["leases"] = len(service.leases.list_leases())
    nav = "".join(
        f'<a href="{esc(link(path, range=rng if rng in ("1h", "7d") else None))}"'
        f"{ON if name == page else ''}>{nav_icon(name)}{esc(label)}"
        + (f"<span>{fmt_int(nav_counts[name])}</span>" if name in nav_counts else "")
        + "</a>"
        for name, (path, label) in PAGES.items()
    )
    api = "".join(
        f'<a href="{esc(path)}">{nav_icon(name)}{esc(label)}</a>'
        for name, path, label in (
            ("guide", "/llms.txt", "Markdown guide"),
            (
                "stats",
                link("/v1/stats", **params) if page == "overview" else link("/v1/stats", range=rng),
                "Stats JSON",
            ),
            ("openapi", "/openapi.json", "OpenAPI"),
            ("targets", "/v1/targets", "Targets JSON"),
        )
    )
    ranges = "".join(
        f'<a href="{esc(link(PAGES[page][0], **{**{k: v for k, v in params.items() if k not in ("start", "end")}, "range": name}))}"'
        f"{ON if name == rng else ''}>{name}</a>"
        for name in ("1h", "24h", "7d")
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
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style nonce="{nonce}">{CSS}{EXEC_CSS}</style></head>
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
<script nonce="{nonce}">{JS}{EXEC_JS if page == "exec" else ""}</script>
</body></html>
"""


def html_response(text: str, nonce: str, status: int = 200) -> HttpResponse:
    csp = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    headers = {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
    }
    return HttpResponse(status, headers=headers, text=text, content_type=HTML_TYPE)


def favicon_response() -> HttpResponse:
    return HttpResponse(
        200,
        headers={"X-Content-Type-Options": "nosniff"},
        text=FAVICON_SVG,
        content_type="image/svg+xml",
    )


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


def sort_th(
    label: str, *, right: bool = False, sort: bool = True, cls: str = "", hidden: bool = False
) -> str:
    classes = " ".join(part for part in ("r" if right else "", cls) if part)
    class_attr = f' class="{classes}"' if classes else ""
    attr = (" data-sort" if sort else "") + (" hidden" if hidden else "")
    return f'<th{class_attr}{attr}>{esc(label)}<span class="s"></span></th>'


def job_anchor(entry_or_view: JobEntry | dict[str, Any]) -> str:
    if isinstance(entry_or_view, JobEntry):
        target, job_id = entry_or_view.target_id, entry_or_view.id
    else:
        target, job_id = entry_or_view["target"], entry_or_view["job_id"]
    return f"/ui/logs?target={quote(target, safe='')}#j-{quote(job_id, safe='')}"


def host_line(row: dict[str, Any]) -> str:
    who = "@".join(part for part in (row["user"], row["host"]) if part)
    version = row["listener_version"] and f"privy {row['listener_version']}"
    return " · ".join(part for part in (who, row["os"], version) if part)


def hyco_cell(row: dict[str, Any]) -> str:
    host = host_line(row)
    return (
        f'<b class="nw">{esc(row["label"])}</b>'
        + (f'<span class="sub clip" title="{esc(host)}">{esc(host)}</span>' if host else "")
        + f'<span class="sub">relay {esc(row["relay"])}{"" if row["configured"] else " · no longer configured"}</span>'
    )


def hyco_status_cell(row: dict[str, Any], now: float) -> str:
    status, since = row["status"], row["status_since_ts"]
    parts = []
    if since is not None:
        at_least = "at least " if row["status_since_first_check"] else ""
        parts.append(f"for {at_least}{fmt_span(max(0.0, now - since))}")
    if row["reason"]:
        parts.append(row["reason"])
    detail = " · ".join(parts)
    title = " · ".join(parts + ([f"since {utc_time(since)}"] if since is not None else []))
    return (
        f'<td data-v="{esc(status)}"{title_attr(title)}>'
        f'<span class="hyco-status {esc(status)}">{esc(status.replace("_", " "))}</span>'
        + (f'<span class="sub clip">{esc(detail)}</span>' if detail else "")
        + "</td>"
    )


def last_online_cell(row: dict[str, Any], now: float) -> str:
    seen = row["last_online_ts"]
    title = title_attr(f"last confirmed online {utc_time(seen)}" if seen is not None else None)
    if row["status"] == ONLINE:
        return f'<td class="nw" data-v="{now}"{title}>now</td>'
    if seen is not None:
        return f'<td class="nw" data-v="{seen}"{title}>{esc(fmt_ago(seen, now))}</td>'
    if row["status"] == UNKNOWN:
        return '<td data-v="0">—</td>'
    return '<td class="nw faint" data-v="0">not since hub start</td>'


#: Optional ``/ui/hycos`` columns as ``(key, header, shown by default)``; the Columns picker toggles them.
FACT_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("os", "OS", True),
    ("environment", "Env", True),
    ("arch", "Arch", False),
    ("privy", "Privy", True),
    ("python", "Python", True),
    ("shell", "Shell", False),
)


def version_key(text: str) -> str:
    """A sort key that orders ``0.10.0`` after ``0.9.0``."""
    return re.sub(r"\d+", lambda match: match.group().zfill(8), text.lower())


def fact_cells(row: dict[str, Any]) -> str:
    stale = row.get("info_stale")
    seen = row.get("info_seen_at_ts")
    as_of = f"as of {utc_time(seen)}, the last report while online" if stale and seen is not None else None
    shells = row.get("shells")
    values = {
        "os": row.get("os_version") or row.get("os_family"),
        "environment": row.get("environment"),
        "arch": row.get("arch"),
        "privy": row.get("privy_version"),
        "python": row.get("python_version"),
        "shell": ", ".join(shells) if shells else NO_SHELL if shells is not None else None,
    }
    cells = []
    for key, _, shown in FACT_COLUMNS:
        value = values[key]
        hidden = "" if shown else " hidden"
        title = as_of
        if key == "environment" and value:
            title = "inferred from the HYCO name suffix"
        elif key == "os" and value:
            title = " · ".join(part for part in (value, as_of) if part)
        classes = " ".join(
            part
            for part in (f"col-{key}", "nw", "fact-clip" if key == "os" else "", "stale" if stale else "")
            if part
        )
        if value is None:
            cells.append(f'<td class="col-{key} faint" data-v=""{hidden}>—</td>')
        else:
            cells.append(
                f'<td class="{classes}" data-v="{esc(version_key(value))}"{title_attr(title)}{hidden}>'
                f"{esc(value)}</td>"
            )
    return "".join(cells)


def fact_headers() -> str:
    return "".join(sort_th(header, cls=f"col-{key}", hidden=not shown) for key, header, shown in FACT_COLUMNS)


def facet_toolbar() -> str:
    pills = "".join(
        f'<button type="button" class="facet-pill" data-facet="{esc(name)}" data-label="{esc(label)}" '
        f'aria-haspopup="dialog" aria-expanded="false">{esc(label)}<span class="v"></span></button>'
        for name, label in FACETS
    )
    columns = "".join(
        f'<span data-col="{esc(key)}" data-label="{esc(header)}" data-default="{1 if shown else 0}"></span>'
        for key, header, shown in FACT_COLUMNS
    )
    return (
        f'<div class="toolbar facets"><span class="facet-title">Filter</span>{pills}'
        f'<button type="button" class="facet-pill cols" id="hycocols" aria-haspopup="dialog" '
        f'aria-expanded="false">Columns</button><span id="hycocolumns" hidden>{columns}</span></div>'
        '<div class="facet-chips" id="hycochips" hidden></div>'
    )


# ---- overview -----------------------------------------------------------------------


def overview_controls(report: dict[str, Any]) -> str:
    ranges = "".join(
        f'<option value="{name}"{" selected" if name == report["range"] else ""}>'
        f"{esc({'all': 'All retained history', 'custom': 'Custom dates'}.get(name, name))}</option>"
        for name in REPORT_RANGES
    )
    resolutions = "".join(
        f'<option value="{name}"{" selected" if name == report["requested_resolution"] else ""}>'
        f"{esc('Auto' if name == 'auto' else name)}</option>"
        for name in RESOLUTIONS
    )
    custom = report["range"] == "custom"
    return (
        '<section class="metrics-controls"><form id="metrics-controls" class="toolbar">'
        f'<label>History range<select id="metrics-range">{ranges}</select></label>'
        f'<label>Resolution<select id="metrics-resolution">{resolutions}</select></label>'
        f'<div id="metrics-custom" class="report-dates"{"" if custom else " hidden"}>'
        f'<label>Start (local time)<input type="datetime-local" id="metrics-start" '
        f'data-timestamp="{report["start_ts"]}"></label>'
        f'<label>End (local time)<input type="datetime-local" id="metrics-end" '
        f'data-timestamp="{report["end_ts"]}"></label></div>'
        '<button type="submit" class="lease-button">Apply</button>'
        '<span id="metrics-error" class="lease-action-error" role="alert"></span></form>'
        f'<p id="metrics-effective" class="fine">{"Adjusted to " if report["resolution_adjusted"] else "Showing "}'
        f"{esc(fmt_span(report['bucket_s']))} per column"
        f"{' to keep the chart within 1,000 columns' if report['resolution_adjusted'] else ''}. "
        f"{esc(report['start'])} to {esc(report['end'])} (UTC).</p>"
        '<p class="fine">Telemetry is kept in RAM without age-based expiry; memory usage grows until restart. '
        f"Request and probe history resets on restart; collecting since {esc(report['telemetry_started_at'])}. "
        "Existing audit-restored job summaries are separate. Online status and running jobs are live.</p></section>"
    )


def overview_page(service: Any, report: dict[str, Any], now: float) -> str:
    jobs = report["jobs"]
    rows = report["targets"]
    finished = jobs["succeeded"] + jobs["failed"]
    success = round(100 * jobs["succeeded"] / finished, 1) if finished else None
    since = report["history_since"]
    chips = (
        f'<span class="chip mono">privy {esc(str(service.version))}</span>'
        f'<span class="chip">{esc(report["range_label"])}</span>'
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
        + overview_controls(report)
        + stats
        + warn
        + activity
        + usage
        + recent
    )


# ---- relay HYCOs ----------------------------------------------------------------------


def purpose_cell(row: dict[str, Any]) -> str:
    purpose = row.get("purpose") or {}
    if not purpose.get("latest"):
        return ""
    target = esc(row["id"])
    start, end = purpose["start"], purpose["end"]
    span = max(end - start, 1)
    segments = []
    for segment in purpose["segments"]:
        x = max(0, min(299, 300 * (segment["start"] - start) / span))
        width = min(300 - x, max(1, 300 * (segment["end"] - segment["start"]) / span))
        kind = segment["kind"]
        color = f"pc{segment['color']}" if kind == "purpose" else f"purpose-{kind}"
        label = segment.get("label") or ("?" if kind == "unknown" else "+")
        title = segment["purpose"] or (
            "Unknown: no purpose supplied" if kind == "unknown" else "Multiple purpose changes"
        )
        text = f'<text x="{x + width / 2:.2f}" y="12">{esc(label)}</text>' if width >= 28 else ""
        segments.append(
            f'<g class="purpose-segment {color}" role="button" tabindex="0" data-kind="{kind}" '
            f'data-purpose-target="{target}" data-purpose-segment="{esc(json.dumps(segment))}" '
            f'aria-label="{esc(title)}" aria-haspopup="dialog">'
            f"<title>{esc(title)}</title>"
            f'<rect x="{x:.2f}" y="1" width="{width:.2f}" height="16"/>{text}</g>'
        )
    latest = time.strftime("%b %d %H:%M UTC", time.gmtime(purpose["latest_at"]))
    left = time.strftime("%Y-%m-%d %H:%M", time.gmtime(start))
    right = time.strftime("%m-%d %H:%M UTC", time.gmtime(end))
    unknown = (
        '<span class="purpose-unknown-note">Latest use: unknown</span>' if purpose["latest_unknown"] else ""
    )
    return (
        f'<button type="button" class="purpose-trigger" data-purpose-target="{target}" aria-haspopup="dialog">'
        f'<span class="purpose-sentence">{esc(purpose["latest"])}</span>'
        f'<span class="purpose-meta">{latest} · {purpose["groups"]} groups</span></button>'
        f"{unknown}"
        '<svg class="purpose-bar" viewBox="0 0 300 18" width="300" height="18" '
        f'role="group" aria-label="Reported purpose over time for {target}">{"".join(segments)}</svg>'
        f'<span class="purpose-axis"><span>{left}</span><span>{right}</span></span>'
    )


def lease_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def lease_at(value: Any) -> str:
    at = lease_time(value)
    return esc(utc_time(at)) if at is not None else "—"


def lease_labels(labels: Mapping[str, Any]) -> str:
    return "".join(
        f'<span class="lease-tag">{esc(str(key))}={esc(str(value))}</span>' for key, value in labels.items()
    )


def lease_state(state: str) -> str:
    return f'<span class="lease-state {esc(state)}">{esc("cooling down" if state == "cooling" else state)}</span>'


def lease_timer(lease: dict[str, Any], now: float) -> str:
    running = lease.get("running_jobs") or []
    ttl = str(lease.get("ttl") or f"{lease.get('ttl_s', 0)}s")
    if lease.get("state", "active") != "active":
        return f'<span class="sub">expires {lease_at(lease.get("expires_at"))}</span>'
    if running:
        return (
            f'{hbar(1)}<span class="sub">pinned: {plural(len(running), "job")} running · '
            f"expires {esc(ttl)} after the last job ends</span>"
        )
    expiry = lease_time(lease.get("expires_at"))
    remaining = max(0, expiry - now) if expiry is not None else 0
    ttl_s = lease.get("ttl_s") or remaining or 1
    return (
        f'{hbar(remaining / ttl_s)}<span class="sub">expires {lease_at(lease.get("expires_at"))}'
        f" (in {esc(fmt_span(remaining))}) · idle TTL {esc(ttl)}, renews on use</span>"
    )


def lease_cell(group: dict[str, Any], now: float) -> str:
    state = group["state"]
    lease = group.get("lease")
    name = esc(str(group["group"]))
    text = lease_state(state)
    if lease:
        holder = lease.get("holder") or {}
        peek = link("/ui/leases", state="all", peek=lease["lease_id"]) + "#s-lease-peek"
        text += (
            f'<a class="lease-holder" href="{esc(peek)}">{esc(str(holder.get("name") or lease.get("client_id") or "Unknown holder"))}</a>'
            f'<span class="sub">{esc(str(holder.get("kind") or ""))} · lease {esc(lease["lease_id"])} · group {name}</span>'
            f"{lease_labels(lease.get('labels') or {})}{lease_timer(lease, now)}"
        )
    else:
        text += f'<span class="sub">group {name} · {plural(len(group.get("members") or {}), "member")}</span>'
    if state == "cooling":
        text += (
            f'<span class="sub">free at {lease_at(group.get("leasable_at"))}</span>'
            f'<span class="sub">last used {lease_at(group.get("last_used_at"))} · '
            f"{esc(str(group.get('last_used_by') or 'unknown origin'))}</span>"
        )
    elif state == "unavailable":
        text += (
            f'<span class="sub">required online: {esc(", ".join(group.get("require_online") or []))}</span>'
        )
    return text


def enrich_lease_report(service: Any, report: dict[str, Any]) -> None:
    store = getattr(service, "leases", None)
    groups = store.groups() if store is not None else []
    by_target = {target: group for group in groups for target in group["members"].values()}
    for row in report["targets"]:
        row["lease_group"] = by_target.get(row["id"])


def lease_entries(service: Any, lease_id: str) -> list[JobEntry]:
    entries = service.metrics.entries()
    if not lease_id:
        return entries
    return [entry for entry in entries if entry.view().get("lease_id") == lease_id]


def leases_page(service: Any, query: Mapping[str, list[str]], now: float) -> str:
    store = getattr(service, "leases", None)
    if store is None:
        return (
            hero("LEASES", "Who is holding which devbox")
            + '<div class="empty">Leasing is not configured.</div>'
        )
    state = _last(query, "state", "active")
    if state not in ("active", "expired", "released", "all"):
        state = "active"
    all_leases = store.list_leases(state="all")
    leases = [lease for lease in all_leases if state == "all" or lease["state"] == state]
    by_lease = {lease["lease_id"]: lease for lease in all_leases}
    groups = store.groups()
    clients = store.list_clients(active=False)
    job_counts: dict[str, int] = {}
    for entry in service.metrics.entries():
        lease_id = entry.view().get("lease_id")
        if lease_id:
            job_counts[lease_id] = job_counts.get(lease_id, 0) + 1
    chips = (
        f'<span class="chip">{plural(len(store.list_leases()), "active lease")}</span>'
        f'<span class="chip">{sum(bool(client.get("leases")) for client in clients)} clients holding</span>'
        f'<span class="chip ok">{sum(group["state"] == "free" for group in groups)} free groups</span>'
        f'<span class="chip">{sum(group["state"] == "cooling" for group in groups)} cooling down</span>'
        '<span class="chip warn">enforcement: advisory · commands without a lease still run</span>'
    )
    filters = "".join(
        f'<a href="{esc(link("/ui/leases", state=value, q=_last(query, "q"), label=_last(query, "label")))}"'
        f"{ON if state == value else ''}>{value}</a>"
        for value in ("active", "expired", "released", "all")
    )
    toolbar = (
        '<div class="toolbar">'
        f'<button type="button" class="lease-button primary" id="take-lease" '
        f'data-default-ttl="{store.default_ttl:g}" data-max-ttl="{store.manual_max_ttl:g}">Take lease</button>'
        '<input type="search" id="leaseq" aria-label="Search leases, clients and groups" '
        f'value="{esc(_last(query, "q"))}" placeholder="filter by client, group, session or purpose…" autocomplete="off">'
        f'<input type="search" id="leaselabel" aria-label="Filter lease labels" value="{esc(_last(query, "label"))}" '
        'placeholder="labels: repo=owner/repo task=leasing" autocomplete="off">'
        f'<span class="seg">{filters}</span><span class="chip" id="leasecount"></span></div>'
        '<div class="toolbar">'
        '<button type="button" class="lease-button" id="select-never-used">Select never used</button>'
        '<button type="button" class="lease-button" id="select-idle">Select idle &gt;</button>'
        '<select id="lease-idle-threshold" aria-label="Idle threshold">'
        '<option value="1800">30m</option><option value="3600">1h</option><option value="14400">4h</option></select>'
        '<button type="button" class="lease-button danger" id="force-release-selected" disabled>'
        "Force release selected (0)</button></div>"
    )
    body = []
    for lease in leases:
        lease_id = lease["lease_id"]
        holder = lease.get("holder") or {}
        search = json.dumps(lease, ensure_ascii=False).lower()
        jobs = job_counts.get(lease_id, 0)
        last_used = lease_time(lease.get("last_used_at"))
        acquired = lease_time(lease.get("acquired_at"))
        running_jobs = lease.get("running_jobs")
        bulk_safe = isinstance(running_jobs, list) and not running_jobs
        selection = (
            f'<input type="checkbox" data-select-lease="{esc(lease_id)}" aria-label="Select {esc(lease_id)}">'
            if lease["state"] == "active"
            else ""
        )
        actions = (
            f'<a class="lease-button" href="{esc(link("/ui/leases", state=state, peek=lease_id))}#s-lease-peek">Peek</a>'
            f'<a class="lease-button" href="{esc(link("/ui/logs", target="*", lease=lease_id))}">Peek logs</a>'
            f'<a class="lease-button" href="{esc(link("/ui/jobs", lease=lease_id))}">Jobs</a>'
        )
        if lease["state"] == "active":
            if lease.get("manual"):
                actions += (
                    f'<button class="lease-button" type="button" data-owner-release="{esc(lease_id)}" '
                    f'data-client="{esc(lease["client_id"])}" hidden>Release</button>'
                )
            actions += f'<button class="lease-button danger" type="button" data-force-release="{esc(lease_id)}">Force release</button>'
        members = "".join(
            f'<span class="lease-tag" title="{esc(target)}">{esc(layer)}</span>'
            for layer, target in lease.get("members", {}).items()
        )
        body.append(
            f'<tr data-lease-row data-lease-id="{esc(lease_id)}" data-bulk-safe="{str(bulk_safe).lower()}" data-last-used="{last_used or 0}" data-use-known="{str(lease.get("last_use_known", False)).lower()}" data-acquired="{acquired or 0}" '
            f'data-q="{esc(search)}" data-labels="{esc(json.dumps(lease.get("labels") or {}))}">'
            f"<td>{selection}</td>"
            f'<td><b>{esc(lease["group"])}</b><span class="sub">{esc(lease_id)}</span>{lease_state(lease["state"])}</td>'
            f"<td>{members}</td><td><b>{esc(str(holder.get('name') or lease.get('client_id') or '—'))}</b>"
            f'<span class="sub">{esc(str(holder.get("kind") or ""))} · {esc(str(holder.get("session_id") or ""))}</span></td>'
            f"<td>{lease_labels(lease.get('labels') or {})}</td><td>{esc(lease.get('purpose') or '')}</td>"
            f'<td class="nw">{lease_at(lease.get("acquired_at"))}</td><td>{lease_timer(lease, now)}</td>'
            f'<td class="nw" data-v="{last_used or 0}"{title_attr(utc_time(last_used) if last_used else None)}>'
            f"{esc((fmt_ago(last_used, now) if last_used is not None else 'never') if lease.get('last_use_known') else 'unknown')}</td>"
            f'<td class="r">{jobs} ({len(lease.get("running_jobs") or [])} running)</td><td class="lease-actions">{actions}</td></tr>'
        )
    lease_table = (
        '<div class="table"><table id="leases"><thead><tr>'
        '<th><input type="checkbox" id="select-all-leases" aria-label="Select all visible active leases"></th>'
        + "".join(
            sort_th(name)
            for name in (
                "Group",
                "Members",
                "Holder",
                "Labels",
                "Purpose",
                "Acquired",
                "Expires",
                "Last used",
                "Jobs",
            )
        )
        + f"<th>Actions</th></tr></thead><tbody>{''.join(body)}</tbody></table></div>"
        '<div class="empty" id="leasenone" hidden>No leases match.</div>'
    )
    client_rows = []
    for client in clients:
        held = client.get("leases") or []
        held_links = []
        held_leases = [lease if isinstance(lease, dict) else by_lease.get(str(lease), {}) for lease in held]
        for lease in held_leases:
            lease_id = lease.get("lease_id", "")
            title = lease.get("group", lease_id)
            held_links.append(
                f'<a href="{esc(link("/ui/leases", state="all", peek=lease_id))}#s-lease-peek">{esc(title)}</a>'
            )
        client_rows.append(
            f'<tr data-client-row data-q="{esc(json.dumps({**client, "leases": held_leases}, ensure_ascii=False).lower())}" '
            f'data-label-sets="{esc(json.dumps([lease.get("labels") or {} for lease in held_leases]))}">'
            f'<td><b>{esc(client.get("name") or "")}</b><span class="sub">{esc(client["client_id"])}</span></td>'
            f"<td>{esc(client.get('kind') or '')}</td><td>{esc(client.get('host') or '')}</td>"
            f'<td class="mono">{esc(client.get("session_id") or "—")}</td>'
            f"<td>{lease_at(client.get('registered_at'))}</td><td>{lease_at(client.get('last_seen_at'))}</td>"
            f"<td>{len(held)} · {', '.join(held_links) or 'no leases held'}</td></tr>"
        )
    client_table = (
        '<div class="table"><table id="lease-clients"><thead><tr>'
        + "".join(
            sort_th(name)
            for name in (
                "Client",
                "Kind",
                "Host",
                "Copilot session",
                "Registered",
                "Last seen",
                "Leases held",
            )
        )
        + f"</tr></thead><tbody>{''.join(client_rows)}</tbody></table></div>"
    )
    group_rows = []
    for group in groups:
        group_rows.append(
            f'<tr data-group-row data-q="{esc(json.dumps(group, ensure_ascii=False).lower())}" '
            f'data-labels="{esc(json.dumps((group.get("lease") or {}).get("labels") or {}))}">'
            f'<td><b>{esc(group["group"])}</b><span class="sub">{esc(", ".join(group["members"].values()))}</span></td>'
            f"<td>{lease_state(group['state'])}</td><td>{lease_cell(group, now)}</td>"
            f"<td>{'now' if group['state'] == 'free' else 'pinned' if (group.get('lease') or {}).get('running_jobs') else lease_at(group.get('leasable_at'))}</td>"
            f'<td>{lease_at(group.get("last_used_at"))}<span class="sub">{esc(str(group.get("last_used_by") or ""))}</span></td></tr>'
        )
    group_table = (
        '<div class="table"><table id="lease-groups"><thead><tr>'
        + "".join(sort_th(name) for name in ("Group", "State", "Lease", "Leasable at", "Last used"))
        + f"</tr></thead><tbody>{''.join(group_rows)}</tbody></table></div>"
    )
    peek = _last(query, "peek")
    peek_section = ""
    if peek:
        try:
            lease = store.get_lease(peek)
        except (KeyError, ValueError, LeaseError):
            lease = None
        if lease:
            events = "".join(
                f'<tr><td class="nw">{lease_at(event.get("at") or event.get("timestamp"))}</td>'
                f"<td><b>{esc(str(event.get('event') or event.get('type') or ''))}</b></td>"
                f'<td class="lease-event">{esc(json.dumps({key: value for key, value in event.items() if key not in ("at", "timestamp", "event", "type")}, ensure_ascii=False))}</td></tr>'
                for event in lease.get("events") or []
            )
            peek_body = (
                f"<p>{lease_cell({'group': lease['group'], 'state': lease['state'], 'lease': lease}, now)}</p>"
                f'<p class="fine">Purpose: {esc(lease.get("purpose") or "—")}</p>'
                f'<p><a href="{esc(link("/ui/logs", target="*", lease=peek))}">Logs</a> · '
                f'<a href="{esc(link("/ui/jobs", lease=peek))}">Jobs and live output</a></p>'
                f'<div class="table"><table><thead><tr><th>Time (UTC)</th><th>Event</th><th>Detail</th></tr></thead>'
                f"<tbody>{events}</tbody></table></div>"
            )
        else:
            peek_body = '<div class="empty">This lease no longer exists.</div>'
        peek_section = section("lease-peek", "04", f"Lease {peek}", "event history · read-only", peek_body)
    rules = (
        f'<p class="fine mono">{esc(str(store.config_path)) if store.config_path else "In-memory defaults"}</p>'
        f'<pre class="lease-rules">{esc(yaml.safe_dump(store.config, sort_keys=False, allow_unicode=True))}</pre>'
        '<p class="fine">Effective configuration, read-only. First matching rule wins; unmatched targets are groups of one. '
        "Leases are advisory. Explicit release skips cooldown; running jobs pin expiry; idle open terminals do not.</p>"
    )
    return (
        hero("LEASES", "Who is holding which devbox", chips)
        + section("leases", "01", "Leases", "a lease always covers the whole group", toolbar + lease_table)
        + section(
            "lease-clients", "02", "Clients", "who registered, and which leases they hold", client_table
        )
        + section("lease-groups", "03", "Lease groups", "availability for new leases right now", group_table)
        + peek_section
        + section("lease-rules", "05", "Lease rules", "effective config, read-only", rules)
    )


def hycos_page(report: dict[str, Any], now: float) -> str:
    rows = report["targets"]
    head = (
        '<tr><th class="r">#</th>'
        + sort_th("Relay HYCO")
        + sort_th("Status")
        + sort_th("Last used")
        + sort_th("Last seen online")
        + fact_headers()
        + sort_th("Lease")
        + sort_th("Last reported purpose")
        + sort_th(f"Jobs over {report['range']}", sort=False)
        + sort_th("Jobs", right=True)
        + sort_th("Failed", right=True)
        + sort_th("Success", right=True)
        + sort_th("p50 duration", right=True)
        + sort_th("Moved", right=True)
        + sort_th("Latency", right=True)
        + sort_th("Uptime", right=True)
        + sort_th("Top origin")
        + "</tr>"
    )
    body = []
    for i, row in enumerate(rows, 1):
        origin = row["top_origin"]
        origin_text = f"{origin['origin']} ({origin['share_pct']}%)" if origin else "—"
        last_used = row["last_used_ts"]
        up = row["status"] == ONLINE
        group = row.get("lease_group")
        lease = (group or {}).get("lease") or {}
        lease_status = group["state"] if group else "unavailable"
        facets = facet_values(row)
        aliases = facet_aliases(row)
        remote = (
            f'<a class="remote-link" href="{esc(link("/ui/exec", target=row["id"]))}" '
            f'aria-label="Remote execute on {esc(row["label"])}">{TERMINAL_ICON}Remote</a>'
            if up and row.get("terminal_shells")
            else ""
        )
        terms = (
            row["label"],
            row["id"],
            row["status"],
            "up" if up else "down",
            row["host"],
            row["user"],
            row["os"],
            host_line(row),
            row["reason"],
            (row.get("purpose") or {}).get("latest"),
            (group or {}).get("group"),
            lease_status,
            lease.get("lease_id"),
            (lease.get("holder") or {}).get("name"),
            lease.get("purpose"),
            json.dumps(lease.get("labels") or {}, ensure_ascii=False),
            *(value for values in facets.values() for value in values if value != UNKNOWN_VALUE),
        )
        body.append(
            f'<tr data-href="{esc(link("/ui/logs", target=row["id"]))}" '
            f'data-q="{esc(" ".join(str(term) for term in terms if term).lower())}" '
            f'data-up="{1 if up else 0}" data-lease-state="{esc(lease_status)}" '
            f'data-facets="{esc(json.dumps(facets, ensure_ascii=False, separators=(",", ":")))}"'
            + (
                f' data-facet-aliases="{esc(json.dumps(aliases, ensure_ascii=False, separators=(",", ":")))}"'
                if aliases
                else ""
            )
            + ">"
            f'<td class="idx">{i:02d}</td>'
            f'<td data-v="{esc(row["label"].lower())}">{hyco_cell(row)}{remote}</td>'
            f"{hyco_status_cell(row, now)}"
            f'<td class="nw" data-v="{last_used or 0}"{title_attr(utc_time(last_used) if last_used else None)}>'
            f"{esc(fmt_ago(last_used, now))}</td>"
            f"{last_online_cell(row, now)}"
            f"{fact_cells(row)}"
            f'<td class="lease-cell" data-v="{esc(lease_status)}">{lease_cell(group, now) if group else "—"}</td>'
            f'<td class="purpose-cell" data-v="{esc(((row.get("purpose") or {}).get("latest") or "").lower())}">'
            f"{purpose_cell(row)}</td>"
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
            f"<td>{esc(origin_text)}</td></tr>"
        )
    toolbar = (
        '<div class="toolbar"><input type="search" id="hycoq" placeholder="search HYCOs, e.g. _windows or '
        'raki- linux (all terms must match)…" autocomplete="off"><span class="seg st"><a class="on" data-s="all">'
        'all</a><a data-s="up">up</a><a data-s="down">down</a></span>'
        '<label class="tg">Lease <select id="hycolease" aria-label="Lease state">'
        '<option value="all">all states</option><option value="leased">leased</option><option value="free">free</option>'
        '<option value="cooling">cooling down</option><option value="unavailable">unavailable</option></select></label>'
        '<span class="chip" id="hycocount"></span></div>' + facet_toolbar()
    )
    patterns = (
        '<svg width="0" height="0" aria-hidden="true"><defs>'
        '<pattern id="purpose-gap" width="6" height="6" patternUnits="userSpaceOnUse">'
        '<rect width="6" height="6" class="purpose-pattern-bg"/>'
        '<path d="M-1 1L1-1M0 6L6 0M5 7L7 5" class="purpose-pattern-line"/></pattern>'
        '<pattern id="purpose-mix" width="6" height="6" patternUnits="userSpaceOnUse">'
        '<rect width="6" height="6" class="purpose-pattern-bg"/>'
        '<path d="M0 0L6 6M0 6L6 0" class="purpose-pattern-line"/></pattern></defs></svg>'
    )
    table = (
        f'{patterns}{toolbar}<div class="table"><table id="hycos"><thead>{head}</thead><tbody>{"".join(body)}</tbody></table>'
        '</div><div class="empty" id="hyconone" hidden>No relay HYCOs match this search and these filters.</div>'
        if rows
        else '<div class="empty">No relays are hydrated yet. Run Sync-PrivyHubRelays.ps1 or edit relays.json.</div>'
    )
    note = (
        '<p class="fine">Most used first; click a header to re-sort and a row for its logs. Sparklines show jobs '
        f"started per {esc(fmt_span(report['bucket_s']))} (red ticks: failures). Latency and uptime come from the hub's "
        "own listener probes, not from Azure. Status times and Last seen online come from the same probes, which run "
        'when a page or tool asks about a HYCO, so they are approximate; "for at least" means the status has not '
        "changed since the hub first checked. They are kept in memory and reset when the hub restarts.</p>"
        '<p class="fine">OS, Arch, Privy, Python and Shell come from each listener\'s own report. Offline HYCOs show '
        "their last report in grey (hover for its time); the hub keeps it across restarts. Env is inferred from "
        "the HYCO name suffix. Filters combine: values inside one filter are alternatives, and every filter must "
        "match. Columns chooses which of these columns to show.</p>"
        '<p class="fine purpose-legend">Purpose: colors show reported work; striped gaps mean unknown; '
        "crosshatching means multiple changes in a small interval. Hover or focus for details; click to pin history. "
        "This is not execution duration or exclusive ownership. In memory only; resets on hub restart.</p>"
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
    lease_id = _last(query, "lease")
    every = lease_entries(service, lease_id)
    kept: dict[str, int] = {}
    for entry in every:
        kept[entry.target_id] = kept.get(entry.target_id, 0) + 1
    selected = _last(query, "target")
    if lease_id and not selected:
        selected = "*"
    if selected not in by_id and selected != "*":
        used = sorted((row for row in rows if row["last_used_ts"]), key=lambda row: -row["last_used_ts"])
        selected = used[0]["id"] if used else (rows[0]["id"] if rows else "*")
    limit = limit_of(query, DEFAULT_LOG_LIMIT)
    pods = [
        f'<a href="{esc(link("/ui/logs", target="*", lease=lease_id))}"{ON if selected == "*" else ""}>'
        f"<b>All relay HYCOs</b><span>{len(every):,}</span><small>every job, newest first</small></a>"
    ]
    for row in sorted(rows, key=lambda row: (-(row["last_used_ts"] or 0), row["label"].lower())):
        who = row["user"] and row["host"] and f"{row['user']}@{row['host']}"
        pods.append(
            f'<a href="{esc(link("/ui/logs", target=row["id"], lease=lease_id))}"{ON if row["id"] == selected else ""}>'
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
            f'<a class="more" href="{esc(link("/ui/logs", target=selected, lease=lease_id, limit=str(min(MAX_LIMIT, limit * 4))))}">'
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
    if lease_id:
        chips += f'<span class="chip">lease {esc(lease_id)}</span><a href="/ui/logs?target=*">Clear lease filter</a>'
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
    lease_id = _last(query, "lease")
    entries = sorted(lease_entries(service, lease_id), key=lambda entry: entry.created_at, reverse=True)
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
        f'<a href="{esc(link("/ui/jobs", state=name or None, lease=lease_id))}"{ON if wanted == name else ""}>{label}</a>'
        for name, label in (("", "all"), ("running", "running"), ("failed", "failed"), ("succeeded", "ok"))
    )
    table = (
        f'<div class="table"><table id="jobs"><thead>{head}</thead><tbody>{body}</tbody></table></div>'
        if views
        else '<div class="empty">No jobs match.</div>'
    )
    if len(views) > limit:
        table += (
            f'<a class="more" href="{esc(link("/ui/jobs", state=wanted or None, lease=lease_id, limit=str(min(MAX_LIMIT, limit * 4))))}">'
            f"Show more ({len(views) - limit:,} older jobs)</a>"
        )
    chips = f'<span class="seg">{filters}</span><span class="chip">{plural(len(views), "job")}</span>'
    if lease_id:
        chips += f'<span class="chip">lease {esc(lease_id)}</span><a href="/ui/jobs">Clear lease filter</a>'
    return hero("EVERY JOB THE HUB REMEMBERS", "Jobs", chips) + section(
        "jobs", "01", "Jobs", "newest first · click a row for its terminal", table
    )


# ---- entry points -----------------------------------------------------------------------


def render(service: Any, page: str, query: Mapping[str, list[str]], base_url: str) -> HttpResponse:
    """One dashboard page (``overview``, ``hycos``, ``logs``, or ``jobs``) as an HTML response."""
    rng = range_of(query)
    try:
        selection = ReportSelection.parse(query) if page == "overview" else ReportSelection(range_name=rng)
    except ValueError as exc:
        return error_response(400, "invalid_argument", str(exc))
    report = build_report(service, selection=selection)
    enrich_lease_report(service, report)
    now = time.time()
    nonce = secrets.token_urlsafe(18)
    params: dict[str, str | None] = {}
    if page == "overview":
        title, main = "Overview", overview_page(service, report, now)
        params = {key: _last(query, key) or None for key in ("range", "start", "end", "resolution")}
    elif page == "hycos":
        title, main = "Relay HYCOs", hycos_page(report, now)
        params = {
            "q": _last(query, "q") or None,
            "status": _last(query, "status") or None,
            "lease_state": _last(query, "lease_state") or None,
        }
    elif page == "leases":
        title, main = "Leases", leases_page(service, query, now)
        params = {key: _last(query, key) or None for key in ("state", "q", "label", "peek")}
    elif page == "exec":
        title, main = "Remote Exec", workspace(service, report, _last(query, "target"))
        params = {"target": _last(query, "target") or None}
    elif page == "logs":
        target, main = logs_page(service, report, query, now)
        title = f"Logs · {target}"
        params = {key: _last(query, key) or None for key in ("target", "limit", "lease")}
    elif page == "jobs":
        title, main = "Jobs", jobs_page(service, report, query, now)
        params = {key: _last(query, key) or None for key in ("state", "limit", "lease")}
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
