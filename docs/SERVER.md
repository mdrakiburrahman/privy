# Server

The server maintains the Azure Relay listener and handles execution jobs, file-transfer operations,
and optional HTTP proxy requests.

## Start from the CLI

Load Relay settings and run:

```bash
privy server
```

Useful options:

```text
--max-workers 32
--recv-timeout-s 1
--proxy-target http://127.0.0.1:8080
--token "$PRIVY_RELAY_TOKEN"
--ttl-seconds 172800
```

Run `privy --help` for the complete option and environment reference.

## Start from Python

```python
from privy import RelayServer

server = RelayServer(
    namespace="my-relay",
    path="privy",
    token="SharedAccessSignature ...",
)
server.serve_forever()
```

Key-based credentials remain supported:

```python
server = RelayServer(
    namespace="my-relay",
    path="privy",
    keyrule="privy-listen",
    key="...",
    ttl_seconds=3600,
)
```

## Expose notebook globals

`mode="inprocess"` executes in the listener interpreter. Seed live notebook objects when
constructing the server:

```python
server = RelayServer(
    namespace="my-relay",
    path="privy",
    token=token_provider,
    inprocess_globals={"spark": spark, "sc": sc},
)
server.serve_forever()
```

In-process calls share globals across requests and capture stdout/stderr per execution thread. They
run concurrently by default. Set `PRIVY_SERIALIZE_INPROCESS=1` to restore one-at-a-time behavior.

In-process execution is for trusted callers and is not an isolation boundary: submitted Python runs
inside the listener interpreter. The POSIX CLI re-executes with sanitized process metadata, protects
the Linux listener from same-user process inspection, and gives subprocess children a scrubbed
environment, but in-process code can still inspect Python process state. Use separate OS
identities/listeners/credentials when callers require different trust boundaries.

## Long-running jobs

Azure Relay requires a listener response in roughly one minute. Privy decouples longer execution
from that response:

1. `submit` starts work and immediately returns a `job_id`.
2. `poll` waits server-side for completion or a short poll deadline.
3. `cancel` makes a best-effort interruption and removes the job handle.

The CLI and `RelayClient.send()` choose this path automatically when `timeout_s` exceeds 55 seconds.
The caller still receives a normal final `ExecResult`.

Finished jobs remain in listener memory for one hour by default. Set `PRIVY_JOB_RETENTION_S` to
change retention. Restarting the listener loses in-memory job handles.

Each long poll occupies a server worker. Keep `max_workers` above expected concurrent client
polling; the default is 32.

## Token refresh and expiry

Pass a callable for long-lived listeners:

```python
server = RelayServer(
    namespace="my-relay",
    path="privy",
    token=lambda: broker.get_current_token(),
)
```

The provider is called before every connection or reconnection. Privy validates expiry and audience
before dialing and logs only the remaining lifetime. If a provider returns the same expired token
after disconnect, the listener exits instead of reconnecting forever. The CLI exit code is `3`.

## Proxy a server-local service

```bash
privy server --proxy-target http://127.0.0.1:8080
```

See [HTTP proxy](PROXY.md) for the client side.
