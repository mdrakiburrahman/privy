"""Transport-neutral hub operations shared by the REST API and the MCP endpoint.

Each :class:`Operation` declares its parameters once; the same declaration
validates arguments, renders MCP tool input schemas, and renders the OpenAPI
document, so the two interfaces cannot drift apart.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from privy.hub.config import Relay, RelaysSnapshot, Target
from privy.hub.jobs import (
    Broadcast,
    HubBusyError,
    HubJob,
    JobLookupError,
    JobManager,
    utc_iso,
)
from privy.hub.registry import ONLINE, UNKNOWN, ProbeResult, TargetLookupError, TargetRegistry

LANGUAGES = ("python", "bash", "powershell")
MODES = ("subprocess", "inprocess")
STREAMS = ("stdout", "stderr")
DEFAULT_WAIT_S = 20.0
MAX_WAIT_S = 120.0
DEFAULT_TIMEOUT_S = 600.0
MAX_TIMEOUT_S = 7 * 24 * 3600.0
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024
DEFAULT_BROADCAST_OUTPUT_BYTES = 4 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
DEFAULT_READ_LIMIT = 64 * 1024
MAX_CODE_CHARS = 1024 * 1024
CANCEL_SETTLE_S = 10.0

INSTRUCTIONS = """\
privy hub runs code on remote machines (privy listeners) reached through Azure Relay.
1. Call list_targets to see every machine; ids look like '<relay>/<hybrid-connection>'.
   Status comes from a live probe (cached briefly; refresh=true re-probes now).
2. Call run with language python, bash, or powershell. It waits up to wait_s seconds and
   returns either the final result or state 'running' with a job_id.
3. Keep calling get_job(job_id) until done is true. stdout/stderr hold the tail of each
   stream; page through everything with read_job_output.
4. broadcast runs one command on many targets; upload_file/download_file copy files between
   this machine (absolute local paths) and a target.
Commands run with the listener user's full privileges. Prefer read-only commands unless the
user asked for changes, and never print secrets."""


class HubError(Exception):
    """An operation failed in a way the caller can act on."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        code: str = "bad_request",
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details is not None:
            error["details"] = self.details
        return error


def _invalid(message: str) -> HubError:
    return HubError(message, code="invalid_argument")


