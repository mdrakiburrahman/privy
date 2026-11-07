# Azure SQL bug bash on another Windows machine

**Review gate:** this branch is for review. Do not merge it or enable auto-merge.

Use **`privy_test`**, a **new home**, and a port other than **7748**. The existing
hub at `C:\.temp\privy-hub` is file-backed: its leases **do not coordinate** with
SQL-backed leases. Do not use any devbox currently used by that hub. The safest
first pass uses the simulated listeners below, which never contact real devboxes.
Do not replace the running hub's executable, copy its home, or copy its Relay keys.

## 1. Prerequisites and checkout

Install Git, Python (3.12 recommended), uv, Azure CLI, and Microsoft's **ODBC Driver
18 for SQL Server** from their official distributions. Driver 18 and Azure CLI
are external prerequisites; they are not bundled in `privy.exe`.

In a new PowerShell window:

```powershell
git --version
python --version
uv --version
az version
Get-OdbcDriver -Name 'ODBC Driver 18 for SQL Server' |
    Select-Object Name, Platform

git clone --branch mdrakiburrahman-azure-sql-hub `
    https://github.com/mdrakiburrahman/privy.git C:\.temp\privy-sql-bug-bash-src
Set-Location C:\.temp\privy-sql-bug-bash-src
uv sync --python 3.12 --extra sql --group dev
```

If uv's download transport fails, use the same project extras in a fresh local
virtual environment, without changing system Python:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[sql,dev]'
```

All later commands use `.\.venv\Scripts\python.exe`. Verify:

```powershell
.\.venv\Scripts\python.exe -c "import azure.identity, pyodbc; print(pyodbc.drivers())"
.\.venv\Scripts\python.exe -m privy --version
az login
az account show --query id --output tsv
```

The resources are in subscription
`ce859648-30e1-4135-9d0f-8358aebfe789`, resource group `privy`, Canada Central.
The server is **`privy-ce859648-sql.database.windows.net`**.

Sign in as the deploying user, who is the server's Entra administrator. A different
identity needs an Entra database user and permissions for schema bootstrap and
application reads/writes. An administrator can grant a dedicated test identity
`db_owner` **in `privy_test` only** for the bug bash; production privileges should
be reviewed separately. Database permission does not grant ARM deployment rights.
Do not print, save, paste, or commit access tokens. The hub obtains and refreshes
tokens internally through `AzureCliCredential`.

## 2. Allow this machine's actual SQL egress IP

An existing authorized infrastructure administrator should run the guarded
deployment script with the new machine's exact public IPv4 address:

```powershell
.\scripts\deploy_infra.ps1 -ClientIPv4Addresses @('REPLACE_WITH_EXACT_IPV4') -ValidateOnly
.\scripts\deploy_infra.ps1 -ClientIPv4Addresses @('REPLACE_WITH_EXACT_IPV4')
```

Replace the placeholder before running. Multiple observed addresses can be passed
as an array. Read `infra\README.md` first: the script preserves existing rules and
rejects changes to existing server identity/security settings. Do not run it as
a different deploying identity merely to change the server administrator.

An HTTPS IP-check site may report a different egress from SQL. If SQL returns
**40615**, its message contains the actual denied client IPv4. Have the
administrator authorize that exact `/32` and retry after propagation. Do not
authorize all IPs, a broad subnet, or an Azure-services bypass. Rotating egress
was observed during verification; stable egress is necessary for reliable
operation. The application never changes firewall rules itself.

## 3. Create a new, isolated SQL home

On machine A choose port **17749**; on machine B choose **17750**. These are new
instances, not replacements for the live hub.

