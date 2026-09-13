import json
import os
import threading

import pytest

from privy.batch import (
    DEFAULT_MAX_PARALLEL,
    BatchCallbackError,
    BatchValidationError,
    CommandSpec,
    parse_batch_manifest,
    run_many,
    validate_commands,
)
from privy.client import ExecResult
from privy.executor import cancel_job, poll_job, submit_job
from privy.protocol import ExecResponse


class LocalJobClient:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.cancelled: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def submit(self, request):
        response = submit_job(request)
        assert response.job_id
        with self._lock:
            self.submitted.append(request.code)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        return response.job_id

    def poll(self, request, job_id, *, wait_s):
        response = poll_job(job_id, wait_s)
        if response.state != "running":
            with self._lock:
                self.active -= 1
        return response.state, ExecResult.from_response(response)

    def cancel(self, request, job_id):
        self.cancelled.append(job_id)
        response = cancel_job(job_id)
        return ExecResult.from_response(response)


def test_parse_batch_manifest():
    manifest = parse_batch_manifest(
        json.dumps(
            {
                "max_parallel": 4,
                "commands": [
                    {"id": "one", "kind": "bash", "code": "echo one"},
                    {
                        "id": "two",
                        "kind": "powershell",
                        "code": "Write-Output two",
                        "depends_on": ["one"],
                    },
                ],
            }
        )
    )

    assert manifest.max_parallel == 4
    assert manifest.commands[1].depends_on == ("one",)
    assert manifest.commands[1].kind == "powershell"


def test_parse_batch_manifest_rejects_inprocess_powershell():
    with pytest.raises(BatchValidationError, match="cannot run powershell inprocess"):
        parse_batch_manifest(
            '{"commands":[{"id":"one","kind":"powershell","code":"echo","mode":"inprocess"}]}'
        )


def test_manifest_defaults_to_32_parallel_jobs():
    manifest = parse_batch_manifest('{"commands":[{"id":"one","kind":"bash","code":"true"}]}')

    assert manifest.max_parallel == DEFAULT_MAX_PARALLEL


@pytest.mark.parametrize(
    ("commands", "message"),
    [
        (
            [
                CommandSpec(id="one", kind="bash", code="true", depends_on=("missing",)),
            ],
            "unknown dependencies",
        ),
        (
            [
                CommandSpec(id="one", kind="bash", code="true", depends_on=("two",)),
                CommandSpec(id="two", kind="bash", code="true", depends_on=("one",)),
            ],
            "cycle",
        ),
        (
            [
                CommandSpec(id="one", kind="bash", code="true"),
                CommandSpec(id="one", kind="bash", code="true"),
            ],
            "duplicate",
        ),
    ],
)
def test_validate_commands_rejects_invalid_graphs(commands, message):
    with pytest.raises(BatchValidationError, match=message):
        validate_commands(commands)


def test_run_many_unlocks_dependencies_and_preserves_input_order():
    client = LocalJobClient()
    commands = [
        CommandSpec(id="a", kind="bash", code="sleep 0.1; echo a"),
        CommandSpec(id="b", kind="python", code="print('b')"),
        CommandSpec(id="c", kind="bash", code="echo c", depends_on=("a",)),
    ]

    result = run_many(client, commands)

    assert result.ok
    assert [outcome.id for outcome in result.outcomes] == ["a", "b", "c"]
    assert [outcome.state for outcome in result.outcomes] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert result.outcomes[0].result.stdout == "a\n"
    assert result.outcomes[1].result.stdout == f"b{os.linesep}"
    assert client.submitted.index("echo c") > client.submitted.index("sleep 0.1; echo a")


def test_failure_skips_dependents_but_continues_independent_branches():
    client = LocalJobClient()
    commands = [
        CommandSpec(id="fail", kind="bash", code="echo fail; exit 7"),
        CommandSpec(id="child", kind="bash", code="echo child", depends_on=("fail",)),
        CommandSpec(id="grandchild", kind="bash", code="echo nope", depends_on=("child",)),
        CommandSpec(id="independent", kind="bash", code="echo independent"),
    ]

    result = run_many(client, commands)
    outcomes = {outcome.id: outcome for outcome in result.outcomes}

    assert not result.ok
    assert outcomes["fail"].state == "failed"
    assert outcomes["fail"].result.exit_code == 7
    assert outcomes["child"].state == "skipped"
    assert outcomes["grandchild"].state == "skipped"
    assert outcomes["independent"].state == "succeeded"
    assert "echo child" not in client.submitted
    assert "echo nope" not in client.submitted


