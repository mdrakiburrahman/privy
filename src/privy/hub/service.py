"""Transport-neutral hub operations shared by the REST API and the MCP endpoint.

Each :class:`Operation` declares its parameters once; the same declaration
validates arguments, renders MCP tool input schemas, and renders the OpenAPI
document, so the interfaces cannot drift apart.
"""

from __future__ import annotations

import contextlib
import math
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from privy.hub.config import Relay, RelaysSnapshot, Target
from privy.hub.jobs import (
    STREAMS,
    TICK_S,
    Broadcast,
    HubBusyError,
    HubJob,
    JobLookupError,
    JobManager,
    utc_iso,
)
from privy.hub.leases import LeaseError, LeaseStore
from privy.hub.listener_facts import FACETS, ListenerInfoStore, facts_for, facts_for_many, matches
from privy.hub.metrics import HubMetrics
from privy.hub.purpose import MAX_PURPOSE_CHARS, WINDOWS, normalize_purpose
from privy.hub.registry import (
    ONLINE,
    UNKNOWN,
    ProbeResult,
    TargetLookupError,
    TargetRegistry,
    language_from,
    machine_name,
)
from privy.hub.sql_store import StorageError
from privy.hub.sync import validate_source
from privy.transfer import TransferError

LANGUAGES = ("python", "bash", "powershell")
MODES = ("subprocess", "inprocess")
DEFAULT_WAIT_S = 30.0
MAX_WAIT_S = 120.0
DEFAULT_TIMEOUT_S = 600.0
MAX_TIMEOUT_S = 7 * 24 * 3600.0
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024
DEFAULT_BROADCAST_OUTPUT_BYTES = 4 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
DEFAULT_READ_LIMIT = 64 * 1024
MAX_CODE_CHARS = 1024 * 1024
CANCEL_SETTLE_S = 10.0
HEARTBEAT_S = 10.0
PROGRESS_MESSAGE_CHARS = 300

#: ``progress(message, done=None, total=None)`` reports activity while a call waits.
Progress = Callable[..., None]

INSTRUCTIONS = """\
privy hub runs commands on remote machines (privy listeners reached through Azure Relay). No
authentication is needed.
Recommended flow: register_client once per chat (session_id makes registration idempotent), then
acquire_lease with client_id, purpose and arbitrary labels (e.g. repo). Omit target for ANY free group,
or name any member to reserve the whole stack. Separate ANY requests can allocate multiple groups.
Pass lease_id on every run, broadcast, transfer, sync, job poll or terminal call. Usage renews the
30m idle TTL; jobs pin it until completion. Before a long wait, renew_lease with ttl up to 4h.
Release with release_lease and client_id when pushed work is done. On none_available wait for
retry_after_s; on group_leased for a user-named box ask before using someone else's group.
Leases are advisory: unleased commands still run with a lease_warning and count as recent use,
but never renew or take over another client's lease. Lease and client metadata are public and
cooperative, not authentication; do not include secrets. Peek with list_leases or get_lease;
reading leases or filtering job history does not renew them.
1. list_targets shows every machine and whether it is online. Name a machine by its short name
   (devbox01), hybrid connection (devbox01_windows), full id (relay/hyco), or host name.
2. run executes code; language is optional (PowerShell on Windows, Bash elsewhere). It waits up to
   wait_s (default 30) and returns the output so far.
3. While a job is running, call get_job(job_id) again: each call waits up to wait_s and returns only
   the output produced since your previous call, until done is true.
4. broadcast runs one command on many machines; upload_file and download_file copy files.
5. Use purpose on work submissions, e.g. "privy: Implementing HYCO purpose history." Reuse the
   same short sentence across related commands and transfers. A lease supplies its purpose when
   omitted; otherwise omission creates an unknown gap.
   Purpose history resets on restart in file mode; SQL mode shares and persists it. A lease's
   default purpose persists with its lease. Never include secrets.
The same API is plain HTTP at this server's / (a quickstart) and /openapi.json. Commands run with the
listener user's privileges: prefer read-only commands unless the user asked for changes."""


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