```powershell
$HomeSql = 'C:\.temp\privy-sql-bug-bash-home'
New-Item -ItemType Directory -Path $HomeSql -Force | Out-Null
@'
version: 1
storage:
  mode: azure_sql
  server: privy-ce859648-sql.database.windows.net
  database: privy_test
  driver: ODBC Driver 18 for SQL Server
  retention_days: 7
  outbox_max_bytes: 268435456
'@ | Set-Content -Path "$HomeSql\appsettings.yaml" -Encoding utf8
$Port = 17750 # Use 17749 on machine A.
$Base = "http://127.0.0.1:$Port"
```

Actual `appsettings.yaml` files are gitignored; keep this copy outside the checkout.
Use the same target topology and `leases.yaml` rules on both machines. SQL startup
rejects conflicting topology or lease/storage configuration rather than silently
splitting coordination.

### Safe first pass: simulated listeners

In a dedicated foreground PowerShell window:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -u .\tests\sql_hub_process.py `
    --home $HomeSql --settings "$HomeSql\appsettings.yaml" --port $Port
```

This runs the actual hub and actual Azure SQL backend with 20 **test-owned,
in-process simulated listeners**. It writes a dummy, nonsecret `relays.json` in
the new home. It does not use Azure Relay or real devboxes. Its `/test/*` fault
injection endpoints exist only in this test harness, not the ordinary hub.

In another PowerShell window, set `$HomeSql`, `$Port`, and `$Base` as above:

```powershell
Invoke-RestMethod "$Base/healthz"
Invoke-RestMethod "$Base/v1"
Start-Process "$Base/ui/hycos"
```

Health must report `storage.mode: azure_sql`; `/v1` must report SQL storage status,
the unique owner incarnation and delivery-journal counts. The startup log says
`Storage: azure_sql` with server/database and owner; the dashboard sidebar shows
`storage Azure SQL` and pending/rejected delivery counts. A successful startup
bootstraps the schema. Missing/invalid explicit settings or inaccessible SQL must
fail, not create a file-backed lease authority.

### Optional real-listener pass

Stop the simulator with Ctrl+C before changing topology. Use only test-owned,
idle devboxes. Hydrate credentials separately into a **new** local `relays.json`
using the documented `Sync-PrivyHubRelays.ps1` workflow in `docs\SCHEDULED_TASKS.md`,
or an operator-provided file for those test targets. Do not copy the live home.
Both SQL hubs need matching target IDs/namespaces/paths, but credentials remain
local. Real probes, jobs, transfers and interactive terminals require Relay
credentials; lease/history SQL storage alone does not transport them.

The disposable database must be reset before switching from the simulator's
topology to another topology (see section 5). Then start the ordinary app:

```powershell
.\.venv\Scripts\python.exe -m privy hub serve `
    --home $HomeSql --settings "$HomeSql\appsettings.yaml" --port $Port
```

Do not use default hub CLI start/status/stop commands: they can discover an
existing hub. All HTTP calls here address the explicitly chosen `$Base`.

## 4. Numbered scenarios and expected results

1. **Shared clients and racing leases.** On each machine, register a different
   client and select the same currently free group:

   ```powershell
   $Client = Invoke-RestMethod "$Base/v1/clients" -Method Post -ContentType application/json `
       -Body (@{name="bug-bash-$env:COMPUTERNAME"; session_id=[guid]::NewGuid().ToString()} | ConvertTo-Json)
   $Groups = Invoke-RestMethod "$Base/v1/groups"
   $Group = $Groups | Where-Object state -eq free | Select-Object -First 1
   $Group.group
   Invoke-RestMethod "$Base/v1/leases" -Method Post -ContentType application/json `
       -Body (@{client_id=$Client.client_id; target=$Group.group; ttl='2m'} | ConvertTo-Json)
   ```

   Agree on the same group name on both machines and issue acquisition together.
   Exactly one new acquisition should return **201**; the other should return
   **409 `group_leased`** with the same holder. Listing leases from either machine
   must show the same winner. Registering the same `session_id` again, including
   from the other machine, must return the existing client ID.

2. **Renew, release, expiry and cooldown.** Find the winning lease from either
   machine, renew it using its actual holder ID, then release it:

   ```powershell
   $Lease = Invoke-RestMethod "$Base/v1/leases" | Where-Object group -eq $Group.group | Select-Object -First 1
   Invoke-RestMethod "$Base/v1/leases/$($Lease.lease_id)/renew" -Method Post `
       -ContentType application/json -Body (@{client_id=$Lease.client_id; ttl='2m'} | ConvertTo-Json)
   Invoke-RestMethod "$Base/v1/leases/$($Lease.lease_id)?client_id=$($Lease.client_id)" -Method Delete
   ```

   Renewal must be visible on both machines. Wrong-holder renewal/release must
   fail. Explicit release skips cooldown if no later usage occurred. Separately,
   acquire with `ttl='5s'`, let it expire, and attempt acquisition as another
   client: the default 15-minute cooldown should be reported. Output/job reads
   count as usage and can restart cooldown after release; choose another free
   test group rather than overriding cooldown.