def test_run_many_honours_parallelism_limit():
    client = LocalJobClient()
    commands = [CommandSpec(id=f"job-{index}", kind="bash", code="sleep 0.1") for index in range(6)]

    result = run_many(client, commands, max_parallel=2)

    assert result.ok
    assert client.max_active <= 2


def test_batch_json_omits_binary_result_fields():
    client = LocalJobClient()

    result = run_many(client, [CommandSpec(id="one", kind="bash", code="printf hi")])
    encoded = json.dumps(result.to_dict())

    assert '"stdout": "hi"' in encoded
    assert "stdout_bytes" not in encoded


@pytest.mark.parametrize("with_callback", [False, True])
def test_keyboard_interrupt_cancels_active_jobs(with_callback):
    class InterruptingClient:
        def __init__(self):
            self.cancelled = []

        def submit(self, request):
            return "job-one"

        def poll(self, request, job_id, *, wait_s):
            raise KeyboardInterrupt

        def cancel(self, request, job_id):
            self.cancelled.append(job_id)
            return ExecResult(
                exit_code=0,
                stdout="",
                stderr="",
                stdout_bytes=b"",
                stderr_bytes=b"",
                duration_ms=0,
                timed_out=False,
                error=None,
                job_id=job_id,
            )

    client = InterruptingClient()
    notified = []

    with pytest.raises(KeyboardInterrupt) as caught:
        run_many(
            client,
            [
                CommandSpec(id="one", kind="bash", code="sleep 10"),
                CommandSpec(id="pending", kind="bash", code="true"),
            ],
            max_parallel=1,
            on_command_complete=notified.append if with_callback else None,
        )

    assert client.cancelled == ["job-one"]
    if with_callback:
        assert notified == list(caught.value.batch_result.outcomes)
        assert [outcome.state for outcome in notified] == ["cancelled", "cancelled"]
    else:
        assert notified == []
        assert not hasattr(caught.value, "batch_result")


@pytest.mark.parametrize("with_callback", [False, True])
def test_transient_poll_failures_retry_without_cancelling(with_callback):
    class FlakyPollClient:
        def __init__(self):
            self.polls = 0
            self.cancelled = []

        def submit(self, request):
            return "original-job"

        def poll(self, request, job_id, *, wait_s):
            self.polls += 1
            if self.polls <= 2:
                raise RuntimeError("transient relay disconnect")
            return "done", ExecResult(
                exit_code=0,
                stdout="ok",
                stderr="",
                stdout_bytes=b"ok",
                stderr_bytes=b"",
                duration_ms=1,
                timed_out=False,
                error=None,
                job_id=job_id,
            )

        def cancel(self, request, job_id):
            self.cancelled.append(job_id)

    client = FlakyPollClient()
    notified = []

    result = run_many(
        client,
        [CommandSpec(id="one", kind="bash", code="true")],
        on_command_complete=notified.append if with_callback else None,
    )

    assert result.ok
    assert client.polls == 3
    assert client.cancelled == []
    assert result.outcomes[0].result.job_id == "original-job"
    assert notified == (list(result.outcomes) if with_callback else [])


@pytest.mark.parametrize("with_callback", [False, True])
def test_terminal_poll_failure_retains_job_id_without_cancelling(with_callback):
    class FailedPollClient:
        def __init__(self):
            self.polls = 0
            self.cancelled = []

        def submit(self, request):
            return "reconcile-me"

        def poll(self, request, job_id, *, wait_s):
            self.polls += 1
            raise RuntimeError("relay unavailable")

        def cancel(self, request, job_id):
            self.cancelled.append(job_id)

    client = FailedPollClient()
    notified = []

    result = run_many(
        client,
        [CommandSpec(id="one", kind="bash", code="true")],
        on_command_complete=notified.append if with_callback else None,
    )
    outcome = result.outcomes[0]

    assert outcome.state == "failed"
    assert outcome.result is not None
    assert outcome.result.job_id == "reconcile-me"
    assert outcome.result.error == "poll_transport"
    assert "reconcile-me" in (outcome.error or "")
    assert client.polls == 4
    assert client.cancelled == []
    assert notified == (list(result.outcomes) if with_callback else [])