@contextlib.contextmanager
def _translated_errors() -> Iterator[None]:
    """Turn lookup and capacity failures into :class:`HubError` with an HTTP status."""
    try:
        yield
    except LeaseError as exc:
        raise HubError(str(exc), status=exc.status, code=exc.code, details=exc.details) from None
    except TargetLookupError as exc:
        raise HubError(
            str(exc), status=404, code="target_not_found", details={"candidates": exc.candidates}
        ) from None
    except JobLookupError as exc:
        raise HubError(str(exc), status=404, code="not_found") from None
    except HubBusyError as exc:
        raise HubError(str(exc), status=429, code="busy") from None
    except StorageError as exc:
        raise HubError(
            "shared storage is unavailable; retry later", status=503, code="storage_unavailable"
        ) from exc


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
        if self.name == "purpose":
            schema["type"] = ["string", "null"]
        if self.name == "ttl":
            schema["type"] = ["string", "number"]
        if self.type == "array":
            schema["items"] = {"type": "string"}
        if self.type == "object":
            schema["additionalProperties"] = {"type": "string"}
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
        if name == "ttl" and type(value) in (int, float):
            if not math.isfinite(value) or value <= 0:
                raise _invalid("ttl seconds must be positive and finite")
            return f"{value:g}s"
        if name == "purpose":
            try:
                return normalize_purpose(value)
            except ValueError as exc:
                raise _invalid(str(exc)) from None
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
        if self.type == "object":
            if not isinstance(value, Mapping) or not all(
                isinstance(key, str) and isinstance(item, str) for key, item in value.items()
            ):
                raise _invalid(f"{name} must be an object with string values")
            return dict(value)
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
    "Machine to use: its short name (devbox01), hybrid connection name (devbox01_windows), full id "
    "'<relay>/<hybrid-connection>', or host name.",
    required=True,
)
_LANGUAGE = Param(
    "language",
    "string",
    "Interpreter. Optional: defaults to powershell on Windows targets and bash elsewhere.",
    enum=LANGUAGES,
)
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
_LEASE_ID = Param("lease_id", "string", "Lease id to associate this call with and renew.")
_REQUIRED_LEASE_ID = Param(
    "lease_id", "string", "Lease id to associate this call with and renew.", required=True
)
_CLIENT_ID = Param("client_id", "string", "Registered lease client id.")
_REQUIRED_CLIENT_ID = Param("client_id", "string", "Registered lease client id.", required=True)
_LABELS = Param("labels", "object", "Free-form string labels.")
_TTL = Param("ttl", "string", "Lease duration, for example 30m or 2h.", max_length=32)
PURPOSE_PARAM = Param(
    "purpose",
    "string",
    'Optional project/task sentence, e.g. "privy: Implementing HYCO purpose history." '
    "Reuse across related work. Whitespace is normalized; omitted/null/blank means unknown and "
    "breaks the preceding purpose group unless a lease supplies its default. Hub-only metadata, "
    "never sent to listeners. History resets on hub restart; lease purpose persists. Do not include secrets.",
    max_length=MAX_PURPOSE_CHARS,
)


def _max_output(default: int) -> Param:
    return Param(
        "max_output_bytes",
        "integer",
        "Return at most this many bytes of new stdout and of new stderr (the newest part).",
        default=default,
        minimum=0,
        maximum=MAX_OUTPUT_BYTES,
    )


