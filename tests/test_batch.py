import json
import os
import threading

import pytest

from privy.batch import (
    DEFAULT_MAX_PARALLEL,
    BatchValidationError,
    CommandSpec,
    parse_batch_manifest,
    run_many,
    validate_commands,
)
from privy.client import ExecResult
from privy.executor import cancel_job, poll_job, submit_job


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


def test_keyboard_interrupt_cancels_active_jobs():
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

    with pytest.raises(KeyboardInterrupt):
        run_many(client, [CommandSpec(id="one", kind="bash", code="sleep 10")])

    assert client.cancelled == ["job-one"]
