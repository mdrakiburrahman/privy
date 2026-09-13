# CLI

The same CLI is installed by the Python package and bundled in the standalone Linux and Windows
x86_64 executables.

## Runtime reference

```bash
privy --help
```

Top-level help recursively prints every command, nested action, option, environment variable,
default, output contract, and concise example. It is deterministic plain text so both humans and
runtime agents can discover all supported behavior without repository access.

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

Flags with matching names override environment variables. Token and key shapes are mutually
exclusive. `--ttl-seconds` controls tokens minted internally from a key.

## Execute one command

```bash
privy client --bash 'uname -a'
privy client --python 'print(6 * 7)'
privy client --powershell 'Get-ComputerInfo'
privy client --file script.py --file-kind python
cat script.sh | privy client --file - --file-kind bash
Get-Content script.ps1 | privy client --file - --file-kind powershell
```

Python modes:

- `subprocess` starts a separate Python interpreter on the server.
- `inprocess` executes in the listener interpreter and can access seeded notebook globals.

```bash
privy client --python 'print(spark.version)' --mode inprocess
```

PowerShell is subprocess-only. The listener runs `pwsh` when available, then falls back to Windows
PowerShell. Commands are passed as one argument rather than interpolated into a shell command line.

## Timeouts and async jobs

`--timeout-s` applies to Python, Bash, and PowerShell and is passed to the remote executor:

```bash
privy client --bash './long-job.sh' --timeout-s 1200
```

When the timeout is above 55 seconds, privy automatically uses submit plus long-poll to avoid Azure
Relay's response deadline. This is transport behavior: the CLI still waits and emits the final
stdout, stderr, and exit status.

- `--async-job` forces submit plus long-poll.
- `--no-async-job` forces one Relay request and may fail around the Relay response deadline.

The default timeout is 600 seconds, so default CLI calls use the long-job path unless explicitly
opted out.

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
      "kind": "powershell",
      "code": ".\\build-right.ps1",
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

The client validates the full DAG before sending work. It launches up to `max_parallel` ready
commands, long-polls them concurrently, unlocks dependents after success, skips transitive
dependents after failure, and continues independent branches.

```bash
privy client --batch pipeline.json --json
```

JSON output preserves manifest order and includes each command's state, result, error, and skip
causes. Any failed or skipped command makes the aggregate exit code non-zero. Invalid manifests are
usage errors.

### Live terminal artifacts (opt-in)

```bash
privy client --batch pipeline.json --json --results-dir ./attempt-001
```

`--results-dir` publishes each terminal command while other commands are still running, using the
same native scheduler. It adds no output to stdout: the final batch JSON (or text output without
`--json`) is unchanged. Without this flag there is no artifact writer or additional filesystem IO.
This is a local result side channel, not SQL logging, remote log extraction, or another scheduler.

Use a **new directory for every invocation**. Its parent must already exist. Existing destinations,
`..` path components, and symlinks in the output path are refused before commands are submitted.
The option requires a POSIX client filesystem supporting directory descriptors, no-follow opens,
hard links, and file/directory `fsync`; unsupported filesystems fail preflight, not silently downgrade.
Directories are mode `0700` and files `0600`. Files are serialized and synced before being atomically
published without overwriting any existing name.

The version-1 contract is:

- **`batch.json`**, published before submission:
  `{schema: "privy.batch.results", version: 1, batch_id, manifest, commands}`.
  `batch_id` is one UUID hex string for this invocation.
  `manifest` contains `source` (the supplied manifest path or `"-"`), `sha256` (the input bytes;
  UTF-8 text when stdin has no binary stream), and effective `max_parallel`.
  `commands` preserves original manifest order as
  `[{index: 0, id: "<original ID>", file: "command-000001.json"}, ...]`.
- **`command-000001.json`**, etc., published once per terminal command:
  `{schema: "privy.batch.command", version: 1, batch_id, index: 0, outcome}`.
  `outcome` is the complete, unmodified native `CommandOutcome.to_dict()` representation, including
  nested `result.stdout`, `result.stderr`, native errors/job IDs, and skip causes. Like final batch
  JSON, it omits binary `stdout_bytes`/`stderr_bytes`. Indexes are zero-based; filenames are numbered
  from one with at least six digits. Command IDs are data, never paths: consumers must use the map
  in `batch.json` and check each file's `batch_id`, `index`, and `outcome.id`.
- **`complete.json`**, published after the scheduler finishes:
  `{schema: "privy.batch.complete", version: 1, batch_id, state, command_count, artifact_count,
  missing_command_ids, errors, result}`.
  `state` is `"complete"`, `"error"`, or `"interrupted"`. `artifact_count` counts successfully
  published command files; `missing_command_ids` lists IDs without confirmed publication.
  `errors` is `[{command_id: "<ID or null>", error: "<message>"}, ...]` (the global ID is JSON
  `null`, not a string). `result` is the native final batch JSON, or `null` if unavailable.

`state: "complete"` means artifact delivery completed, **not** that every command succeeded.
Check the native results and CLI exit code as well. A missing end marker is never proof of completion.
Hidden `.pending-*` files, if left by process termination or an IO failure, are not completion records.

A terminal-artifact IO failure stops new submissions and drains already-active jobs using native
polling/retry/deadline handling. Their real SQL/command results are retained; unsubmitted work becomes
`cancelled` with `error: "batch_callback_failed"`. The CLI still emits the original-shaped final
response, reports delivery failures on stderr, and exits `1` even if every executed command succeeded.
The end marker records the error when it can be written; a failure to write that marker also exits
`1` without suppressing the native final stdout. Preflight errors exit `2` without execution.

`Ctrl-C` keeps native best-effort cancellation and exit `130`; available terminal outcomes and an
`interrupted` end marker are retained when possible. Cancellation is not proof a remote worker has
stopped. Existing `poll_transport`/`poll_deadline` errors remain ambiguous and retain job IDs for
reconciliation; neither artifact delivery nor a missing receipt automatically reruns remote work.

## Transfer files

```bash
privy file upload ./model.pkl /tmp/model.pkl
privy file download /tmp/results.parquet ./results.parquet
```

Transfers resume matching partial files, verify SHA-256, and refuse existing destinations unless
`--overwrite` is set. See [File transfer](FILE_TRANSFER.md).

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