def _require_text(name: str, value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(f"{name} must be valid Unicode text (it contains an unpaired surrogate)") from None
    return value


@dataclass(frozen=True)
class Param:
    """One operation argument."""

    name: str
    type: str
    description: str
    required: bool = False
    default: Any = None
    enum: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.type == "array":
            schema["items"] = {"type": "string"}
        if self.enum:
            schema["enum"] = list(self.enum)
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.max_length is not None:
            schema["maxLength"] = self.max_length
        if self.default is not None:
            schema["default"] = self.default
        return schema

    def coerce(self, value: Any) -> Any:
        """Validate ``value``; strings are accepted for scalars (REST query parameters)."""
        name = self.name
        if self.type == "string":
            if not isinstance(value, str):
                raise _invalid(f"{name} must be a string")
            if self.max_length is not None and len(value) > self.max_length:
                raise _invalid(f"{name} must be at most {self.max_length} characters")
            if self.enum and value not in self.enum:
                raise _invalid(f"{name} must be one of: {', '.join(self.enum)}")
            return _require_text(name, value)
        if self.type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"):
                return True
            if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off"):
                return False
            raise _invalid(f"{name} must be a boolean")
        if self.type in ("integer", "number"):
            if isinstance(value, bool):
                raise _invalid(f"{name} must be a {self.type}")
            if isinstance(value, str):
                try:
                    value = float(value.strip())
                except ValueError:
                    raise _invalid(f"{name} must be a {self.type}") from None
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise _invalid(f"{name} must be a finite {self.type}")
            if self.type == "integer":
                if float(value) != int(value):
                    raise _invalid(f"{name} must be an integer")
                value = int(value)
            else:
                value = float(value)
            if self.minimum is not None and value < self.minimum:
                raise _invalid(f"{name} must be at least {self.minimum:g}")
            if self.maximum is not None and value > self.maximum:
                raise _invalid(f"{name} must be at most {self.maximum:g}")
            return value
        if self.type == "array":
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise _invalid(f"{name} must be a list of strings")
            return [_require_text(name, item) for item in value]
        raise AssertionError(f"unsupported parameter type {self.type!r}")


@dataclass(frozen=True)
class Operation:
    """One hub capability: an MCP tool and a REST endpoint."""

    name: str
    title: str
    description: str
    params: tuple[Param, ...]
    read_only: bool
    destructive: bool = False
    idempotent: bool = False

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {param.name: param.json_schema() for param in self.params},
            "required": [param.name for param in self.params if param.required],
            "additionalProperties": False,
        }

    def parse(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        known = {param.name for param in self.params}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise _invalid(f"unknown argument(s) for {self.name}: {', '.join(unknown)}")
        values: dict[str, Any] = {}
        for param in self.params:
            value = raw.get(param.name)
            if value is None:
                if param.required:
                    raise _invalid(f"{param.name} is required")
                default = param.default
                values[param.name] = list(default) if isinstance(default, list) else default
            else:
                values[param.name] = param.coerce(value)
        return values


_TARGET = Param(
    "target",
    "string",
    "Target id '<relay>/<hybrid-connection>' from list_targets, or a hybrid connection name "
    "that is unique across relays.",
    required=True,
)
_LANGUAGE = Param("language", "string", "Interpreter to run the code with.", required=True, enum=LANGUAGES)
_CODE = Param(
    "code",
    "string",
    "Source code to run. Bash and PowerShell receive it as a script; Python as a program.",
    required=True,
    max_length=MAX_CODE_CHARS,
)
_MODE = Param(
    "mode",
    "string",
    "subprocess starts a fresh interpreter. inprocess runs Python inside the listener process "
    "and shares state across calls (python only).",
    default="subprocess",
    enum=MODES,
)
_TIMEOUT = Param(
    "timeout_s",
    "number",
    "Remote execution timeout in seconds; the remote process tree is killed when it expires.",
    default=DEFAULT_TIMEOUT_S,
    minimum=1,
    maximum=MAX_TIMEOUT_S,
)
_WAIT = Param(
    "wait_s",
    "number",
    "Seconds to wait for completion before returning. The job keeps running afterwards.",
    default=DEFAULT_WAIT_S,
    minimum=0,
    maximum=MAX_WAIT_S,
)
_JOB_ID = Param("job_id", "string", "Job id returned by run, broadcast, upload_file, or download_file.", True)


def _max_output(default: int) -> Param:
    return Param(
        "max_output_bytes",
        "integer",
        "Return at most this many trailing bytes of stdout and of stderr.",
        default=default,
        minimum=0,
        maximum=MAX_OUTPUT_BYTES,
    )


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "list_targets",
        "List targets",
        "List every machine (privy listener) the hub can reach, grouped by Azure Relay, with its "
        "status: online, offline (no listener connected), not_found (hybrid connection missing), "
        "unauthorized (stale key), unreachable, or error. Online listeners report hostname, OS, user, "
        "privy version, and languages. Status is probed live and cached briefly. Start here.",
        (
            Param(
                "refresh",
                "boolean",
                "Re-probe every target now instead of using cached status.",
                default=False,
            ),
            Param("online_only", "boolean", "Only return targets that are online.", default=False),
            Param("relay", "string", "Only return targets of this relay name."),
            Param(
                "probe",
                "boolean",
                "Probe targets without a fresh cached status. false returns cached status only (fast).",
                default=True,
            ),
        ),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "get_target",
        "Get target",
        "Show one target's live status and full listener details: runtimes, working directory, "
        "user, capabilities, and active jobs.",
        (_TARGET, Param("refresh", "boolean", "Re-probe now instead of using cached status.", default=True)),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "run",
        "Run code on a target",
        "Run Python, Bash, or PowerShell code on one target. Waits up to wait_s seconds; if the "
        "command finishes in time the result has exit_code, stdout, and stderr, otherwise state is "
        "'running' and you continue with get_job(job_id). stdout/stderr hold the tail of each "
        "stream (see *_truncated); page through all output with read_job_output. Runs with the "
        "listener user's privileges.",
        (_TARGET, _LANGUAGE, _CODE, _MODE, _TIMEOUT, _WAIT, _max_output(DEFAULT_MAX_OUTPUT_BYTES)),
        read_only=False,
        destructive=True,
    ),
    Operation(
        "get_job",
        "Get job",
        "Get a job's state and output tail, waiting up to wait_s seconds for it to finish. States: "
        "queued, running, succeeded, failed (non-zero exit), timed_out, cancelled, error (the hub "
        "could not run it, for example because the target is offline).",
        (_JOB_ID, _WAIT, _max_output(DEFAULT_MAX_OUTPUT_BYTES)),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "read_job_output",
        "Read job output",
        "Read a page of a job's stdout or stderr from a byte offset; continue from next_offset. "
        "The hub keeps the most recent 1 MiB of each stream; older bytes appear as skipped_bytes.",
        (
            _JOB_ID,
            Param("stream", "string", "Which stream to read.", default="stdout", enum=STREAMS),
            Param("offset", "integer", "Byte offset to start reading from.", default=0, minimum=0),
            Param(
                "limit",
                "integer",
                "Maximum bytes to return.",
                default=DEFAULT_READ_LIMIT,
                minimum=1,
                maximum=MAX_OUTPUT_BYTES,
            ),
        ),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "cancel_job",
        "Cancel job",
        "Cancel a queued or running job. Best effort: the listener kills the remote process tree "
        "and a partial file transfer stays resumable.",
        (_JOB_ID,),
        read_only=False,
        idempotent=True,
    ),
    Operation(
        "list_jobs",
        "List jobs",
        "List recent hub jobs, newest first, without their output.",
        (
            Param("target", "string", "Only jobs for this target."),
            Param("limit", "integer", "Maximum jobs to return.", default=20, minimum=1, maximum=200),
        ),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "broadcast",
        "Broadcast code to many targets",
        "Run the same code on every target matching the patterns, in parallel. Returns a "
        "broadcast_id, one job per target (state, exit_code, output tail), and skipped targets with "
        "their status. Call get_broadcast to wait for jobs that are still running.",
        (
            Param(
                "targets",
                "array",
                "Glob patterns matched against target ids ('dev/*') or hybrid connection names "
                "('*_windows').",
                default=["*"],
            ),
            _LANGUAGE,
            _CODE,
            _MODE,
            _TIMEOUT,
            _WAIT,
            Param(
                "only_online",
                "boolean",
                "Skip targets that are not online (uses cached status when fresh).",
                default=True,
            ),
            _max_output(DEFAULT_BROADCAST_OUTPUT_BYTES),
        ),
        read_only=False,
        destructive=True,
    ),
    Operation(
        "get_broadcast",
        "Get broadcast",
        "Get every job of a broadcast, waiting up to wait_s seconds for all of them to finish.",
        (
            Param("broadcast_id", "string", "Broadcast id returned by broadcast.", required=True),
            _WAIT,
            _max_output(DEFAULT_BROADCAST_OUTPUT_BYTES),
        ),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "upload_file",
        "Upload a file to a target",
        "Copy a file from this machine to a target in resumable, SHA-256 verified chunks. Runs as "
        "a job: if it is not done within wait_s, continue with get_job.",
        (
            _TARGET,
            Param("local_path", "string", "Absolute path of the source file on this machine.", required=True),
            Param(
                "remote_path",
                "string",
                "Destination path on the target; relative paths resolve against the listener's "
                "working directory.",
                required=True,
            ),
            Param("overwrite", "boolean", "Replace an existing destination file.", default=False),
            _WAIT,
        ),
        read_only=False,
        destructive=True,
    ),
    Operation(
        "download_file",
        "Download a file from a target",
        "Copy a file from a target to this machine in resumable, SHA-256 verified chunks. Runs as "
        "a job: if it is not done within wait_s, continue with get_job.",
        (
            _TARGET,
            Param("remote_path", "string", "Source file path on the target.", required=True),
            Param("local_path", "string", "Absolute destination path on this machine.", required=True),
            Param("overwrite", "boolean", "Replace an existing destination file.", default=False),
            _WAIT,
        ),
        read_only=False,
        destructive=True,
    ),
)
OPERATIONS_BY_NAME = {operation.name: operation for operation in OPERATIONS}


