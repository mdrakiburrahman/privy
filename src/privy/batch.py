"""Client-side dependency graph scheduling over privy's async job API."""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from privy.protocol import DEFAULT_POLL_WAIT_S, DEFAULT_TIMEOUT_S, ExecRequest, Kind, Mode

if TYPE_CHECKING:
    from privy.client import ExecResult, RelayClient

DEFAULT_MAX_PARALLEL = 32
POLL_RETRY_LIMIT = 3
POLL_RETRY_BACKOFF_S = 0.25
POLL_DEADLINE_GRACE_S = 60.0
log = logging.getLogger("privy.batch")

CommandState = Literal["pending", "running", "succeeded", "failed", "skipped", "cancelled"]


class BatchValidationError(ValueError):
    """A command graph or JSON manifest is invalid."""


@dataclass(frozen=True)
class CommandSpec:
    id: str
    kind: Kind
    code: str
    mode: Mode = "subprocess"
    timeout_s: float = DEFAULT_TIMEOUT_S
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise BatchValidationError("command id must be a non-empty string")
        if self.kind not in ("python", "bash", "powershell"):
            raise BatchValidationError(f"command {self.id!r} kind must be 'python', 'bash', or 'powershell'")
        if not isinstance(self.code, str):
            raise BatchValidationError(f"command {self.id!r} code must be a string")
        if self.mode not in ("subprocess", "inprocess"):
            raise BatchValidationError(f"command {self.id!r} mode must be 'subprocess' or 'inprocess'")
        if self.kind != "python" and self.mode != "subprocess":
            raise BatchValidationError(f"command {self.id!r} cannot run {self.kind} inprocess")
        _positive_number(self.timeout_s, f"command {self.id!r} timeout_s")
        if not isinstance(self.depends_on, tuple) or not all(
            isinstance(item, str) and item for item in self.depends_on
        ):
            raise BatchValidationError(f"command {self.id!r} depends_on must be a tuple of IDs")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise BatchValidationError(f"command {self.id!r} has duplicate dependencies")
        if self.id in self.depends_on:
            raise BatchValidationError(f"command {self.id!r} cannot depend on itself")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CommandSpec:
        command_id = value.get("id")
        if not isinstance(command_id, str) or not command_id.strip():
            raise BatchValidationError("each command requires a non-empty string id")
        kind = value.get("kind")
        if kind not in ("python", "bash", "powershell"):
            raise BatchValidationError(
                f"command {command_id!r} kind must be 'python', 'bash', or 'powershell'"
            )
        code = value.get("code")
        if not isinstance(code, str):
            raise BatchValidationError(f"command {command_id!r} code must be a string")
        mode = value.get("mode", "subprocess")
        if mode not in ("subprocess", "inprocess"):
            raise BatchValidationError(f"command {command_id!r} mode must be 'subprocess' or 'inprocess'")
        if kind != "python" and mode != "subprocess":
            raise BatchValidationError(f"command {command_id!r} cannot run {kind} inprocess")
        timeout_s = _positive_number(
            value.get("timeout_s", DEFAULT_TIMEOUT_S),
            f"command {command_id!r} timeout_s",
        )
        raw_dependencies = value.get("depends_on", [])
        if not isinstance(raw_dependencies, (list, tuple)) or not all(
            isinstance(item, str) and item for item in raw_dependencies
        ):
            raise BatchValidationError(f"command {command_id!r} depends_on must be a list of IDs")
        dependencies = tuple(raw_dependencies)
        if len(set(dependencies)) != len(dependencies):
            raise BatchValidationError(f"command {command_id!r} has duplicate dependencies")
        if command_id in dependencies:
            raise BatchValidationError(f"command {command_id!r} cannot depend on itself")
        return cls(
            id=command_id,
            kind=kind,
            code=code,
            mode=mode,
            timeout_s=timeout_s,
            depends_on=dependencies,
        )

    def to_request(self) -> ExecRequest:
        return ExecRequest(
            kind=self.kind,
            code=self.code,
            mode=self.mode,
            timeout_s=self.timeout_s,
        )