def _offset(stream: str) -> Param:
    return Param(
        f"{stream}_offset",
        "integer",
        f"Read {stream} from this byte offset instead of your previous position (0 re-reads from "
        "the start). Leaves the shared position untouched.",
        minimum=0,
    )


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "list_targets",
        "List machines",
        "List every machine (privy listener) the hub can reach with its status: online, offline "
        "(no listener connected), not_found (hybrid connection missing), unauthorized (stale key), "
        "unreachable, or error. Online machines report host, OS, user, privy version, and "
        "languages; os_family, os_version, arch, privy_version, python_version, environment, and "
        "shells are also shown for offline machines from their last report (info_stale, "
        "info_seen_at). Filter on those with os, os_version, environment, arch, privy_version, "
        "python_version, and shell. Status is probed live and cached briefly. Start here.",
        (
            Param(
                "refresh",
                "boolean",
                "Re-probe every machine now instead of using cached status.",
                default=False,
            ),
            Param("online_only", "boolean", "Only return machines that are online.", default=False),
            Param("relay", "string", "Only return machines of this relay."),
            Param(
                "probe",
                "boolean",
                "Probe machines without a fresh cached status. false returns cached status only (fast).",
                default=True,
            ),
            *(
                Param(
                    name,
                    "array",
                    f"Only machines whose {label} matches one of these values (case-insensitive, * globs; "
                    "'Unknown' matches machines that never reported it). Repeat or comma-separate "
                    "alternatives.",
                )
                for name, label in FACETS
            ),
        ),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "get_target",
        "Get machine",
        "Show one machine's live status and full listener details: runtimes, working directory, "
        "user, capabilities, and active jobs.",
        (_TARGET, Param("refresh", "boolean", "Re-probe now instead of using cached status.", default=True)),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "run",
        "Run code on a machine",
        "Run PowerShell, Bash, or Python on one machine. Waits up to wait_s seconds (default 30): "
        "a command that finishes in time returns its exit_code, stdout, and stderr; otherwise state "
        "is 'running' and you call get_job(job_id), which returns only the output produced since "
        "your previous read, until done is true. Runs with the listener user's privileges.",
        (
            _TARGET,
            _CODE,
            PURPOSE_PARAM,
            _LANGUAGE,
            _MODE,
            _TIMEOUT,
            _WAIT,
            _max_output(DEFAULT_MAX_OUTPUT_BYTES),
            _LEASE_ID,
        ),
        read_only=False,
        destructive=True,
    ),
    Operation(
        "get_job",
        "Get the next output of a job",
        "Wait up to wait_s seconds for a job to finish and return the stdout and stderr produced "
        "since your previous run/get_job call for it, plus its state. Call it repeatedly to follow "
        "a long command; stop when done is true. States: queued, running, succeeded, failed "
        "(non-zero exit), timed_out, cancelled, error (the hub could not run it).",
        (
            _JOB_ID,
            _WAIT,
            _max_output(DEFAULT_MAX_OUTPUT_BYTES),
            _offset("stdout"),
            _offset("stderr"),
            _LEASE_ID,
        ),
        read_only=True,
    ),
    Operation(
        "read_job_output",
        "Read job output by offset",
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
            _LEASE_ID,
        ),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "cancel_job",
        "Cancel job",
        "Cancel a queued or running job. Best effort: the listener kills the remote process tree "
        "and a partial file transfer stays resumable.",
        (_JOB_ID, _LEASE_ID),
        read_only=False,
        idempotent=True,
    ),
    Operation(
        "list_jobs",
        "List jobs",
        "List recent hub jobs, newest first, without their output.",
        (
            Param("target", "string", "Only jobs for this machine."),
            _LEASE_ID,
            _CLIENT_ID,
            Param("limit", "integer", "Maximum jobs to return.", default=20, minimum=1, maximum=200),
        ),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "get_purpose_history",
        "Get a machine's purpose history",
        "Newest-first grouped, request-declared purposes and explicit unknown gaps. "
        "In memory only: resets on hub restart. Timestamps are Unix seconds, not execution durations. "
        "Groups include first/last reports and total matching report counts; start/end are clipped to "
        "the requested window. Follow next_cursor for older groups.",
        (
            _TARGET,
            Param("range", "string", "Timeline window.", default="24h", enum=tuple(WINDOWS)),
            Param("cursor", "string", "Opaque next_cursor from a previous page.", max_length=80),
            Param("limit", "integer", "Maximum groups per page.", default=50, minimum=1, maximum=200),
        ),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "broadcast",
        "Run code on many machines",
        "Run the same code on every machine matching the patterns, in parallel. Returns a "
        "broadcast_id, one job per machine (state, exit_code, new output), and skipped machines "
        "with their status. Call get_broadcast to follow jobs that are still running.",
        (
            Param(
                "targets",
                "array",
                "Glob patterns matched against target ids ('dev/*') or hybrid connection names "
                "('*_windows'). Comma-separated in a query string.",
                default=["*"],
            ),
            _CODE,
            PURPOSE_PARAM,
            _LANGUAGE,
            _MODE,
            _TIMEOUT,
            _WAIT,
            Param(
                "only_online",
                "boolean",
                "Skip machines that are not online (uses cached status when fresh).",
                default=True,
            ),
            _max_output(DEFAULT_BROADCAST_OUTPUT_BYTES),
            _LEASE_ID,
        ),
        read_only=False,
        destructive=True,
    ),
    Operation(
        "get_broadcast",
        "Get the next output of a broadcast",
        "Wait up to wait_s seconds for every job of a broadcast and return each job's state and the "
        "output produced since your previous read.",
        (
            Param("broadcast_id", "string", "Broadcast id returned by broadcast.", required=True),
            _WAIT,
            _max_output(DEFAULT_BROADCAST_OUTPUT_BYTES),
            _LEASE_ID,
        ),
        read_only=True,
    ),
    Operation(
        "upload_file",
        "Upload a file to a machine",
        "Copy a file from this machine to a target in resumable, SHA-256 verified chunks. Runs as "
        "a job: if it is not done within wait_s, continue with get_job.",
        (
            _TARGET,
            PURPOSE_PARAM,
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
            _LEASE_ID,
        ),
        read_only=False,
        destructive=True,
    ),
    Operation(
        "download_file",
        "Download a file from a machine",
        "Copy a file from a target to this machine in resumable, SHA-256 verified chunks. Runs as "
        "a job: if it is not done within wait_s, continue with get_job.",
        (
            _TARGET,
            PURPOSE_PARAM,
            Param("remote_path", "string", "Source file path on the target.", required=True),
            Param("local_path", "string", "Absolute destination path on this machine.", required=True),
            Param("overwrite", "boolean", "Replace an existing destination file.", default=False),
            _WAIT,
            _LEASE_ID,
        ),
        read_only=False,
        destructive=True,
    ),
    Operation(
        "register_client",
        "Register a lease client",
        "Register or retrieve a client identity used to acquire and manage leases. Registration is "
        "idempotent for a session_id.",
        (
            Param("name", "string", "Chat title or short task name.", required=True, max_length=200),
            Param(
                "kind", "string", "Client type, such as copilot-app, copilot-cli, service, script, or human."
            ),
            Param("host", "string", "Client host name."),
            Param(
                "session_id",
                "string",
                "Copilot session identifier; registration is idempotent on this value.",
            ),
            _LABELS,
        ),
        read_only=False,
        idempotent=True,
    ),
    Operation(
        "list_clients",
        "List lease clients",
        "List registered lease clients, optionally limiting the result to clients with active leases.",
        (Param("active", "boolean", "Only return clients currently holding active leases.", default=False),),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "get_client",
        "Get a lease client",
        "Show one client and the leases it holds.",
        (_REQUIRED_CLIENT_ID,),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "delete_client",
        "Unregister a lease client",
        "Unregister a client and release its leases.",
        (_REQUIRED_CLIENT_ID,),
        read_only=False,
        destructive=True,
        idempotent=True,
    ),
    Operation(
        "list_groups",
        "List lease groups",
        "List lease groups with their members, availability, holder, and cooldown information.",
        (),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "acquire_lease",
        "Acquire a lease",
        "Acquire an advisory lease on a target or group. Omit target to select an available group.",
        (
            _REQUIRED_CLIENT_ID,
            Param("target", "string", "Target member or group name; omit to acquire any available group."),
            _LABELS,
            PURPOSE_PARAM,
            _TTL,
        ),
        read_only=False,
        destructive=True,
    ),
    Operation(
        "list_leases",
        "List leases",
        "List leases, optionally filtered by client, group, repeated key=value labels, or state.",
        (
            _CLIENT_ID,
            Param("group", "string", "Only leases for this group."),
            Param("label", "array", "Match each key=value label; repeat to require multiple labels."),
            Param(
                "state",
                "string",
                "Lease state to include.",
                default="active",
                enum=("active", "expired", "released", "all"),
            ),
        ),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "get_lease",
        "Get a lease",
        "Inspect a lease, including its members, labels, timers, event history, and linked jobs.",
        (_REQUIRED_LEASE_ID,),
        read_only=True,
        idempotent=True,
    ),
    Operation(
        "renew_lease",
        "Renew a lease",
        "Renew a lease held by the specified client, optionally changing its TTL.",
        (_REQUIRED_LEASE_ID, _REQUIRED_CLIENT_ID, _TTL),
        read_only=False,
        idempotent=True,
    ),
    Operation(
        "release_lease",
        "Release a lease",
        "Release a lease, skipping its group's cooldown. When client_id is supplied, only its holder may release it.",
        (_REQUIRED_LEASE_ID, _CLIENT_ID),
        read_only=False,
        destructive=True,
        idempotent=True,
    ),
)
OPERATIONS_BY_NAME = {operation.name: operation for operation in OPERATIONS}
SYNC_OPERATION = Operation(
    "sync_files",
    "Sync a local file or folder into a machine (REST only)",
    "Asynchronously copy an absolute hub-local file or folder to an absolute target path. "
    "destination_type=directory copies folder contents or places a single file under its basename; "
    "destination_type=file uses an exact filename and requires a single source file. "
    "Create missing directories, skip SHA-256-identical files, replace changed files by default, "
    "and never delete remote-only files. Links and special files are rejected. "
    "At most 10,000 manifest entries and 1 MiB serialized metadata. "
    "Returns 202 and a job Location; poll/cancel with existing job endpoints. "
    "Failure/cancellation leaves completed files intact; no whole-tree rollback. REST only, not an MCP tool.",
    (
        _TARGET,
        PURPOSE_PARAM,
        _LEASE_ID,
        Param(
            "local_path",
            "string",
            "Absolute source file or folder on the hub machine.",
            True,
            max_length=4096,
        ),
        Param("remote_path", "string", "Absolute destination on the target machine.", True, max_length=4096),
        Param(
            "destination_type",
            "string",
            "Interpret remote_path explicitly.",
            True,
            enum=("directory", "file"),
        ),
        Param(
            "overwrite", "boolean", "Replace changed files; identical files are always skipped.", default=True
        ),
    ),
    read_only=False,
    destructive=True,
)
FORCE_RELEASE_OPERATION = Operation(
    "force_release_lease",
    "Force-release a lease (REST dashboard only)",
    "Force-release a lease without holder credentials. Restricted to same-origin dashboard requests carrying "
    "X-Privy-UI: 1; not exposed over MCP. Running jobs are not stopped.",
    (_REQUIRED_LEASE_ID,),
    read_only=False,
    destructive=True,
)
#: Operations that wait for jobs and can report progress while they do.
PROGRESS_OPERATIONS = frozenset(
    {"run", "get_job", "broadcast", "get_broadcast", "upload_file", "download_file"}
)