3. **Shared jobs, output and cross-hub cancellation.** Acquire a fresh lease
   on A, then start a simulated blocking job on A:

   ```powershell
   $Lease = Invoke-RestMethod "$Base/v1/leases" -Method Post -ContentType application/json `
       -Body (@{client_id=$Client.client_id; target=$Group.group} | ConvertTo-Json)
   $Target = $Lease.members.windows
   $Job = Invoke-RestMethod "$Base/v1/jobs" -Method Post -ContentType application/json `
       -Body (@{target=$Target; code='block'; language='python'; wait_s=0; lease_id=$Lease.lease_id} | ConvertTo-Json)
   $Job.job_id
   ```

   On B, find the matching ID via `$Base/v1/jobs?limit=200` or copy only that ID:

   ```powershell
   $JobId = 'REPLACE_WITH_JOB_ID'
   Invoke-RestMethod "$Base/v1/jobs/$JobId/output?stream=stdout&offset=0&limit=4096"
   Invoke-RestMethod "$Base/v1/jobs/$JobId/cancel" -Method Post
   Invoke-RestMethod "$Base/v1/jobs/$JobId?wait_s=10&stdout_offset=0&stderr_offset=0"
   ```

   B must see `simulated: block`, then the job must become `cancelled` after A
   processes the durable cancellation. Lease release, including force release,
   must fail while the job is pinned; after confirmed completion it succeeds.
   Cancellation acceptance is not proof of immediate completion while A is offline.
   With real listeners, use a harmless long-running command instead of `block`.

4. **Restart persistence and conservative pins.** After the job finishes, stop
   A with Ctrl+C and restart it with the same explicit home/settings/port.
   Both machines must still show its summary and output. The new process has a
   different owner incarnation and must not rerun the old job. For an interrupted
   running job, stale heartbeat alone must **not** clear its pin or reassign its
   group. The simulator dies with its process; real remote jobs may keep running.
   Confirm actual completion before administrator-led reconciliation or resetting
   the disposable database.

