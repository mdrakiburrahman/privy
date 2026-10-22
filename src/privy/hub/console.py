"""Everything the hub prints for the operator watching its console.

* one line per REST/MCP call (what was asked, the outcome, how long it took)
* one line per probe (relay/hyco, status, latency, listener host or reason)
* per remote command: a request box, the live output lines prefixed with
  ``[<job> <hyco>]``, and a response box, matching ``privy server``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from privy._logfmt import MAX_LOG_BYTES, box, clip_text, indent
from privy.executor import echo_barrier, echo_output

if TYPE_CHECKING:
    from privy.hub.config import Target
    from privy.hub.jobs import HubJob
    from privy.hub.registry import ProbeResult

log = logging.getLogger("privy.hub")

ARGS_PREVIEW_CHARS = 300
_PROGRESS_STEPS = (25, 50, 75)


def compact_json(value: Any, limit: int = ARGS_PREVIEW_CHARS) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def call(label: str, outcome: str, seconds: float) -> None:
    log.info("%s -> %s (%.1fs)", label, outcome, seconds)


def probe(target: Target, result: ProbeResult) -> None:
    if result.status == "online" and result.info:
        detail = f"host={result.info.get('hostname')} privy={result.info.get('privy_version')}"
    elif result.status == "online":
        detail = "older listener without host details"
    else:
        detail = result.error or ""
    log.info("probe %s: %s %sms %s", target.id, result.status, result.latency_ms, detail)


def _where(target: Target, host: str | None) -> str:
    where = f"relay={target.relay} ({target.namespace})  hyco={target.path}"
    return where + (f"  host={host}" if host else "")


def _echo_prefix(job: HubJob) -> str:
    return f"{job.id[:8]} {job.target.path}"


def job_started(job: HubJob, host: str | None) -> None:
    if job.type == "exec":
        spec = job.spec
        lines = [
            f"│   via={job.origin}  language={spec['language']}  mode={spec['mode']}  "
            f"timeout={spec['timeout_s']:g}s"
            + (f"  broadcast={job.broadcast_id}" if job.broadcast_id else ""),
        ]
        if not job.broadcast_id:
            # A broadcast prints its code once, in the BROADCAST box.
            lines.append(indent(clip_text(spec["code"]), "│   "))
        title = f"▶ RUN {job.id}  {_where(job.target, host)}"
    else:
        spec = job.spec
        arrow = "->" if job.type == "upload" else "<-"
        lines = [
            f"│   via={job.origin}  overwrite={spec['overwrite']}",
            f"│   local {spec['local_path']} {arrow} remote {spec['remote_path']}",
        ]
        title = f"▶ {job.type.upper()} {job.id}  {_where(job.target, host)}"
    log.info(box(title, lines))


def echo(job: HubJob, stream: str, data: bytes) -> None:
    echo_output(_echo_prefix(job), stream, data)


def _tail(data: bytes, total: int) -> str:
    if total <= 0:
        return ""
    text = data[-MAX_LOG_BYTES:].decode("utf-8", "replace").rstrip("\n")
    hidden = total - min(len(data), MAX_LOG_BYTES)
    return (f"… [{hidden} earlier bytes]\n" if hidden > 0 else "") + text


def job_finished(job: HubJob) -> None:
    """Print the response box (call after the job is settled)."""
    prefix = _echo_prefix(job)
    echo_output(prefix, "stdout", None)
    echo_output(prefix, "stderr", None)
    echo_barrier(0.5)
    mark = "✓" if job.state == "succeeded" else "✗"
    summary = f"{mark} {job.state}"
    if job.exit_code is not None:
        summary += f"  exit={job.exit_code}"
    duration = job.duration_ms()
    if duration is not None:
        summary += f"  {duration}ms"
    title = f"◀ RESULT {job.id}  relay={job.target.relay}  hyco={job.target.path}  {summary}"
    lines: list[str] = []
    if job.error:
        lines.append(f"│ error: {job.error}")
    if job.type == "exec":
        if job.remote_job_id:
            title += f"  remote={job.remote_job_id[:8]}"
        for name, buffer in (("stdout", job.stdout), ("stderr", job.stderr)):
            lines.append(f"│ {name}:")
            lines.append(indent(_tail(buffer.tail(MAX_LOG_BYTES), buffer.total), "│   "))
    elif job.transfer:
        lines.append(f"│ {job.transfer.get('size')} bytes  sha256={job.transfer.get('sha256')}")
        if job.transfer.get("resumed_from"):
            lines.append(f"│ resumed from byte {job.transfer['resumed_from']}")
    log.info(box(title, lines or ["│ (no output)"]))


def transfer_progress(job: HubJob, done: int, total: int, last_step: int) -> int:
    """Log 25/50/75% milestones; returns the last milestone printed."""
    if total <= 0:
        return last_step
    percent = done * 100 // total
    for step in _PROGRESS_STEPS:
        if last_step < step <= percent < 100:
            log.info("%s %s %s%% (%d/%d bytes)", job.id, job.type, step, done, total)
            last_step = step
    return last_step


def broadcast_started(
    broadcast_id: str,
    *,
    patterns: list[str],
    targets: list[Target],
    skipped: list[dict[str, Any]],
    code: str,
    origin: str,
) -> None:
    lines = [
        f"│   via={origin}  patterns={', '.join(patterns)}",
        f"│   targets ({len(targets)}): {', '.join(target.id for target in targets) or '-'}",
    ]
    if skipped:
        lines.append(
            f"│   skipped ({len(skipped)}): "
            + ", ".join(f"{item['target']} ({item['status']})" for item in skipped)
        )
    lines.append(indent(clip_text(code), "│   "))
    log.info(box(f"▶ BROADCAST {broadcast_id}", lines))


def cancel_requested(job: HubJob) -> None:
    log.info("cancel requested for job %s on %s (state %s)", job.id, job.target.id, job.state)