def _listener_summary(probe: ProbeResult | None) -> dict[str, Any]:
    if probe is None or probe.status != ONLINE:
        return {}
    if probe.info is None:
        return {"details": "older listener without host details; re-download privy.exe on it"}
    info = probe.info
    platform = info.get("platform") or {}
    capabilities = info.get("capabilities") or {}
    summary = {
        "host": info.get("hostname"),
        "os": platform.get("description") or platform.get("system"),
        "user": info.get("user"),
        "languages": capabilities.get("languages"),
        "privy_version": info.get("privy_version"),
        "running_jobs": (info.get("jobs") or {}).get("running"),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


class _ProgressReporter:
    """Turn job output the caller has not read yet into rate-limited progress messages."""

    def __init__(self, jobs: Sequence[HubJob], progress: Progress, *, prefix_targets: bool) -> None:
        self._jobs = list(jobs)
        self._progress = progress
        self._prefix_targets = prefix_targets
        # Start at each job's read cursor: output produced before this call began
        # (even before this reporter existed) is still news to the caller.
        self._marks: dict[str, dict[str, int]] = {}
        for job in jobs:
            with job.cond:
                shared_cursor = getattr(job, "read_cursor", None)
                self._marks[job.id] = shared_cursor() if shared_cursor is not None else dict(job.cursor)
        self._started = time.monotonic()
        #: When a message was last sent; the first output goes out immediately.
        self._last = float("-inf")
        self._last_bytes = 0

    def __call__(self) -> None:
        now = time.monotonic()
        if now - self._last < TICK_S:
            return
        parts: list[str] = []
        transfer: tuple[int, int] | None = None
        for job in self._jobs:
            if job.type != "exec":
                if job.progress is not None:
                    transfer = job.progress
                continue
            for stream in STREAMS:
                begin, data = job.peek(stream, self._marks[job.id][stream], 4096)
                if not data:
                    continue
                self._marks[job.id][stream] = begin + len(data)
                line = _last_line(data.decode("utf-8", "replace"))
                if line:
                    parts.append(
                        f"[{machine_name(job.target.path)}] {line}" if self._prefix_targets else line
                    )
        if transfer is not None and transfer[0] > self._last_bytes:
            self._last_bytes = transfer[0]
            self._last = now
            self._progress(f"{transfer[0]} of {transfer[1]} bytes", done=transfer[0], total=transfer[1])
        elif parts:
            self._last = now
            self._progress("  ·  ".join(parts)[-PROGRESS_MESSAGE_CHARS:])
        elif now - max(self._last, self._started) >= HEARTBEAT_S and any(not job.done for job in self._jobs):
            self._last = now
            self._progress(f"still running ({int(now - self._started)}s)")


class HubService:
    """The hub's operations, independent of HTTP or MCP framing."""

    def __init__(
        self,
        *,
        registry: TargetRegistry,
        jobs: JobManager,
        version: str,
        started_at: float | None = None,
        metrics: HubMetrics | None = None,
        leases: LeaseStore | None = None,
        listener_infos: ListenerInfoStore | None = None,
    ) -> None:
        self.registry = registry
        self.jobs = jobs
        self.version = version
        self.started_at = time.time() if started_at is None else started_at
        self.metrics = metrics if metrics is not None else HubMetrics()
        self.leases = leases if leases is not None else LeaseStore(registry, audit=jobs.audit)
        self.listener_infos = listener_infos if listener_infos is not None else ListenerInfoStore()
        jobs.on_job = self._job_added
        jobs.on_finish = self.leases.job_finished
        registry.on_probe = self._probed
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            operation.name: getattr(self, f"_op_{operation.name}") for operation in OPERATIONS
        }

    def _probed(self, target: Target, result: ProbeResult) -> None:
        self.metrics.probe(target, result)
        self.listener_infos.record(target, result)

    def listener_facts(self, target_id: str, path: str, probe: ProbeResult | None) -> dict[str, Any]:
        """OS and version facts, live or last known (see :mod:`privy.hub.listener_facts`)."""
        return facts_for(self.listener_infos, target_id, path, probe)

    def listener_facts_many(
        self, targets: Sequence[Target], probes: Mapping[str, ProbeResult | None]
    ) -> dict[str, dict[str, Any]]:
        return facts_for_many(self.listener_infos, targets, probes)

    def _job_added(self, job: HubJob) -> None:
        self.leases.job_started(job)
        self.metrics.job_added(job)

    def lease_use(self, target: str, lease_id: str | None, *, origin: str) -> dict[str, Any]:
        with _translated_errors():
            return self.leases.use(target, lease_id, origin=origin)

    def invoke(
        self,
        name: str,
        raw_args: Mapping[str, Any] | None,
        *,
        origin: str,
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        operation = OPERATIONS_BY_NAME.get(name)
        if operation is None:
            raise HubError(f"unknown operation {name!r}", status=404, code="unknown_operation")
        if raw_args is not None and not isinstance(raw_args, Mapping):
            raise _invalid("arguments must be an object")
        args = operation.parse(raw_args or {})
        if name in PROGRESS_OPERATIONS:
            args["progress"] = progress
        with _translated_errors():
            return self._handlers[name](origin=origin, **args)

    def start_run(self, raw_args: Mapping[str, Any] | None, *, origin: str) -> HubJob:
        """Validate ``run`` arguments and start the job without waiting (for live streams)."""
        args = OPERATIONS_BY_NAME["run"].parse(raw_args or {})
        with _translated_errors():
            return self._start_exec(origin=origin, **args)

    def start_sync(self, raw_args: Mapping[str, Any], *, origin: str) -> dict[str, Any]:
        if any(value is None for name, value in raw_args.items() if name not in ("purpose", "lease_id")):
            raise _invalid("sync arguments must not be null")
        args = SYNC_OPERATION.parse(raw_args)
        if not args["target"].strip():
            raise _invalid("target must not be empty")
        if not args["remote_path"].strip() or "\0" in args["remote_path"] or "\0" in args["local_path"]:
            raise _invalid("local_path and remote_path must be non-empty paths without null bytes")
        try:
            local = validate_source(args["local_path"], args["destination_type"])
        except TransferError as exc:
            status = 404 if exc.code == "local_source_unavailable" else 400
            raise HubError(str(exc), status=status, code=exc.code) from None
        args["local_path"] = str(local)
        with _translated_errors():
            job = self.jobs.start_sync(self.registry.resolve(args.pop("target")), origin=origin, **args)
        return self._job_view(job, 0)

    def job(self, job_id: str, *, lease_id: str | None = None, origin: str = "rest") -> HubJob:
        return self.job_usage(job_id, lease_id=lease_id, origin=origin)[0]

    def job_usage(
        self, job_id: str, *, lease_id: str | None = None, origin: str = "rest"
    ) -> tuple[HubJob, dict[str, Any]]:
        with _translated_errors():
            job = self.jobs.get(job_id)
            usage = self.leases.use(
                job.target.id, lease_id if job.lease_id == lease_id else None, origin=origin
            )
            return job, usage

    def summary(self) -> dict[str, Any]:
        snapshot = self.registry.snapshot()
        result = {
            "version": self.version,
            "uptime_s": int(time.time() - self.started_at),
            "relays_file": self._relays_file_view(snapshot),
            "relays": len(snapshot.relays),
            "targets": len(snapshot.targets),
            "jobs": self.jobs.counts(),
        }
        backend = getattr(self.jobs, "backend", None)
        if backend is not None:
            result["storage"] = backend.status()
        return result

    # ---- views --------------------------------------------------------------

    @staticmethod
    def _relays_file_view(snapshot: RelaysSnapshot) -> dict[str, Any]:
        view: dict[str, Any] = {"path": str(snapshot.path), "exists": snapshot.file_exists}
        if snapshot.loaded_at is not None:
            view["loaded_at"] = utc_iso(snapshot.loaded_at)
        if snapshot.error:
            view["error"] = snapshot.error
        return view

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

    def _target_view(
        self,
        target: Target,
        probe: ProbeResult | None,
        *,
        full: bool = False,
        facts: dict[str, Any] | None = None,
        lease_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        view: dict[str, Any] = {
            "id": target.id,
            "machine": machine_name(target.path),
            "status": probe.status if probe else UNKNOWN,
        }
        view.update(_listener_summary(probe))
        view.update(lease_summary if lease_summary is not None else self.leases.target_summary(target.id))
        if probe is not None and probe.status == ONLINE:
            language = language_from(probe.info, target.path)
            if language:
                view["default_language"] = language
        if probe is not None and probe.error:
            view["reason"] = probe.error
        if probe is not None and probe.latency_ms is not None:
            view["latency_ms"] = probe.latency_ms
        if target.description:
            view["description"] = target.description
        facts = dict(facts) if facts is not None else self.listener_facts(target.id, target.path, probe)
        if facts["info_seen_at"] is None:
            facts.pop("info_stale")
        view.update(
            {
                key: value
                for key, value in facts.items()
                if value is not None and key != "info_seen_at_ts" and key not in view
            }
        )
        if full:
            view.update(relay=target.relay, namespace=target.namespace, hybrid_connection=target.path)
            if probe is not None:
                view["checked_at"] = utc_iso(probe.checked_at)
                if probe.info is not None:
                    view["listener_info"] = probe.info
        return view

    @staticmethod
    def _job_view(
        job: HubJob,
        max_output_bytes: int,
        *,
        offsets: Mapping[str, int | None] | None = None,
    ) -> dict[str, Any]:
        view = job.view(max_output_bytes=max_output_bytes, offsets=offsets)
        if not view["done"]:
            view["next_step"] = (
                f"Still running. Call get_job with job_id '{job.id}' (REST: GET /v1/jobs/{job.id}) for "
                "the next output; each call returns only output produced since the previous one."
            )
        return view

    def _broadcast_view(
        self, broadcast: Broadcast, jobs: list[HubJob], max_output_bytes: int
    ) -> dict[str, Any]:
        views = [job.view(max_output_bytes=max_output_bytes) for job in jobs]
        done = all(view["done"] for view in views)
        summary = Counter(view["state"] for view in views)
        result: dict[str, Any] = {
            "broadcast_id": broadcast.id,
            "patterns": broadcast.patterns,
            "done": done,
            "summary": {**dict(sorted(summary.items())), "skipped": len(broadcast.skipped)},
            "jobs": views,
        }
        if broadcast.skipped:
            result["skipped"] = broadcast.skipped
        if not done:
            result["next_step"] = (
                f"Some jobs are still running. Call get_broadcast with broadcast_id '{broadcast.id}' "
                f"(REST: GET /v1/broadcasts/{broadcast.id}) for their next output."
            )
        return result

    # ---- helpers ------------------------------------------------------------

    def _language_for(self, target: Target, language: str | None, mode: str) -> str:
        if language is not None:
            return language
        if mode == "inprocess":
            return "python"
        inferred = self.registry.default_language(target)
        if inferred is None:
            raise _invalid(
                f"language is required for {target.id}: its OS is unknown (it may be offline); "
                "pass language=powershell, bash, or python"
            )
        return inferred

    @staticmethod
    def _validate_code(language: str | None, mode: str, code: str) -> None:
        if mode == "inprocess" and language not in (None, "python"):
            raise _invalid("mode=inprocess is only valid for language=python")
        if not code.strip():
            raise _invalid("code must not be empty")

    def _start_exec(
        self,
        *,
        origin: str,
        target: str,
        code: str,
        language: str | None,
        mode: str,
        timeout_s: float,
        purpose: str | None = None,
        lease_id: str | None = None,
        **_: Any,
    ) -> HubJob:
        self._validate_code(language, mode, code)
        resolved = self.registry.resolve(target)
        return self.jobs.start_exec(
            resolved,
            language=self._language_for(resolved, language, mode),
            code=code,
            mode=mode,
            timeout_s=timeout_s,
            origin=origin,
            purpose=purpose,
            lease_id=lease_id,
        )

    def _wait(
        self, jobs: Sequence[HubJob], wait_s: float, progress: Progress | None, *, prefix: bool
    ) -> None:
        reporter = _ProgressReporter(jobs, progress, prefix_targets=prefix) if progress else None
        self.jobs.wait_all(jobs, wait_s, reporter)

    # ---- operations ---------------------------------------------------------

    def _op_list_targets(
        self,
        *,
        origin: str,
        refresh: bool,
        online_only: bool,
        relay: str | None,
        probe: bool,
        **facets: list[str] | None,
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
        facts = self.listener_facts_many(targets, probes)
        lease_summaries = self.leases.target_summaries([target.id for target in targets])
        views = [
            self._target_view(
                target,
                probes.get(target.id),
                facts=facts[target.id],
                lease_summary=lease_summaries[target.id],
            )
            for target in targets
        ]
        counts = Counter(view["status"] for view in views)
        if online_only:
            views = [view for view in views if view["status"] == ONLINE]
        filters = {name: values for name, values in facets.items() if values}
        if filters:
            views = [view for view in views if matches(facts[view["id"]], filters)]
        result: dict[str, Any] = {
            "counts": {"targets": len(targets), **dict(sorted(counts.items()))},
            "targets": views,
            "relays": [self._relay_view(item) for item in relays],
            "relays_file": self._relays_file_view(snapshot),
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
        code: str,
        language: str | None,
        mode: str,
        timeout_s: float,
        wait_s: float,
        max_output_bytes: int,
        progress: Progress | None,
        purpose: str | None,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        job = self._start_exec(
            origin=origin,
            target=target,
            code=code,
            language=language,
            mode=mode,
            timeout_s=timeout_s,
            purpose=purpose,
            lease_id=lease_id,
        )
        self._wait([job], wait_s, progress, prefix=False)
        return self._job_view(job, max_output_bytes)

    def _op_get_job(
        self,
        *,
        origin: str,
        job_id: str,
        wait_s: float,
        max_output_bytes: int,
        stdout_offset: int | None,
        stderr_offset: int | None,
        progress: Progress | None,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        job, usage = self.job_usage(job_id, lease_id=lease_id, origin=origin)
        self._wait([job], wait_s, progress, prefix=False)
        offsets = None
        if stdout_offset is not None or stderr_offset is not None:
            offsets = {"stdout": stdout_offset, "stderr": stderr_offset}
        return {
            **{
                k: v
                for k, v in self._job_view(job, max_output_bytes, offsets=offsets).items()
                if k != "lease_warning"
            },
            **{k: v for k, v in usage.items() if k == "lease_warning"},
        }

    def _op_read_job_output(
        self, *, origin: str, job_id: str, stream: str, offset: int, limit: int, lease_id: str | None = None
    ) -> dict[str, Any]:
        job, usage = self.job_usage(job_id, lease_id=lease_id, origin=origin)
        if job.type != "exec":
            raise _invalid(f"job {job_id!r} is a file transfer and has no output streams")
        return {
            **job.read_output(stream, offset, limit),
            **{k: v for k, v in usage.items() if k == "lease_warning"},
        }

    def _op_cancel_job(self, *, origin: str, job_id: str, lease_id: str | None = None) -> dict[str, Any]:
        current = self.jobs.get(job_id)
        usage = self.leases.use(current.target.id, lease_id, origin=origin)
        job = self.jobs.cancel(job_id)
        self.jobs.wait(job, CANCEL_SETTLE_S)
        return {
            **{
                k: v for k, v in self._job_view(job, DEFAULT_MAX_OUTPUT_BYTES).items() if k != "lease_warning"
            },
            **{k: v for k, v in usage.items() if k == "lease_warning"},
        }

    def _op_list_jobs(
        self,
        *,
        origin: str,
        target: str | None,
        limit: int,
        lease_id: str | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        target_id = None
        if target is not None:
            try:
                target_id = self.registry.resolve(target).id
            except TargetLookupError:
                target_id = target.strip()
        jobs = self.jobs.list(target_id=target_id, limit=limit, lease_id=lease_id, client_id=client_id)
        return {"jobs": [job.summary(include_created=True) for job in jobs], "counts": self.jobs.counts()}

    def _op_get_purpose_history(
        self, *, origin: str, target: str, range: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        resolved = self.registry.resolve(target)
        try:
            return self.metrics.purposes.history(resolved.id, range_name=range, cursor=cursor, limit=limit)
        except ValueError as exc:
            raise _invalid(str(exc)) from None

    def _op_broadcast(
        self,
        *,
        origin: str,
        targets: list[str],
        code: str,
        language: str | None,
        mode: str,
        timeout_s: float,
        wait_s: float,
        only_online: bool,
        purpose: str | None,
        max_output_bytes: int,
        progress: Progress | None,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        patterns = [part.strip() for pattern in targets for part in pattern.split(",") if part.strip()]
        self._validate_code(language, mode, code)
        matched = self.registry.select(patterns)
        if not matched:
            raise HubError(
                f"no targets match {patterns!r}",
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
                {"target": target.id, "status": probes[target.id].status, "reason": probes[target.id].error}
                for target in matched
                if probes[target.id].status != ONLINE
            ]
        runs: list[tuple[Target, str]] = []
        if lease_id:
            self.leases.validate_targets(lease_id, [target.id for target in matched])
        for target in eligible:
            try:
                runs.append((target, self._language_for(target, language, mode)))
            except HubError as exc:
                skipped.append({"target": target.id, "status": "unknown_os", "reason": str(exc)})
        broadcast, jobs = self.jobs.start_broadcast(
            runs,
            patterns=patterns,
            skipped=skipped,
            code=code,
            mode=mode,
            timeout_s=timeout_s,
            origin=origin,
            purpose=purpose,
            lease_id=lease_id,
        )
        self._wait(jobs, wait_s, progress, prefix=True)
        return self._broadcast_view(broadcast, jobs, max_output_bytes)

    def _op_get_broadcast(
        self,
        *,
        origin: str,
        broadcast_id: str,
        wait_s: float,
        max_output_bytes: int,
        progress: Progress | None,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        broadcast, jobs = self.jobs.get_broadcast(broadcast_id)
        if lease_id:
            self.leases.validate_targets(lease_id, [job.target.id for job in jobs])
        for job in jobs:
            self.leases.use(job.target.id, lease_id, origin=origin)
        self._wait(jobs, wait_s, progress, prefix=True)
        return self._broadcast_view(broadcast, jobs, max_output_bytes)

    def _transfer(
        self,
        *,
        direction: str,
        purpose: str | None,
        origin: str,
        target: str,
        local: Path,
        remote_path: str,
        overwrite: bool,
        wait_s: float,
        progress: Progress | None,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        if not remote_path.strip():
            raise _invalid("remote_path must not be empty")
        job = self.jobs.start_transfer(
            self.registry.resolve(target),
            direction=direction,
            local_path=str(local),
            remote_path=remote_path,
            overwrite=overwrite,
            origin=origin,
            purpose=purpose,
            lease_id=lease_id,
        )
        self._wait([job], wait_s, progress, prefix=False)
        return self._job_view(job, 0)

    def _op_upload_file(
        self,
        *,
        origin: str,
        purpose: str | None,
        target: str,
        local_path: str,
        remote_path: str,
        overwrite: bool,
        wait_s: float,
        progress: Progress | None,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        local = Path(local_path).expanduser()
        if not local.is_absolute():
            raise _invalid("local_path must be an absolute path on the hub machine")
        if not local.is_file():
            raise HubError(f"local_path is not a file: {local}", status=404, code="local_file_not_found")
        return self._transfer(
            direction="upload",
            purpose=purpose,
            origin=origin,
            target=target,
            local=local,
            remote_path=remote_path,
            overwrite=overwrite,
            wait_s=wait_s,
            progress=progress,
            lease_id=lease_id,
        )

    def _op_download_file(
        self,
        *,
        origin: str,
        purpose: str | None,
        target: str,
        remote_path: str,
        local_path: str,
        overwrite: bool,
        wait_s: float,
        progress: Progress | None,
        lease_id: str | None = None,
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
        return self._transfer(
            direction="download",
            purpose=purpose,
            origin=origin,
            target=target,
            local=local,
            remote_path=remote_path,
            overwrite=overwrite,
            wait_s=wait_s,
            progress=progress,
            lease_id=lease_id,
        )

    def _op_register_client(
        self,
        *,
        origin: str,
        name: str,
        kind: str | None,
        host: str | None,
        session_id: str | None,
        labels: dict[str, str] | None,
    ) -> dict[str, Any]:
        with _translated_errors():
            return self.leases.register_client(
                name=name, kind=kind, host=host, session_id=session_id, labels=labels
            )

    def _op_list_clients(self, *, origin: str, active: bool) -> list[dict[str, Any]]:
        with _translated_errors():
            return self.leases.list_clients(active=active)

    def _op_get_client(self, *, origin: str, client_id: str) -> dict[str, Any]:
        with _translated_errors():
            return self.leases.get_client(client_id)

    def _op_delete_client(self, *, origin: str, client_id: str) -> dict[str, Any]:
        with _translated_errors():
            return self.leases.delete_client(client_id)

    def _op_list_groups(self, *, origin: str) -> list[dict[str, Any]]:
        with _translated_errors():
            return self.leases.groups(probe=True)

    def _op_acquire_lease(
        self,
        *,
        origin: str,
        client_id: str,
        target: str | None,
        labels: dict[str, str] | None,
        purpose: str | None,
        ttl: str | None,
    ) -> dict[str, Any]:
        with _translated_errors():
            return self.leases.acquire(
                client_id, target=target, labels=labels, purpose=purpose, ttl=ttl, manual=origin == "ui"
            )

    def _op_list_leases(
        self,
        *,
        origin: str,
        client_id: str | None,
        group: str | None,
        label: list[str] | None,
        state: str,
    ) -> list[dict[str, Any]]:
        with _translated_errors():
            return self.leases.list_leases(client_id=client_id, group=group, label=label, state=state)

    def _op_get_lease(self, *, origin: str, lease_id: str) -> dict[str, Any]:
        with _translated_errors():
            return self.leases.get_lease(lease_id)

    def _op_renew_lease(
        self, *, origin: str, lease_id: str, client_id: str, ttl: str | None
    ) -> dict[str, Any]:
        with _translated_errors():
            return self.leases.renew(lease_id, client_id, ttl=ttl, manual=origin == "ui")

    def _op_release_lease(self, *, origin: str, lease_id: str, client_id: str | None) -> dict[str, Any]:
        with _translated_errors():
            return self.leases.release(lease_id, client_id=client_id)

    def _op_force_release_lease(self, *, origin: str, lease_id: str) -> dict[str, Any]:
        if origin != "rest":
            raise HubError("force-release is REST dashboard only", status=403, code="forbidden")
        with _translated_errors():
            return self.leases.release(lease_id, force=True)
