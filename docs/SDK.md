# Python SDK

## Create a client

Use a short-lived token:

```python
from privy import RelayClient

client = RelayClient(
    namespace="my-relay",
    path="privy",
    token="SharedAccessSignature ...",
)
```

Long-lived processes can pass a provider called before every request:

```python
client = RelayClient(
    namespace="my-relay",
    path="privy",
    token=lambda: broker.current_token(),
)
```

Key-based compatibility:

```python
client = RelayClient(
    namespace="my-relay",
    path="privy",
    keyrule="privy-send",
    key="...",
    ttl_seconds=3600,
)
```

## Execute Python, Bash, or PowerShell

```python
bash = client.run_bash("uname -a", timeout_s=30)
powershell = client.run_powershell("Get-ComputerInfo", timeout_s=30)
python = client.run_python(
    "print(spark.version)",
    mode="inprocess",
    timeout_s=1200,
)

print(python.exit_code, python.stdout, python.stderr)
```

`ExecResult` includes decoded and raw output, duration, timeout/error fields, and a `job_id` when
the long-job path was used. `ok` is true only for exit code zero without timeout.

Requests with a timeout above 55 seconds automatically use submit plus long-poll while preserving
the same final result shape. Override with `async_job=True` or `False`.

## Drive jobs directly

```python
from privy import ExecRequest

request = ExecRequest(
    kind="python",
    code="run_expensive_query()",
    mode="inprocess",
    timeout_s=3600,
)

job_id = client.submit(request)
state, result = client.poll(request, job_id, wait_s=20)
if state == "running":
    state, result = client.poll(request, job_id, wait_s=20)

client.cancel(request, job_id)
```

Jobs live in listener memory and are not durable across listener restarts.

## Run commands with dependencies

```python
from privy import CommandSpec

batch = client.run_many(
    [
        CommandSpec(id="one", kind="bash", code="./one.sh"),
        CommandSpec(id="two", kind="powershell", code=".\\two.ps1"),
        CommandSpec(
            id="three",
            kind="python",
            code="finish()",
            mode="inprocess",
            depends_on=("one",),
        ),
    ],
    max_parallel=32,
)

for outcome in batch.outcomes:
    print(outcome.id, outcome.state, outcome.result)
```

The client rejects duplicate IDs, missing dependencies, cycles, invalid modes, and invalid limits
before submission. Failed commands skip transitive dependents while independent branches continue.

### Observe terminal commands

Both `RelayClient.run_many()` and `privy.batch.run_many()` accept the optional keyword
`on_command_complete: Callable[[CommandOutcome], None] | None = None`. Each native terminal outcome
is delivered once, including failed, skipped, remotely cancelled, and ambiguous transport/deadline
outcomes. Notifications run synchronously on the scheduler's calling thread, outside poll workers
and their locks. Keep callbacks small and local; do not extract remote logs or mutate outcomes there.
Use a separate observer for expensive work. With `None`, execution and final result semantics are
unchanged and no side-channel IO occurs.

If a callback raises an `Exception`, the scheduler stops new submissions but drains already-active
jobs with its normal polls, retries, and deadlines. Native successes, failures, and dependency skips
remain intact; never-submitted commands become `cancelled` / `batch_callback_failed`. Notifications
are still attempted once for the remaining terminal outcomes, not retried. After draining,
`privy.BatchCallbackError` is raised with:

- `result`: the complete ordered native `BatchResult`, not a replacement execution error;
- `failures`: tuples of `(command_id, original_exception)`, also retaining the first exception as
  `__cause__`.

For `KeyboardInterrupt` or another escaping `BaseException`, native best-effort cancellation and
the original exception type are preserved. When a callback is enabled, the exception also carries
`batch_result` and `batch_callback_failures`; callback errors during cleanup do not replace the
original interruption. Cancellation does not confirm that an ambiguous remote worker has stopped.
Transport/deadline failures retain their original job IDs and are not automatically resubmitted.

For a ready-made private local sink, use the CLI's
[`--results-dir` contract](CLI.md#live-terminal-artifacts-opt-in).

## Transfer files

```python
uploaded = client.upload_file(
    "./model.pkl",
    "/tmp/model.pkl",
    chunk_size=1024 * 1024,
    overwrite=False,
)

downloaded = client.download_file(
    "/tmp/results.parquet",
    "./results.parquet",
    overwrite=True,
)
```

Both methods resume matching partial files and return `TransferResult` with direction, paths, byte
count, SHA-256, transfer ID, and resume offset.

## Send a raw execution request

```python
from privy import ExecRequest

result = client.send(
    ExecRequest(kind="bash", code="printf hello", timeout_s=30),
)
```

Wire execution requests remain backward-compatible: missing action fields default to synchronous
`exec`.