@pytest.mark.parametrize("callback_fails", [False, True])
def test_callbacks_notify_failed_and_shared_skipped_descendants_once(callback_fails):
    client = LocalJobClient()
    commands = [
        CommandSpec(id="fail", kind="bash", code="printf failed; exit 7"),
        CommandSpec(id="left", kind="bash", code="true", depends_on=("fail",)),
        CommandSpec(id="right", kind="bash", code="true", depends_on=("fail",)),
        CommandSpec(id="join", kind="bash", code="true", depends_on=("left", "right")),
        CommandSpec(id="independent", kind="bash", code="printf ok"),
    ]
    notified = []
    caller = threading.get_ident()

    def completed(outcome):
        assert threading.get_ident() == caller
        assert client._lock.acquire(timeout=1)
        client._lock.release()
        notified.append(outcome)
        if callback_fails and outcome.id == "fail":
            raise OSError("failed command receipt unavailable")

    if callback_fails:
        with pytest.raises(BatchCallbackError) as caught:
            run_many(client, commands, on_command_complete=completed)
        result = caught.value.result
    else:
        result = run_many(client, commands, on_command_complete=completed)

    assert len(notified) == len(commands)
    assert {outcome.id: outcome for outcome in notified} == {
        outcome.id: outcome for outcome in result.outcomes
    }
    assert [outcome.state for outcome in result.outcomes] == [
        "failed", "skipped", "skipped", "skipped", "succeeded",
    ]
    assert result.outcomes[0].result.exit_code == 7
    assert result.outcomes[0].result.stdout == "failed"
    assert result.outcomes[1].skipped_due_to == ("fail",)


@pytest.mark.parametrize(
    ("state", "error", "expected"),
    [("cancelled", "cancelled", "cancelled"), ("missing", "unknown_job", "failed")],
)
def test_callbacks_preserve_remote_terminal_states(state, error, expected):
    class TerminalClient:
        def submit(self, request):
            return "native-job"

        def poll(self, request, job_id, *, wait_s):
            return state, ExecResult.from_response(
                ExecResponse.from_output(
                    exit_code=1, stdout=b"partial", stderr=b"native error",
                    duration_ms=12, error=error, job_id=job_id,
                )
            )

    notified = []
    result = run_many(
        TerminalClient(),
        [CommandSpec(id="one", kind="bash", code="unused")],
        on_command_complete=notified.append,
    )

    assert notified == list(result.outcomes)
    assert notified[0].state == expected
    assert notified[0].result.stdout == "partial"
    assert notified[0].result.error == error
    assert notified[0].result.job_id == "native-job"


def test_callback_on_submit_failure_and_skips():
    class FailedSubmitClient:
        def submit(self, request):
            raise OSError("submit unavailable")

    notified = []
    result = run_many(
        FailedSubmitClient(),
        [
            CommandSpec(id="one", kind="bash", code="unused"),
            CommandSpec(id="child", kind="bash", code="unused", depends_on=("one",)),
        ],
        on_command_complete=notified.append,
    )

    assert notified == list(result.outcomes)
    assert notified[0].error == "submit failed: submit unavailable"
    assert notified[1].skipped_due_to == ("one",)


def test_callback_preserves_poll_deadline_ambiguity(monkeypatch):
    class RunningClient:
        def submit(self, request):
            return "still-needs-reconciliation"

        def poll(self, request, job_id, *, wait_s):
            return "running", ExecResult.from_response(
                ExecResponse.from_output(
                    exit_code=0, stdout=b"", stderr=b"", duration_ms=1, job_id=job_id,
                )
            )

    monkeypatch.setattr("privy.batch.POLL_DEADLINE_GRACE_S", -1)
    notified = []
    result = run_many(
        RunningClient(),
        [CommandSpec(id="one", kind="bash", code="unused", timeout_s=0.01)],
        on_command_complete=notified.append,
    )

    assert notified == list(result.outcomes)
    assert notified[0].state == "failed"
    assert notified[0].result.error == "poll_deadline"
    assert notified[0].result.timed_out
    assert notified[0].result.job_id == "still-needs-reconciliation"


