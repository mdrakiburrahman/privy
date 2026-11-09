# Standalone retained-job observer

`privy observe-job` is a separately invoked, one-shot observer. It does not start or contact a hub,
construct `SqlBackend`/`HubService`, register a hub incarnation, open an outbox, import/replay
journals, or submit, execute, cancel, reconcile, renew, release, acknowledge cancellation, or unpin
work. It installs no schema and writes no shared SQL records.

**Building this artifact is not permission to invoke it.** The workload owner must separately obtain
authorization for an exact invocation with independently verified credentials, bindings and artifact
hash. This guide's example uses fictitious identifiers and is not an operational W9 command.

## Inputs and command

Supply all three files explicitly; there is no hub/home or candidate discovery. The settings file
uses the existing `version: 1`, `storage.mode: azure_sql` YAML schema. Existing authorized Relay
credentials use the existing `relays.json` schema. Credentials remain in worker memory and are not
copied into receipts, logs, command-line arguments or a new credential file.

The expected-binding YAML contains exactly these keys:

```yaml
server: example.database.windows.net
database: privy
job_id: exact-hub-job
remote_job_id: exact-listener-job
owner: explicitly-inactive-owner
lease_id: exact-lease
client_id: exact-client
session_id: exact-holder-session
repo_label_location: lease
repo_label_key: repo
repo_label: expected-repository
purpose: expected-purpose
target: dev/example_windows
member_layer: windows
member: dev/example_windows
namespace: example-relay
path: example_windows
request_sha256: "0000000000000000000000000000000000000000000000000000000000000000"
```

`member` is the canonical target ID stored in the lease's `members` mapping, **not** just the Relay
path. `repo_label_location` is explicitly `lease` or `client`, and `repo_label_key` identifies the
existing label; neither is guessed. The request hash is the SHA-256 of the authoritative persisted
`spec.code` UTF-8 bytes. It is checked locally; the listener does not attest that hash, lease or
client. Supplied IDs and hashes are assertions, **not authentication**.

After separate operator authorization, the command shape is:

```powershell
.\privy.exe observe-job --bindings .\expected-bindings.yaml --settings .\appsettings.yaml --relays .\relays.json
```

Capture stdout only into a sanitized, operator-owned receipt. A result exits `0` only for
`retained-terminal-result-observed`; inconclusive exits `1`; missing command arguments exit `2`.
`--help` does not acquire SQL/Relay credentials or initialize a hub. The hidden worker entry point
is an implementation detail; it is not a separate operational command.

## Read and transport boundary

Before and after the poll, the worker opens a fresh SQL connection using `AzureCliCredential`,
encrypted ODBC Driver 18 with certificate verification, and no connection pooling. Each exact-key
read uses a short SERIALIZABLE transaction and a two-second SQL lock timeout; query/login timeouts
are three seconds. It never enables snapshot isolation or acquires the global lease application
lock. Each transaction is rolled back and its cursor/connection closed before proceeding.

The read set includes the exact job/owner/revision/request, inactive owner row, lease, client and
existing target configuration. It requires JSON `active: false` as an actual boolean, an active
lease unexpired according to database time, the expected holder session/repository/purpose/member,
and a matching exact-job pin owned by the expected hub. Local authorized Relay mapping must agree
with SQL. Missing, oversized, changed or inconsistent evidence refuses observation. Matching
before/after fingerprints do not exclude every ABA change or establish uninterrupted validity
between reads. Short serializable reads can briefly block writers: noninterrupting does not mean
zero contention.

The observer sends **at most one HTTPS POST**, without redirects, automatic retries, stock
`RelayClient.poll`, or the coercing `ExecResponse` decoder. Its payload has `action: poll`, the
exact remote job ID, empty `code`, `wait_s: 0`, and explicit stdout/stderr acknowledgement offsets
**0/0**. There is no second poll to drain output or improve an inconclusive answer.

Transport uses a two-second socket timeout, an eight-second poll watchdog/deadline, and a 512 KiB
raw response limit. Encoded responses, redirects and non-200 statuses are refused without reading an
error body or following another endpoint. A private supervised worker process imposes a 30-second
whole-worker bound including credential acquisition, native ODBC, DNS and HTTP. Timeout terminates
only that private process, not any hub/listener/workload. The worker has its own deadline too. A
timeout cannot retract the already-sent poll or prove whether its response was delivered.

Raw JSON must have present `job_id`, `state`, `exit_code`, `timed_out` and `error` fields. Duplicate
JSON keys are refused. Exit code must be a genuine integer (not a bool, float, string or default),
timeout a genuine boolean, remote ID exact, state `done` or `cancelled`, and error null, `timeout`
or `cancelled`. The precise subprocess/true/timeout/-1 sentinel is inconclusive; legitimate negative
exits are not rejected wholesale. Output payload contents are neither printed nor interpreted as
instructions.

## Legacy retained-memory effects and retention

This is **non-destructive observation, not an absolute zero-memory-write promise**. In the examined
0.3.1 non-streaming terminal poll, `executor.poll_job` annotates the existing retained response:

- `job.response.job_id` is assigned the requested job ID.
- `job.response.state` is assigned `cancelled` if `job.cancelled`, otherwise `done`.

It does not update the job's `created_at`/`finished_at`, extend result retention, run the retention
reaper, or delete the retained handle. Normal listener retention/overflow removal can still happen
independently. Reading is not a guarantee that the evidence will remain available for another call.

Streaming polls call buffer trimming with the supplied acknowledgements; **0/0 never advances either
buffer's base**. Already-trimmed buffers can produce `bad_offset`, which is inconclusive even when
the reply says `done`. Large undrained output can report `running` after execution has finished. The
examined legacy stream poll returns at most 128 KiB per stream, but the observer also enforces its
own raw-body limit. Never advance acknowledgements to obtain terminal state.

## What a receipt does not authorize

Receipts include actual UTC observation bounds, database read times, exact expected bindings,
validated raw terminal fields, the observer version and artifact SHA-256. For a packaged executable
the hash covers that executable; a source invocation identifies its observer module hash instead.
Deadline/process failures can yield a shorter inconclusive receipt without completed provenance.

Only `retained-terminal-result-observed` and `inconclusive` are outcomes. There is no synthesized
hub success/failure/cancel state. A positive result is **not continuous fencing**, process identity
or process-tree/descendant exit proof, replay protection, an execution capability, permission for
Up, or authority to release/unpin work. All existing holds remain in force unless separately
satisfied.

Offline tests use only synthetic SQL cursors, private sockets/processes and test-owned retained
listener handles that never start execution. They do not constitute real SQL-engine, Azure Relay,
two-machine or live-workload validation.