@dataclass(frozen=True)
class CommandOutcome:
    id: str
    state: CommandState
    result: ExecResult | None = None
    error: str | None = None
    skipped_due_to: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "skipped_due_to": list(self.skipped_due_to),
            "result": None,
        }
        if self.result is not None:
            value["result"] = {
                "exit_code": self.result.exit_code,
                "stdout": self.result.stdout,
                "stderr": self.result.stderr,
                "duration_ms": self.result.duration_ms,
                "timed_out": self.result.timed_out,
                "error": self.result.error,
                "job_id": self.result.job_id,
            }
        return value


@dataclass(frozen=True)
class BatchResult:
    outcomes: tuple[CommandOutcome, ...]
    duration_ms: int

    @property
    def ok(self) -> bool:
        return bool(self.outcomes) and all(outcome.state == "succeeded" for outcome in self.outcomes)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "commands": [outcome.to_dict() for outcome in self.outcomes],
        }


CommandCompleteCallback = Callable[[CommandOutcome], None]


class BatchCallbackError(RuntimeError):
    """Completion delivery failed; ``result`` retains the native batch outcomes."""

    def __init__(
        self,
        result: BatchResult,
        failures: tuple[tuple[str, BaseException], ...],
    ) -> None:
        self.result = result
        self.failures = failures
        command_id, cause = failures[0]
        super().__init__(f"command completion callback failed for {command_id!r}: {cause}")


@dataclass(frozen=True)
class BatchManifest:
    commands: tuple[CommandSpec, ...]
    max_parallel: int = DEFAULT_MAX_PARALLEL


@dataclass
class _RunningCommand:
    command: CommandSpec
    job_id: str
    deadline: float
    poll_failures: int = 0