5. **Safe, per-process outage and delivery replay (simulator only).**

   ```powershell
   Invoke-RestMethod "$Base/test/sql-outage" -Method Post -ContentType application/json -Body '{"enabled":true}'
   $AuditId = "bug-bash-outage-$([guid]::NewGuid())"
   Invoke-RestMethod "$Base/test/audit" -Method Post -ContentType application/json `
       -Body (@{id=$AuditId} | ConvertTo-Json)
   Invoke-RestMethod "$Base/v1/clients" -Method Post -ContentType application/json -Body '{"name":"must-fail-offline"}'
   ```

   The registration must return **503**, with no file-backed registration/lease
   fallback. The other machine must remain healthy. Pending journal writes stay
   on disk. Restore only this process's connection factory:

   ```powershell
   Invoke-RestMethod "$Base/test/sql-outage" -Method Post -ContentType application/json -Body '{"enabled":false}'
   Invoke-RestMethod "$Base/healthz"
   ```

   Pending writes should drain and the audit record should appear in SQL. Never
   disable networking/firewalls, sign everyone out of Azure CLI, or change
   machine-wide network settings to simulate an outage.

6. **Seven-day cleanup and measured loading.** Stop both manual test hubs before
   running the automated controller, so it has the disposable database to itself:

   ```powershell
   $env:PYTHONPATH = 'src'
   .\.venv\Scripts\python.exe .\tests\sql_two_hubs.py `
       --settings "$HomeSql\appsettings.yaml" `
       --artifacts C:\.temp\privy-sql-bug-bash-evidence --benchmark-jobs 3360
   ```

   This starts and stops only its own new processes on reserved non-7748 ports,
   tests actual SQL races/cancellation/restart/outage, seeds 3,360 jobs over
   seven days across 20 simulated devboxes with 4 KiB output per job, measures
   job pages, a seven-day metrics report, dashboard HTML and output loading,
   and proves bounded GC removes an eight-day completed job but retains an
   unfinished one. `evidence.yaml` must say `result: passed`; examine measured
   latency rather than assuming performance. It cleans only its own benchmark
   IDs, not unrelated data. Active leases, live jobs, output/control records
   required by live jobs and unresolved pins must survive cleanup.
   The controller enforces a five-second budget for job/output pages and a
   ten-second budget for seven-day statistics/dashboard HTML. The measured Basic-tier
   run loaded those in 0.63 / 1.42 / 4.21 / 5.35 seconds respectively; larger
   histories, verbose output and different networks need their own measurements.

## 5. Disposable database reset and production cutover

`privy_test` is shared by all bug-bash instances. **Stop every test hub/controller
using it and coordinate with other testers first.** Its data is disposable.
For a complete test reset, connect using Entra authentication to `privy_test`
(for example Azure portal's query editor) and execute:

```sql
IF DB_NAME() <> 'privy_test'
    THROW 51000, 'Refusing to reset a non-test database', 1;
DROP TABLE IF EXISTS dbo.privy_output, dbo.privy_cursors, dbo.privy_cancels,
    dbo.privy_records, dbo.privy_coordination, dbo.privy_schema;
```

Use new empty test homes after reset, or retain old homes separately as evidence:
an old `sql-outbox.sqlite3` will replay its pending records into a reset database.
Never reset `privy`, the SQL server, the resource group, or the live hub's home.

Production cutover is a separate reviewed action. After review and coordinated
quiescence, configure both intended production SQL hubs with `database: privy`,
matching topology/rules and local credentials. Do not concurrently schedule
work through the old file-mode hub; it has a separate lease authority.

## 6. Rollback and bug reports

Before deliberate rollback, finish/release SQL work and let each journal drain.
Keep the new SQL home/journal and logs for replay/evidence. Remove only its local
`appsettings.yaml`, clear `PRIVY_HUB_SETTINGS` if set, and stop passing `--settings`;
then restart in a **different new file-mode home**. Alternatively explicitly
select `version: 1` and `storage: {mode: file}` in that file-mode home's settings.
Deleting a file while retaining `--settings` must fail, not silently switch modes.
Never treat file-mode leases as protection from still-active SQL work.

Report branch/commit, timestamps, database name, new hub owner IDs and ports,
client/lease/job IDs, HTTP status/error codes, the scenario and expected/actual
result, journal counts and measured latency. Attach `evidence.yaml` and redacted
stdout/stderr logs. Do not attach Relay keys/tokens, Azure access tokens, local
credentials, or a copy of the live hub home.

## Verification boundaries

The implementation is exercised against the real provisioned Azure SQL database
using two isolated local processes and simulated listeners. That proves shared
database behavior, not two physical machines or real Relay execution. The
fresh-checkout commands are also exercised locally; second-machine egress and
any different Entra identity require your own verification. New packaged builds
were produced, but this machine's Windows App Control policy blocked their
extracted Python DLL, so executable runtime compatibility is not claimed.