def test_callback_error_drains_native_jobs_and_retains_ordered_results():
    client = LocalJobClient()
    commands = [
        CommandSpec(id="fast", kind="bash", code="printf fast"),
        CommandSpec(id="slow", kind="bash", code="sleep 0.1; printf slow"),
        CommandSpec(id="pending", kind="bash", code="printf never"),
        CommandSpec(id="child", kind="bash", code="printf never-child", depends_on=("fast",)),
    ]
    notified = []
    failure = OSError("result disk full")

    def completed(outcome):
        notified.append(outcome)
        if outcome.id == "fast":
            raise failure

    with pytest.raises(BatchCallbackError) as caught:
        run_many(client, commands, max_parallel=2, on_command_complete=completed)

    error = caught.value
    assert error.__cause__ is failure
    assert error.failures == (("fast", failure),)
    assert [outcome.id for outcome in error.result.outcomes] == [command.id for command in commands]
    assert [outcome.state for outcome in error.result.outcomes] == [
        "succeeded", "succeeded", "cancelled", "cancelled",
    ]
    assert error.result.outcomes[1].result.stdout == "slow"
    assert error.result.outcomes[2].error == "batch_callback_failed"
    assert len(notified) == 4
    assert len({outcome.id for outcome in notified}) == 4
    assert client.submitted == ["printf fast", "sleep 0.1; printf slow"]
    assert client.cancelled == []
    assert client.active == 0


def test_interruption_in_callback_does_not_notify_terminal_command_twice():
    class Client:
        cancelled = []

        def submit(self, request):
            return request.code

        def poll(self, request, job_id, *, wait_s):
            return "done", ExecResult.from_response(
                ExecResponse.from_output(
                    exit_code=0, stdout=b"done", stderr=b"", duration_ms=1, job_id=job_id,
                )
            )

        def cancel(self, request, job_id):
            self.cancelled.append(job_id)

    notified = []

    def completed(outcome):
        notified.append(outcome)
        if outcome.id == "one":
            raise KeyboardInterrupt
        raise OSError("cleanup sink unavailable")

    client = Client()
    with pytest.raises(KeyboardInterrupt) as caught:
        run_many(
            client,
            [
                CommandSpec(id="one", kind="bash", code="one"),
                CommandSpec(id="two", kind="bash", code="two"),
                CommandSpec(id="pending", kind="bash", code="pending"),
            ],
            max_parallel=1,
            on_command_complete=completed,
        )

    assert client.cancelled == []
    assert [outcome.id for outcome in notified] == ["one", "two", "pending"]
    assert [outcome.state for outcome in notified] == ["succeeded", "cancelled", "cancelled"]
    assert caught.value.batch_result.outcomes == tuple(notified)
    assert len(caught.value.batch_callback_failures) == 2


def test_invalid_callback_is_rejected_before_execution():
    client = LocalJobClient()
    with pytest.raises(BatchValidationError, match="must be callable"):
        run_many(
            client,
            [CommandSpec(id="one", kind="bash", code="true")],
            on_command_complete="not callable",
        )
    assert client.submitted == []


def test_invalid_utf8_manifest_is_a_validation_error():
    with pytest.raises(BatchValidationError, match="not valid JSON"):
        parse_batch_manifest(b"\xff")


def test_interruption_while_notifying_pending_commands_preserves_original_interrupt():
    client = LocalJobClient()
    notified = []
    delivery_error = OSError("result unavailable")

    def completed(outcome):
        notified.append(outcome)
        if outcome.id == "one":
            raise delivery_error
        if outcome.id == "pending":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt) as caught:
        run_many(
            client,
            [
                CommandSpec(id="one", kind="bash", code="printf one"),
                CommandSpec(id="pending", kind="bash", code="printf never"),
                CommandSpec(id="other", kind="bash", code="printf also-never"),
            ],
            max_parallel=1,
            on_command_complete=completed,
        )

    assert client.submitted == ["printf one"]
    assert client.active == 0
    assert caught.value.batch_result.outcomes == tuple(notified)
    assert caught.value.batch_callback_failures == (("one", delivery_error),)
    assert [outcome.id for outcome in notified] == ["one", "pending", "other"]