def _listener_summary(probe: ProbeResult | None) -> dict[str, Any] | None:
    if probe is None or probe.status != ONLINE:
        return None
    if probe.info is None:
        return {
            "details_available": False,
            "note": "This listener runs an older privy without listener_info; re-download privy.exe "
            "on that machine to report host details.",
        }
    info = probe.info
    platform = info.get("platform") or {}
    capabilities = info.get("capabilities") or {}
    return {
        "details_available": True,
        "hostname": info.get("hostname"),
        "user": info.get("user"),
        "os": platform.get("description") or platform.get("system"),
        "machine": platform.get("machine"),
        "privy_version": info.get("privy_version"),
        "languages": capabilities.get("languages"),
        "running_jobs": (info.get("jobs") or {}).get("running"),
        "uptime_s": info.get("uptime_s"),
    }


class HubService:
    """The hub's operations, independent of HTTP or MCP framing."""

    def __init__(
        self,
        *,
        registry: TargetRegistry,
        jobs: JobManager,
        version: str,
        started_at: float | None = None,
    ) -> None:
        self.registry = registry
        self.jobs = jobs
        self.version = version
        self.started_at = time.time() if started_at is None else started_at
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            operation.name: getattr(self, f"_op_{operation.name}") for operation in OPERATIONS
        }

    def invoke(self, name: str, raw_args: Mapping[str, Any] | None, *, origin: str) -> dict[str, Any]:
        operation = OPERATIONS_BY_NAME.get(name)
        if operation is None:
            raise HubError(f"unknown operation {name!r}", status=404, code="unknown_operation")
        if raw_args is not None and not isinstance(raw_args, Mapping):
            raise _invalid("arguments must be an object")
        args = operation.parse(raw_args or {})
        try:
            return self._handlers[name](origin=origin, **args)
        except TargetLookupError as exc:
            raise HubError(
                str(exc), status=404, code="target_not_found", details={"candidates": exc.candidates}
            ) from None
        except JobLookupError as exc:
            raise HubError(str(exc), status=404, code="not_found") from None
        except HubBusyError as exc:
            raise HubError(str(exc), status=429, code="busy") from None

    def summary(self) -> dict[str, Any]:
        snapshot = self.registry.snapshot()
        return {
            "version": self.version,
            "uptime_s": int(time.time() - self.started_at),
            "relays_file": self._relays_file_view(snapshot),
            "relays": len(snapshot.relays),
            "targets": len(snapshot.targets),
            "jobs": self.jobs.counts(),
        }

    # ---- views --------------------------------------------------------------

    @staticmethod
    def _relays_file_view(snapshot: RelaysSnapshot) -> dict[str, Any]:
        return {
            "path": str(snapshot.path),
            "exists": snapshot.file_exists,
            "loaded_at": utc_iso(snapshot.loaded_at),
            "error": snapshot.error,
        }

    @staticmethod
    def _relay_view(relay: Relay) -> dict[str, Any]:
        view: dict[str, Any] = {
            "name": relay.name,
            "namespace": relay.namespace,
            "targets": len(relay.targets),
        }
        if relay.description:
            view["description"] = relay.description
        view.update(relay.metadata)
        return view

    @staticmethod
    def _target_view(target: Target, probe: ProbeResult | None, *, full: bool = False) -> dict[str, Any]:
        view: dict[str, Any] = {
            "id": target.id,
            "relay": target.relay,
            "path": target.path,
            "status": probe.status if probe else UNKNOWN,
            "checked_at": utc_iso(probe.checked_at) if probe else None,
            "latency_ms": probe.latency_ms if probe else None,
            "error": probe.error if probe else None,
            "listener": _listener_summary(probe),
        }
        if target.description:
            view["description"] = target.description
        if full:
            view["namespace"] = target.namespace
            view["listener_info"] = probe.info if probe else None
        return view

    @staticmethod
    def _job_view(job: HubJob, max_output_bytes: int) -> dict[str, Any]:
        view = job.view(max_output_bytes=max_output_bytes)
        if not view["done"]:
            view["next_step"] = f"Call get_job with job_id '{job.id}' to wait for the result."
        return view

    def _broadcast_view(
        self, broadcast: Broadcast, jobs: list[HubJob], max_output_bytes: int
    ) -> dict[str, Any]:
        views = [job.view(max_output_bytes=max_output_bytes) for job in jobs]
        done = all(view["done"] for view in views)
        summary = Counter(view["state"] for view in views)
        result: dict[str, Any] = {
            "broadcast_id": broadcast.id,
            "created_at": utc_iso(broadcast.created_at),
            "patterns": broadcast.patterns,
            "language": broadcast.language,
            "done": done,
            "summary": {**dict(sorted(summary.items())), "skipped": len(broadcast.skipped)},
            "jobs": views,
            "skipped": broadcast.skipped,
        }
        if not done:
            result["next_step"] = (
                f"Call get_broadcast with broadcast_id '{broadcast.id}' to wait for the rest."
            )
        return result

    @staticmethod
    def _validate_code(language: str, mode: str, code: str) -> None:
        if mode == "inprocess" and language != "python":
            raise _invalid("mode=inprocess is only valid for language=python")
        if not code.strip():
            raise _invalid("code must not be empty")

    # ---- operations ---------------------------------------------------------

    def _op_list_targets(
        self, *, origin: str, refresh: bool, online_only: bool, relay: str | None, probe: bool
    ) -> dict[str, Any]:
        snapshot = self.registry.snapshot()
        relays = snapshot.relays
        if relay is not None:
            relays = tuple(item for item in relays if item.name.lower() == relay.strip().lower())
            if not relays:
                raise HubError(
                    f"unknown relay {relay!r}",
                    status=404,
                    code="relay_not_found",
                    details={"relays": [item.name for item in snapshot.relays]},
                )
        targets = [target for item in relays for target in item.targets]
        if probe:
            probes = self.registry.probe_many(targets, refresh=refresh)
        else:
            probes = {target.id: self.registry.cached(target) for target in targets}
        views = [self._target_view(target, probes.get(target.id)) for target in targets]
        counts = Counter(view["status"] for view in views)
        if online_only:
            views = [view for view in views if view["status"] == ONLINE]
        result: dict[str, Any] = {
            "relays_file": self._relays_file_view(snapshot),
            "relays": [self._relay_view(item) for item in relays],
            "counts": {"targets": len(targets), **dict(sorted(counts.items()))},
            "targets": views,
        }
        if not snapshot.relays:
            result["hint"] = (
                f"No relays are hydrated. Write {snapshot.path} with the hydration script in "
                "docs/SCHEDULED_TASKS.md; the hub reloads it automatically."
            )
        return result

    def _op_get_target(self, *, origin: str, target: str, refresh: bool) -> dict[str, Any]:
        resolved = self.registry.resolve(target)
        return self._target_view(resolved, self.registry.probe(resolved, refresh=refresh), full=True)

    def _op_run(
        self,
        *,
        origin: str,
        target: str,
        language: str,
        code: str,
        mode: str,
        timeout_s: float,
        wait_s: float,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        self._validate_code(language, mode, code)
        resolved = self.registry.resolve(target)
        job = self.jobs.start_exec(
            resolved,
            language=language,
            code=code,
            mode=mode,
            timeout_s=timeout_s,
            origin=origin,
        )
        self.jobs.wait(job, wait_s)
        return self._job_view(job, max_output_bytes)

    def _op_get_job(
        self, *, origin: str, job_id: str, wait_s: float, max_output_bytes: int
    ) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        self.jobs.wait(job, wait_s)
        return self._job_view(job, max_output_bytes)

    def _op_read_job_output(
        self, *, origin: str, job_id: str, stream: str, offset: int, limit: int
    ) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job.type != "exec":
            raise _invalid(f"job {job_id!r} is a file transfer and has no output streams")
        return job.read_output(stream, offset, limit)

    def _op_cancel_job(self, *, origin: str, job_id: str) -> dict[str, Any]:
        job = self.jobs.cancel(job_id)
        self.jobs.wait(job, CANCEL_SETTLE_S)
        return self._job_view(job, DEFAULT_MAX_OUTPUT_BYTES)

    def _op_list_jobs(self, *, origin: str, target: str | None, limit: int) -> dict[str, Any]:
        target_id = None
        if target is not None:
            try:
                target_id = self.registry.resolve(target).id
            except TargetLookupError:
                target_id = target.strip()
        jobs = self.jobs.list(target_id=target_id, limit=limit)
        summaries = []
        for job in jobs:
            view = job.view(max_output_bytes=0)
            summaries.append(
                {
                    key: view.get(key)
                    for key in (
                        "job_id",
                        "type",
                        "target",
                        "state",
                        "exit_code",
                        "error",
                        "created_at",
                        "duration_ms",
                        "broadcast_id",
                        "language",
                    )
                    if view.get(key) is not None
                }
            )
        return {"jobs": summaries, "counts": self.jobs.counts()}

    def _op_broadcast(
        self,
        *,
        origin: str,
        targets: list[str],
        language: str,
        code: str,
        mode: str,
        timeout_s: float,
        wait_s: float,
        only_online: bool,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        self._validate_code(language, mode, code)
        matched = self.registry.select(targets)
        if not matched:
            raise HubError(
                f"no targets match {targets!r}",
                status=404,
                code="target_not_found",
                details={"targets": [target.id for target in self.registry.snapshot().targets]},
            )
        eligible = matched
        skipped: list[dict[str, Any]] = []
        if only_online:
            probes = self.registry.probe_many(matched)
            eligible = [target for target in matched if probes[target.id].status == ONLINE]
            skipped = [
                {
                    "target": target.id,
                    "status": probes[target.id].status,
                    "reason": probes[target.id].error,
                }
                for target in matched
                if probes[target.id].status != ONLINE
            ]
        broadcast, jobs = self.jobs.start_broadcast(
            eligible,
            patterns=targets,
            skipped=skipped,
            language=language,
            code=code,
            mode=mode,
            timeout_s=timeout_s,
            origin=origin,
        )
        self.jobs.wait_all(jobs, wait_s)
        return self._broadcast_view(broadcast, jobs, max_output_bytes)

    def _op_get_broadcast(
        self, *, origin: str, broadcast_id: str, wait_s: float, max_output_bytes: int
    ) -> dict[str, Any]:
        broadcast, jobs = self.jobs.get_broadcast(broadcast_id)
        self.jobs.wait_all(jobs, wait_s)
        return self._broadcast_view(broadcast, jobs, max_output_bytes)

    def _op_upload_file(
        self,
        *,
        origin: str,
        target: str,
        local_path: str,
        remote_path: str,
        overwrite: bool,
        wait_s: float,
    ) -> dict[str, Any]:
        local = Path(local_path).expanduser()
        if not local.is_absolute():
            raise _invalid("local_path must be an absolute path on the hub machine")
        if not local.is_file():
            raise HubError(f"local_path is not a file: {local}", status=404, code="local_file_not_found")
        if not remote_path.strip():
            raise _invalid("remote_path must not be empty")
        job = self.jobs.start_transfer(
            self.registry.resolve(target),
            direction="upload",
            local_path=str(local),
            remote_path=remote_path,
            overwrite=overwrite,
            origin=origin,
        )
        self.jobs.wait(job, wait_s)
        return self._job_view(job, 0)

    def _op_download_file(
        self,
        *,
        origin: str,
        target: str,
        remote_path: str,
        local_path: str,
        overwrite: bool,
        wait_s: float,
    ) -> dict[str, Any]:
        local = Path(local_path).expanduser()
        if not local.is_absolute():
            raise _invalid("local_path must be an absolute path on the hub machine")
        if not local.parent.is_dir():
            raise HubError(
                f"local_path's directory does not exist: {local.parent}",
                status=404,
                code="local_directory_not_found",
            )
        if not remote_path.strip():
            raise _invalid("remote_path must not be empty")
        job = self.jobs.start_transfer(
            self.registry.resolve(target),
            direction="download",
            local_path=str(local),
            remote_path=remote_path,
            overwrite=overwrite,
            origin=origin,
        )
        self.jobs.wait(job, wait_s)
        return self._job_view(job, 0)