def parse_batch_manifest(raw: str | bytes) -> BatchManifest:
    """Parse and validate the JSON representation accepted by the CLI."""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BatchValidationError(f"batch manifest is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BatchValidationError("batch manifest must be a JSON object")
    raw_commands = value.get("commands")
    if not isinstance(raw_commands, list) or not raw_commands:
        raise BatchValidationError("batch manifest requires a non-empty commands array")
    commands: list[CommandSpec] = []
    for item in raw_commands:
        if not isinstance(item, dict):
            raise BatchValidationError("each batch command must be a JSON object")
        commands.append(CommandSpec.from_dict(item))
    max_parallel = _positive_int(value.get("max_parallel", DEFAULT_MAX_PARALLEL), "max_parallel")
    validated = validate_commands(commands)
    return BatchManifest(commands=validated, max_parallel=max_parallel)


def validate_commands(commands: Iterable[CommandSpec]) -> tuple[CommandSpec, ...]:
    """Validate graph references and reject dependency cycles."""
    ordered = tuple(commands)
    if not ordered:
        raise BatchValidationError("at least one command is required")
    by_id: dict[str, CommandSpec] = {}
    for command in ordered:
        if not isinstance(command, CommandSpec):
            raise BatchValidationError("commands must contain CommandSpec values")
        if command.id in by_id:
            raise BatchValidationError(f"duplicate command id: {command.id!r}")
        by_id[command.id] = command
    for command in ordered:
        missing = [dependency for dependency in command.depends_on if dependency not in by_id]
        if missing:
            raise BatchValidationError(
                f"command {command.id!r} has unknown dependencies: {', '.join(missing)}"
            )

    indegree = {command.id: len(command.depends_on) for command in ordered}
    dependents = _dependents(ordered)
    ready = deque(command.id for command in ordered if indegree[command.id] == 0)
    visited = 0
    while ready:
        command_id = ready.popleft()
        visited += 1
        for dependent_id in dependents[command_id]:
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                ready.append(dependent_id)
    if visited != len(ordered):
        cycle_ids = [command.id for command in ordered if indegree[command.id] > 0]
        raise BatchValidationError("dependency cycle detected involving: " + ", ".join(cycle_ids))
    return ordered


def run_many(
    client: RelayClient,
    commands: Iterable[CommandSpec],
    *,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    on_command_complete: CommandCompleteCallback | None = None,
) -> BatchResult:
    """Run a DAG, optionally delivering each terminal outcome on the caller thread.

    Callback exceptions stop new submissions, but active jobs drain through the
    native poll loop. ``BatchCallbackError.result`` retains every outcome.
    Callbacks should do only small local work, not remote extraction.
    """
    ordered = validate_commands(commands)
    max_parallel = _positive_int(max_parallel, "max_parallel")
    if on_command_complete is not None and not callable(on_command_complete):
        raise BatchValidationError("on_command_complete must be callable")
    started = time.monotonic()
    by_id = {command.id: command for command in ordered}
    dependents = _dependents(ordered)
    remaining = {command.id: len(command.depends_on) for command in ordered}
    ready = deque(command.id for command in ordered if not command.depends_on)
    outcomes: dict[str, CommandOutcome] = {}
    running: dict[str, _RunningCommand] = {}
    polls: dict[Future[tuple[str | None, ExecResult]], str] = {}
    notifications: deque[CommandOutcome] = deque()
    callback_failures: list[tuple[str, BaseException]] = []

    def record(outcome: CommandOutcome) -> None:
        outcomes[outcome.id] = outcome
        if on_command_complete is not None:
            notifications.append(outcome)

    def notify_terminal(*, interruptible: bool = True) -> None:
        while notifications:
            outcome = notifications.popleft()
            assert on_command_complete is not None
            try:
                on_command_complete(outcome)
            except BaseException as exc:
                if interruptible and not isinstance(exc, Exception):
                    raise
                callback_failures.append((outcome.id, exc))
                log.error("Command completion callback failed for %r: %s", outcome.id, exc)

    def batch_result() -> BatchResult:
        return BatchResult(
            outcomes=tuple(outcomes[command.id] for command in ordered),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def cancel_pending(error: str) -> None:
        for command in ordered:
            if command.id not in outcomes:
                record(CommandOutcome(id=command.id, state="cancelled", error=error))

    def schedule_poll(command_id: str, *, delay_s: float = 0.0) -> None:
        active = running[command_id]

        def poll() -> tuple[str | None, ExecResult]:
            if delay_s:
                time.sleep(delay_s)
            return client.poll(
                active.command.to_request(),
                active.job_id,
                wait_s=min(DEFAULT_POLL_WAIT_S, max(1.0, active.command.timeout_s)),
            )

        future = pool.submit(poll)
        polls[future] = command_id

    def skip_descendants(failed_id: str) -> None:
        queue = deque(dependents[failed_id])
        while queue:
            command_id = queue.popleft()
            if command_id in outcomes or command_id in running:
                continue
            causes = tuple(
                dependency
                for dependency in by_id[command_id].depends_on
                if dependency == failed_id
                or outcomes.get(dependency, CommandOutcome(dependency, "pending")).state
                in ("failed", "skipped", "cancelled")
            )
            record(
                CommandOutcome(
                    id=command_id,
                    state="skipped",
                    error="dependency_failed",
                    skipped_due_to=causes or (failed_id,),
                )
            )
            queue.extend(dependents[command_id])

    def finish(command_id: str, outcome: CommandOutcome) -> None:
        record(outcome)
        running.pop(command_id, None)
        if outcome.state != "succeeded":
            skip_descendants(command_id)
        else:
            for dependent_id in dependents[command_id]:
                if dependent_id in outcomes:
                    continue
                remaining[dependent_id] -= 1
                if remaining[dependent_id] == 0:
                    ready.append(dependent_id)
        notify_terminal()

    def cancel_running() -> None:
        for command_id, active in list(running.items()):
            try:
                client.cancel(active.command.to_request(), active.job_id)
            except Exception as exc:
                log.warning(
                    "Failed to cancel job %s for command %s: %s",
                    active.job_id,
                    command_id,
                    exc,
                )
            record(
                CommandOutcome(
                    id=command_id,
                    state="cancelled",
                    error="batch_cancelled",
                )
            )
        running.clear()

    with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="privy-batch-poll") as pool:
        try:
            while (ready and not callback_failures) or running:
                while ready and len(running) < max_parallel and not callback_failures:
                    command_id = ready.popleft()
                    if command_id in outcomes:
                        continue
                    command = by_id[command_id]
                    try:
                        job_id = client.submit(command.to_request())
                    except Exception as exc:
                        finish(
                            command_id,
                            CommandOutcome(
                                id=command_id,
                                state="failed",
                                error=f"submit failed: {exc}",
                            ),
                        )
                        continue
                    running[command_id] = _RunningCommand(
                        command=command,
                        job_id=job_id,
                        deadline=time.monotonic() + command.timeout_s + POLL_DEADLINE_GRACE_S,
                    )
                    schedule_poll(command_id)

                if not running:
                    continue
                completed, _ = wait(tuple(polls), return_when=FIRST_COMPLETED)
                for future in completed:
                    command_id = polls.pop(future)
                    if command_id not in running:
                        continue
                    active = running[command_id]
                    try:
                        state, result = future.result()
                    except Exception as exc:
                        active.poll_failures += 1
                        if active.poll_failures <= POLL_RETRY_LIMIT and time.monotonic() < active.deadline:
                            schedule_poll(
                                command_id,
                                delay_s=min(
                                    POLL_RETRY_BACKOFF_S * (2 ** (active.poll_failures - 1)),
                                    max(0.0, active.deadline - time.monotonic()),
                                ),
                            )
                            continue
                        error = f"poll failed for job {active.job_id}: {exc}"
                        finish(
                            command_id,
                            CommandOutcome(
                                id=command_id,
                                state="failed",
                                result=_transport_failure_result(
                                    active.job_id,
                                    error,
                                    started,
                                ),
                                error=error,
                            ),
                        )
                        continue
                    active.poll_failures = 0
                    if state == "running":
                        if time.monotonic() >= active.deadline:
                            error = f"poll deadline exceeded for job {active.job_id}"
                            finish(
                                command_id,
                                CommandOutcome(
                                    id=command_id,
                                    state="failed",
                                    result=_transport_failure_result(
                                        active.job_id,
                                        error,
                                        started,
                                        timed_out=True,
                                    ),
                                    error=error,
                                ),
                            )
                        else:
                            schedule_poll(command_id)
                        continue
                    if state == "done" and result.ok:
                        finish(
                            command_id,
                            CommandOutcome(id=command_id, state="succeeded", result=result),
                        )
                    else:
                        terminal_state: CommandState = "cancelled" if state == "cancelled" else "failed"
                        finish(
                            command_id,
                            CommandOutcome(
                                id=command_id,
                                state=terminal_state,
                                result=result,
                                error=result.error or f"job ended in state {state!r}",
                            ),
                        )
            if callback_failures:
                cancel_pending("batch_callback_failed")
                notify_terminal()
        except BaseException as exc:
            cancel_running()
            for future in polls:
                future.cancel()
            if on_command_complete is not None:
                cancel_pending("batch_cancelled")
                # Cleanup must reach every active job even if a callback also
                # raises during interruption. Preserve the original exception.
                notify_terminal(interruptible=False)
                exc.batch_result = batch_result()
                exc.batch_callback_failures = tuple(callback_failures)
            raise

    if callback_failures:
        raise BatchCallbackError(batch_result(), tuple(callback_failures)) from callback_failures[0][1]
    return batch_result()


def _transport_failure_result(
    job_id: str,
    error: str,
    batch_started: float,
    *,
    timed_out: bool = False,
) -> ExecResult:
    from privy.client import ExecResult

    return ExecResult(
        exit_code=1,
        stdout="",
        stderr=error + "\n",
        stdout_bytes=b"",
        stderr_bytes=(error + "\n").encode(),
        duration_ms=int((time.monotonic() - batch_started) * 1000),
        timed_out=timed_out,
        error="poll_transport" if not timed_out else "poll_deadline",
        job_id=job_id,
    )


def _dependents(commands: Iterable[CommandSpec]) -> dict[str, list[str]]:
    values = tuple(commands)
    result = {command.id: [] for command in values}
    for command in values:
        for dependency in command.depends_on:
            result[dependency].append(command.id)
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BatchValidationError(f"{name} must be a positive integer")
    return value


def _positive_number(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise BatchValidationError(f"{name} must be a positive number")
    return float(value)
