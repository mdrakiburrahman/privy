# CLI

The same CLI is installed by the Python package and bundled in the standalone Linux x86_64 executable.

## Runtime reference

```bash
privy --help
```

Top-level help recursively prints every command, nested action, option, environment variable, default, output contract, and concise example. It is deterministic plain text so both humans and runtime agents can discover all supported behavior without repository access.

Focused help remains available:

```bash
privy client --help
privy file --help
privy token mint --help
```

## Relay credentials

Every Relay command requires namespace and path plus exactly one credential shape:

```bash
export PRIVY_RELAY_NAMESPACE=my-relay
export PRIVY_RELAY_PATH=privy

# Preferred on consumers:
export PRIVY_RELAY_TOKEN='SharedAccessSignature ...'

# Or key-based:
export PRIVY_RELAY_KEYRULE=privy-send
export PRIVY_RELAY_KEY='...'
```

Flags with matching names override environment variables. Token and key shapes are mutually exclusive. `--ttl-seconds` controls tokens minted internally from a key.

## Execute one command

```bash
privy client --bash 'uname -a'
privy client --python 'print(6 * 7)'
privy client --file script.py --file-kind python
cat script.sh | privy client --file - --file-kind bash
```

Python modes:

- `subprocess` starts a separate Python interpreter on the server.
- `inprocess` executes in the listener interpreter and can access seeded notebook globals.

```bash
privy client --python 'print(spark.version)' --mode inprocess
```

## Timeouts and async jobs

`--timeout-s` applies to both Python and Bash and is passed to the remote executor:

```bash
privy client --bash './long-job.sh' --timeout-s 1200
```

When the timeout is above 55 seconds, privy automatically uses submit plus long-poll to avoid Azure Relay's response deadline. This is transport behavior: the CLI still waits and emits the final stdout, stderr, and exit status.

- `--async-job` forces submit plus long-poll.
- `--no-async-job` forces one Relay request and may fail around the Relay response deadline.

The default timeout is 600 seconds, so default CLI calls use the long-job path unless explicitly opted out.

## Run a dependency graph

Use `--batch FILE`, or `--batch -` for stdin:

```json
{
  "max_parallel": 32,
  "commands": [
    {
      "id": "extract",
      "kind": "bash",
      "code": "./extract.sh",
      "timeout_s": 600
    },
    {
      "id": "left",
      "kind": "python",
      "code": "build_left()",
      "mode": "inprocess",
      "depends_on": ["extract"]
    },
    {
      "id": "right",
      "kind": "bash",
      "code": "./build-right.sh",
      "depends_on": ["extract"]
    },
    {
      "id": "publish",
      "kind": "bash",
      "code": "./publish.sh",
      "depends_on": ["left", "right"]
    }
  ]
}
```

The client validates the full DAG before sending work. It launches up to `max_parallel` ready commands, long-polls them concurrently, unlocks dependents after success, skips transitive dependents after failure, and continues independent branches.

```bash
privy client --batch pipeline.json --json
```

JSON output preserves manifest order and includes each command's state, result, error, and skip causes. Any failed or skipped command makes the aggregate exit code non-zero. Invalid manifests are usage errors.

## Transfer files

```bash
privy file upload ./model.pkl /tmp/model.pkl
privy file download /tmp/results.parquet ./results.parquet
```

Transfers resume matching partial files, verify SHA-256, and refuse existing destinations unless `--overwrite` is set. See [File transfer](FILE_TRANSFER.md).

## Mint a token

```bash
privy token mint --rights send --ttl 30m
privy token mint --rights listen --ttl 2h
```

See [Authentication](AUTHENTICATION.md) for the role-specific environment variables and broker flow.

## Output and exit behavior

- Single-command stdout and stderr preserve their remote streams.
- `--json` emits structured single, batch, or transfer results.
- A single command exits with the remote exit code; a timeout exits `124` and reports `timed_out`.
- Batch execution exits `0` only when every command succeeds.
- Usage and manifest errors exit `2`.
- Listener token expiry exits `3`.
- `Ctrl-C` exits `130`; active DAG jobs receive best-effort cancellation.
