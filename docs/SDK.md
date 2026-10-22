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

## Stream output

Pass `on_output` to receive chunks while the command runs; it implies the job transport. The result
still holds the complete output.

```python
result = client.run_bash(
    "npx nx run app:build --verbose",
    on_output=lambda stream, data: print(data.decode(), end=""),
)
```

`stream` is `"stdout"` or `"stderr"`. Against a listener without streaming support, everything
arrives once at completion.

`on_submit` receives the listener's job id as soon as the job starts, so another thread can cancel
it:

```python
job_ids = []
request = ExecRequest(kind="bash", code="./long.sh", timeout_s=3600)
result = client.send(request, on_submit=job_ids.append)

# From another thread while send() is running:
client.cancel(request, job_ids[0])
```

## Probe a listener

```python
from privy import RelayHTTPError

try:
    info = client.listener_info()
except RelayHTTPError as exc:
    print("unreachable:", exc.status_code, exc.detail)
else:
    if info is None:
        print("online, but the listener predates listener_info")
    else:
        print(info["hostname"], info["platform"]["description"], info["runtimes"])
```

`listener_info()` never runs code on the listener. `RelayHTTPError` (a `RuntimeError`) carries the
HTTP status, a redacted response excerpt, and whether the request timed out. Azure Relay answers 404
both when no listener is connected (`EndpointNotFound`) and when the Hybrid Connection does not
exist.

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
