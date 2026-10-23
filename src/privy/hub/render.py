"""Plain-text renderings of hub results, for LLMs and terminals.

REST callers get them with ``?format=text``; MCP tool results carry the same
text as their ``content`` (the JSON stays in ``structuredContent``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def _seconds(milliseconds: Any) -> str:
    return f"{milliseconds / 1000:.1f}s" if isinstance(milliseconds, (int, float)) else ""


def _ensure_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def _next_hint(kind: str, identifier: str, transport: str) -> str:
    if kind == "job":
        if transport == "mcp":
            return f'still running: call get_job(job_id="{identifier}") for the next output\n'
        return (
            f"still running: GET /v1/jobs/{identifier}?format=text returns the next output; "
            f"GET /v1/jobs/{identifier}/stream follows it live\n"
        )
    if transport == "mcp":
        return f'still running: call get_broadcast(broadcast_id="{identifier}") for the next output\n'
    return f"still running: GET /v1/broadcasts/{identifier}?format=text returns the next output\n"


def _job_header(job: dict[str, Any]) -> str:
    what = job.get("language") or job.get("type", "job")
    header = f"job {job['job_id']} on {job['target']} ({what}): {job['state']}"
    if job.get("exit_code") is not None:
        header += f", exit {job['exit_code']}"
    if job.get("duration_ms") is not None:
        header += f", {_seconds(job['duration_ms'])}"
    if job.get("error"):
        header += f" ({job['error']})"
    return header + "\n"


def _job_body(job: dict[str, Any]) -> str:
    if "stdout" not in job:
        progress = job.get("progress") or {}
        lines = [f"{job.get('type')}: local {job.get('local_path')} / remote {job.get('remote_path')}"]
        result = job.get("result")
        if job.get("type") == "sync":
            counts = result or progress
            lines.append(
                f"{counts.get('phase', 'queued')}: {counts.get('files_copied', 0)} copied, "
                f"{counts.get('files_skipped', 0)} skipped, "
                f"{counts.get('files_done', 0)}/{counts.get('files_total', 0)} files, "
                f"{counts.get('bytes_done', 0)}/{counts.get('bytes_total', 0)} bytes"
            )
        elif result:
            lines.append(f"{result.get('size')} bytes, sha256 {result.get('sha256')}")
        elif progress:
            lines.append(f"{progress.get('bytes_done')} of {progress.get('bytes_total')} bytes so far")
        return "\n".join(lines) + "\n"
    parts: list[str] = []
    for stream in ("stdout", "stderr"):
        skipped = job.get(f"{stream}_skipped_bytes")
        text = job.get(stream) or ""
        if skipped:
            parts.append(f"[{skipped} earlier bytes of {stream} skipped; page them with read_job_output]\n")
        if text:
            parts.append(("--- stderr ---\n" if stream == "stderr" else "") + _ensure_newline(text))
    return "".join(parts) or "(no new output)\n"


def render_job(job: dict[str, Any], transport: str = "rest") -> str:
    text = _job_header(job) + _job_body(job)
    if not job.get("done"):
        text += _next_hint("job", job["job_id"], transport)
    return text


def render_broadcast(result: dict[str, Any], transport: str = "rest") -> str:
    summary = ", ".join(f"{count} {state}" for state, count in result.get("summary", {}).items() if count)
    lines = [f"broadcast {result['broadcast_id']} ({', '.join(result.get('patterns', []))}): {summary}\n"]
    for job in result.get("jobs", []):
        lines.append("== " + _job_header(job)[len(f"job {job['job_id']} on ") :])
        lines.append(_job_body(job))
    for item in result.get("skipped", []):
        lines.append(f"skipped {item['target']} ({item['status']})\n")
    if not result.get("done"):
        lines.append(_next_hint("broadcast", result["broadcast_id"], transport))
    return "".join(lines)


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return "".join(
        "  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() + "\n"
        for row in rows
    )


def render_targets(result: dict[str, Any], transport: str = "rest") -> str:
    counts = result.get("counts", {})
    relays = ", ".join(relay["name"] for relay in result.get("relays", [])) or "none"
    text = f"{counts.get('online', 0)} of {counts.get('targets', 0)} machines online (relays: {relays})\n"
    targets = result.get("targets", [])
    online = [target for target in targets if target["status"] == "online"]
    if online:
        rows = [["machine", "target", "host", "os", "language", "privy"]]
        for target in online:
            rows.append(
                [
                    target.get("machine", ""),
                    target["id"],
                    target.get("host", "-"),
                    target.get("os", "-"),
                    target.get("default_language", "-"),
                    target.get("privy_version", "older"),
                ]
            )
        text += "online:\n" + _table(rows)
    others: dict[str, list[str]] = {}
    for target in targets:
        if target["status"] != "online":
            others.setdefault(target["status"], []).append(target["id"])
    for status, ids in sorted(others.items()):
        text += f"{status} ({len(ids)}): {', '.join(ids)}\n"
    if result.get("hint"):
        text += result["hint"] + "\n"
    return text


def render_target(target: dict[str, Any], transport: str = "rest") -> str:
    lines = [f"{target['id']} (machine {target.get('machine')}): {target['status']}"]
    if target.get("latency_ms") is not None:
        lines[0] += f", {target['latency_ms']} ms"
    for key in (
        "host",
        "user",
        "os",
        "privy_version",
        "default_language",
        "reason",
        "details",
        "description",
    ):
        if target.get(key):
            lines.append(f"{key}: {target[key]}")
    if target.get("languages"):
        lines.append("languages: " + ", ".join(target["languages"]))
    info = target.get("listener_info") or {}
    runtimes = info.get("runtimes") or {}
    if runtimes:
        lines.append(
            "runtimes: " + ", ".join(f"{name}={path or 'missing'}" for name, path in runtimes.items())
        )
    if info.get("cwd"):
        lines.append(f"cwd: {info['cwd']}")
    lines.append(f"relay: {target.get('relay')} (namespace {target.get('namespace')})")
    return "\n".join(lines) + "\n"


def render_jobs(result: dict[str, Any], transport: str = "rest") -> str:
    jobs = result.get("jobs", [])
    if not jobs:
        return "no jobs\n"
    rows = [["job", "target", "what", "state", "exit", "duration", "created"]]
    for job in jobs:
        rows.append(
            [
                job["job_id"],
                job["target"],
                job.get("language") or job.get("type", ""),
                job["state"],
                str(job.get("exit_code", "")),
                _seconds(job.get("duration_ms")),
                job.get("created_at", ""),
            ]
        )
    return _table(rows)


def render_output(result: dict[str, Any], transport: str = "rest") -> str:
    footer = (
        f"[{result['stream']} bytes {result['offset']}-{result['next_offset']} of {result['total_bytes']}"
        + (", end" if result.get("eof") else "")
        + "]\n"
    )
    return _ensure_newline(result.get("data", "")) + footer


_RENDERERS: dict[str, Callable[[dict[str, Any], str], str]] = {
    "list_targets": render_targets,
    "get_target": render_target,
    "run": render_job,
    "get_job": render_job,
    "cancel_job": render_job,
    "upload_file": render_job,
    "download_file": render_job,
    "list_jobs": render_jobs,
    "broadcast": render_broadcast,
    "get_broadcast": render_broadcast,
    "read_job_output": render_output,
}


def render(operation: str, result: dict[str, Any], *, transport: str = "rest") -> str:
    renderer = _RENDERERS.get(operation)
    if renderer is None:
        return json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    return renderer(result, transport)


def render_error(error: dict[str, Any]) -> str:
    text = f"error ({error.get('code')}): {error.get('message')}\n"
    candidates = (
        (error.get("details") or {}).get("candidates") if isinstance(error.get("details"), dict) else None
    )
    if candidates:
        text += "candidates: " + ", ".join(candidates) + "\n"
    return text
