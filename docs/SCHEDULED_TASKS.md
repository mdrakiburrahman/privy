# Scheduled tasks on Windows

Two Windows logon tasks connect a fleet of machines to one workstation:

- **PrivyServer** runs `privy server` on every remote machine (for example each Dev Box). Each
  machine listens on its own Hybrid Connection.
- **PrivyHub** runs `privy hub serve` on your workstation. It reads pre-hydrated keys for every
  listener and exposes them to you and to Copilot through a loopback REST API and MCP endpoint. See
  [Hub](HUB.md).

```text
 your workstation                         Azure Relay namespace(s)          remote machines
┌────────────────────┐   short-lived SAS  ┌──────────────────────────┐     ┌──────────────────┐
│ PrivyHub task      │ ─────────────────► │ devbox01_windows         │ ◄── │ PrivyServer task │
│ 127.0.0.1:7748     │                    │ devbox02_windows         │ ◄── │ PrivyServer task │
│ relays.json (keys) │                    │ ...                      │     │ ...              │
└────────────────────┘                    └──────────────────────────┘     └──────────────────┘
```

Both tasks start when you sign in, run as you in your interactive session, and keep a PowerShell
console window open that shows live logs. The examples use placeholders: replace
`<relay-namespace>`, `<subscription-id>`, and `<resource-group>` with your own values.

## Listener on each remote machine

`Install-PrivyListenerTask.ps1` prepares one Dev Box as a listener:

1. Derives the Dev Box name from the Dev Box agent configuration and uses `<devbox name>_windows` as
   the Hybrid Connection.
2. Creates that Hybrid Connection and an `all` authorization rule (Manage, Listen, Send) when they
   do not exist, using your Azure CLI login.
3. Downloads the latest `privy.exe`, verifies that it runs, and writes a credentials file plus a
   console launcher into `C:\.temp\privy`.
4. Registers and starts the `PrivyServer` logon task.

Prerequisites: an `az login` session whose identity can create Hybrid Connections and list their
keys in the namespace, and a Microsoft Dev Box (the script reads the Dev Box agent configuration).

```powershell
# Install-PrivyListenerTask.ps1
param(
    [Parameter(Mandatory)]
    [string]$RelayNamespace,
    [Parameter(Mandatory)]
    [string]$RelaySubscriptionId,
    [Parameter(Mandatory)]
    [string]$RelayResourceGroup,
    [string]$TaskName = "PrivyServer",
    [string]$WorkDir = "C:\.temp\privy",
    [string]$RuntimeDir = "C:\ProgramData\privy\rt",
    [string]$PrivyUrl = "https://rakirahman.blob.core.windows.net/public/bins/privy-windows-x86_64.exe",
    [string]$RelayKeyRule = "all",
    [string]$RunAsUser = "$env:USERDOMAIN\$env:USERNAME",
    [switch]$UpdateBinary
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Escape-SingleQuoted([string]$value) {
    if ($null -eq $value) { return "" }
    return $value -replace "'", "''"
}

function Invoke-AzureCliJson([string[]]$Arguments) {
    $output = & az @Arguments --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command 'az $($Arguments -join ' ')' failed with exit code $LASTEXITCODE."
    }
    $output | ConvertFrom-Json
}

function Test-ExeHeader([string]$Path) {
    if (-not (Test-Path $Path)) { return $false }

    $fi = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($fi.Length -lt 5MB) { return $false }

    $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        $b0 = $fs.ReadByte()
        $b1 = $fs.ReadByte()
        return ($b0 -eq 0x4D -and $b1 -eq 0x5A)  # MZ
    }
    finally {
        $fs.Dispose()
    }
}

function Download-Privy([string]$Uri, [string]$Path) {
    $tmp = "$Path.download"
    if (Test-Path $tmp) { Remove-Item -Force $tmp -ErrorAction SilentlyContinue }

    for ($i = 1; $i -le 3; $i++) {
        try {
            Invoke-WebRequest -Uri $Uri -OutFile $tmp -UseBasicParsing
            if (-not (Test-ExeHeader $tmp)) {
                throw "Downloaded file failed integrity checks."
            }
            Move-Item -Path $tmp -Destination $Path -Force
            return
        }
        catch {
            if ($i -eq 3) { throw }
            Start-Sleep -Seconds (2 * $i)
        }
    }
}

function Test-PrivyHelp([string]$ExePath, [string]$RtPath) {
    if (-not (Test-Path $ExePath)) { return $false }
    $oldTemp = $env:TEMP
    $oldTmp  = $env:TMP
    try {
        $env:TEMP = $RtPath
        $env:TMP  = $RtPath
        & $ExePath --help *> $null
        return $true
    }
    catch {
        return $false
    }
    finally {
        $env:TEMP = $oldTemp
        $env:TMP  = $oldTmp
    }
}

function Get-DefenderBlock([string]$Path, [datetime]$Since) {
    # Microsoft Defender logs event 1121 when an attack surface reduction rule stops a program.
    try {
        $events = Get-WinEvent -FilterHashtable @{
            LogName   = "Microsoft-Windows-Windows Defender/Operational"
            Id        = 1121
            StartTime = $Since
        } -ErrorAction Stop
    }
    catch {
        return $null
    }
    $name = [regex]::Escape([IO.Path]::GetFileName($Path))
    $blocked = $events | Where-Object { $_.Message -match "Path:[^\r\n]*\\$name" } | Select-Object -First 1
    if (-not $blocked) { return $null }
    $rule = "unknown"
    if ($blocked.Message -match "(?m)^\s*ID:\s*([0-9A-Fa-f-]{36})") { $rule = $Matches[1].ToUpperInvariant() }
    $names = @{
        "C1DB55AB-C21A-4637-BB3F-A12568109D35" = "Use advanced protection against ransomware"
        "01443614-CD74-433A-B99E-2ECDC07BFC25" = "Block executable files from running unless they meet a prevalence, age, or trusted list criterion"
    }
    if ($names.ContainsKey($rule)) { return "'$($names[$rule])' ($rule)" }
    return $rule
}

function Wait-PrivyAllowed([string]$ExePath, [string]$RtPath, [datetime]$Since) {
    # privy.exe is unsigned. Microsoft Defender's ransomware protection refuses to let Windows
    # PowerShell or cmd.exe start a newly downloaded copy ("Access is denied"), but lets PowerShell 7
    # start it, and once it has run it starts from anywhere. Without pwsh.exe, wait for Defender.
    $pwsh = Get-Command pwsh.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $waitStart = Get-Date
    $announced = $false
    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    try {
        $env:TEMP = $RtPath
        $env:TMP = $RtPath
        while ($true) {
            if ($pwsh) {
                try { & $pwsh.Path -NoProfile -NonInteractive -Command "& '$($ExePath -replace "'", "''")' --version" *> $null } catch { }
            }
            $refused = $null
            try {
                & $ExePath --version *> $null
            }
            catch {
                # NativeCommandError means it started and wrote to stderr.
                if ($_.FullyQualifiedErrorId -notlike "NativeCommandError*") {
                    $refused = $_.Exception.Message
                    if ($_.Exception.InnerException) { $refused = $_.Exception.InnerException.Message }
                }
            }
            if (-not $refused) {
                if ($announced) {
                    Write-Host "Microsoft Defender allowed the new privy.exe after $([int]((Get-Date) - $waitStart).TotalSeconds) seconds."
                }
                return
            }
            if (((Get-Date) - $waitStart).TotalMinutes -ge 15) {
                throw "Windows still refuses to start $ExePath after 15 minutes ($refused). Install PowerShell 7 (pwsh.exe) and run this script again, or ask your IT administrator to allow privy.exe."
            }
            if (-not $announced) {
                $rule = Get-DefenderBlock -Path $ExePath -Since $Since
                if ($rule) { $refused += "; attack surface reduction rule $rule" }
                Write-Host "Microsoft Defender refuses to start the new privy.exe ($refused). Waiting up to 15 minutes..."
                $announced = $true
            }
            Start-Sleep -Seconds 15
        }
    }
    finally {
        $env:TEMP = $oldTemp
        $env:TMP = $oldTmp
    }
}

$config = Get-ChildItem 'C:\Program Files\Microsoft Dev Box Agent\*\*\appsettings.Production.json' -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($null -eq $config) {
    throw "Dev Box agent configuration was not found."
}

$agentSettings = Get-Content $config.FullName -Raw | ConvertFrom-Json
$devboxName = ($agentSettings.DevBoxAgent.metadata.devBoxDataplaneId -split ':')[-1]
if ([string]::IsNullOrWhiteSpace($devboxName)) {
    throw "Dev Box agent configuration does not contain a Dev Box name."
}
$RelayPath = "$($devboxName.ToLowerInvariant())_windows"

$relayArgs = @(
    '--subscription', $RelaySubscriptionId,
    '--resource-group', $RelayResourceGroup,
    '--namespace-name', $RelayNamespace
)

$connections = @(Invoke-AzureCliJson (@('relay', 'hyco', 'list') + $relayArgs))
if ($RelayPath -notin $connections.name) {
    Invoke-AzureCliJson (@(
        'relay', 'hyco', 'create', '--name', $RelayPath,
        '--requires-client-authorization', 'true'
    ) + $relayArgs) | Out-Null
}

$ruleArgs = $relayArgs + @('--hybrid-connection-name', $RelayPath)
$rules = @(Invoke-AzureCliJson (@('relay', 'hyco', 'authorization-rule', 'list') + $ruleArgs))
if ($RelayKeyRule -notin $rules.name) {
    Invoke-AzureCliJson (@(
        'relay', 'hyco', 'authorization-rule', 'create', '--name', $RelayKeyRule,
        '--rights', 'Manage', 'Listen', 'Send'
    ) + $ruleArgs) | Out-Null
}

$keys = Invoke-AzureCliJson (@(
    'relay', 'hyco', 'authorization-rule', 'keys', 'list', '--name', $RelayKeyRule
) + $ruleArgs)
$RelayKey = $keys.primaryKey
if ([string]::IsNullOrWhiteSpace($RelayKey)) {
    throw "Azure Relay did not return a primary key for '$RelayPath' rule '$RelayKeyRule'."
}

$exePath  = Join-Path $WorkDir "privy.exe"
$staged   = Join-Path $WorkDir "privy.new.exe"
$envFile  = Join-Path $WorkDir "relay.$RelayPath.env.ps1"
$launcher = Join-Path $WorkDir "start-privy-console.ps1"
$psExe    = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$installStart = Get-Date

New-Item -ItemType Directory -Force -Path $WorkDir, $RuntimeDir | Out-Null

if ($UpdateBinary -or -not (Test-ExeHeader $exePath) -or -not (Test-PrivyHelp -ExePath $exePath -RtPath $RuntimeDir)) {
    # Stage the new executable next to the old one; a running listener keeps serving meanwhile.
    Download-Privy -Uri $PrivyUrl -Path $staged
    Unblock-File -Path $staged -ErrorAction SilentlyContinue
    Wait-PrivyAllowed -ExePath $staged -RtPath $RuntimeDir -Since $installStart
    if (-not (Test-PrivyHelp -ExePath $staged -RtPath $RuntimeDir)) {
        throw "The downloaded privy.exe cannot run --help. Likely endpoint policy/quarantine issue."
    }

    # Stop the running listener so its executable can be replaced.
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName
    }
    # Both PyInstaller processes map the executable; wait until it can be deleted.
    for ($i = 0; (Test-Path -LiteralPath $exePath) -and $i -lt 40; $i++) {
        Get-CimInstance Win32_Process -Filter "Name = 'privy.exe'" |
            Where-Object { $_.ExecutablePath -eq $exePath } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Remove-Item -Force $exePath -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $exePath) { Start-Sleep -Milliseconds 500 }
    }
    if (Test-Path -LiteralPath $exePath) {
        throw "$exePath is still in use; close the privy console window and run this script again."
    }
    # A rename keeps the file that Microsoft Defender has just allowed.
    Move-Item -LiteralPath $staged -Destination $exePath
}

Get-ChildItem -Path $RuntimeDir -Directory -Filter "_MEI*" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$envContent = @"
`$env:PRIVY_RELAY_NAMESPACE = '$(Escape-SingleQuoted $RelayNamespace)'
`$env:PRIVY_RELAY_PATH      = '$(Escape-SingleQuoted $RelayPath)'
`$env:PRIVY_RELAY_KEYRULE   = '$(Escape-SingleQuoted $RelayKeyRule)'
`$env:PRIVY_RELAY_KEY       = '$(Escape-SingleQuoted $RelayKey)'
`$env:TEMP                  = '$(Escape-SingleQuoted $RuntimeDir)'
`$env:TMP                   = '$(Escape-SingleQuoted $RuntimeDir)'
"@
Set-Content -Path $envFile -Value $envContent -Encoding UTF8

$launcherContent = @"
`$ErrorActionPreference = 'Stop'
. '$($envFile -replace "'", "''")'
Set-Location '$($WorkDir -replace "'", "''")'
& '$($exePath -replace "'", "''")' server -v
"@
Set-Content -Path $launcher -Value $launcherContent -Encoding UTF8

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $RunAsUser
$principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType Interactive -RunLevel Highest
$taskSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$action = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument "-NoProfile -NoLogo -NoExit -ExecutionPolicy Bypass -File `"$launcher`""

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings | Out-Null
Start-ScheduledTask -TaskName $TaskName

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
```

Run it from an elevated PowerShell session (the task runs with highest privileges):

```powershell
.\Install-PrivyListenerTask.ps1 `
    -RelayNamespace <relay-namespace> `
    -RelaySubscriptionId <subscription-id> `
    -RelayResourceGroup <resource-group>
```

Re-running the script is safe. It keeps the existing executable unless it is missing or broken. Pass
`-UpdateBinary` to download the latest release and restart the listener with it. Listeners upgraded
to privy 0.2.0 or later report their host name, OS, user, and runtimes to the hub.

`privy.exe` is unsigned, so Microsoft Defender's ransomware protection refuses to let Windows
PowerShell start a newly downloaded copy ("Access is denied"). PowerShell 7 may start it, and once
it has run it starts from anywhere. The script therefore downloads to `privy.new.exe` while the
current listener keeps running, starts the new file once with `pwsh.exe` (or, without PowerShell 7,
waits up to 15 minutes for Defender to allow it), and only then stops the listener, renames the file
into place, and restarts the task.

The credentials file in `C:\.temp\privy` holds a key with Manage rights on that Hybrid Connection.
Folders created under `C:\` let every authenticated user modify their contents, so on shared
machines restrict the folder to yourself with the `Protect-Directory` function from the hydration
script below.

## Hub on your workstation

The hub never calls Azure Resource Manager at startup. You hydrate the keys for every listener into
its hosting folder once, and re-run the hydration whenever machines are added; a running hub reloads
the file within a second. If the file does not exist the hub still starts and serves no targets.

### 1. Hydrate relay keys

`Sync-PrivyHubRelays.ps1` uses your `az login` session to list the Hybrid Connections in one Relay
namespace, read each one's authorization-rule key, and merge them into `relays.json` in the hub
hosting folder. It restricts the folder to you, SYSTEM, and Administrators, writes the file
atomically, and never prints a key.

```powershell
# Sync-PrivyHubRelays.ps1
param(
    [Parameter(Mandatory)]
    [string]$SubscriptionId,
    [Parameter(Mandatory)]
    [string]$ResourceGroup,
    [Parameter(Mandatory)]
    [string]$Namespace,
    [string]$RelayName = ($Namespace -split '\.')[0],
    [string]$KeyRule = "all",
    [ValidateSet("HybridConnection", "Namespace")]
    [string]$KeyScope = "HybridConnection",
    [string[]]$Include = @("*"),
    [string]$WorkDir = "C:\.temp\privy-hub"
)

$ErrorActionPreference = "Stop"

function Invoke-AzureCliJson([string[]]$Arguments) {
    $output = & az @Arguments --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command 'az $($Arguments -join ' ')' failed with exit code $LASTEXITCODE."
    }
    ($output -join "`n") | ConvertFrom-Json
}

function Protect-Directory([string]$Path) {
    # Folders under C:\ inherit Modify for every authenticated user; keys must not.
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $Path /inheritance:r /grant:r "${user}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls could not restrict access to $Path."
    }
}

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
Protect-Directory $WorkDir
$relaysFile = Join-Path $WorkDir "relays.json"

$relayArgs = @(
    '--subscription', $SubscriptionId,
    '--resource-group', $ResourceGroup,
    '--namespace-name', $Namespace
)

$listed = Invoke-AzureCliJson (@('relay', 'hyco', 'list') + $relayArgs)
$connections = @(@($listed) | Where-Object {
        $name = if ($null -ne $_) { $_.name }
        $name -and @($Include | Where-Object { $name -like $_ }).Count -gt 0
    } | Sort-Object name)

$relay = [ordered]@{
    name            = $RelayName
    namespace       = $Namespace
    subscription_id = $SubscriptionId
    resource_group  = $ResourceGroup
    hydrated_at     = [DateTime]::UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
}
$entries = @()
if ($KeyScope -eq "Namespace") {
    $keys = Invoke-AzureCliJson @(
        'relay', 'namespace', 'authorization-rule', 'keys', 'list', '--name', $KeyRule,
        '--subscription', $SubscriptionId, '--resource-group', $ResourceGroup, '--namespace-name', $Namespace
    )
    $relay.keyrule = $KeyRule
    $relay.key = $keys.primaryKey
    foreach ($connection in $connections) {
        $entries += [ordered]@{ path = $connection.name }
    }
}
else {
    foreach ($connection in $connections) {
        try {
            $keys = Invoke-AzureCliJson (@(
                'relay', 'hyco', 'authorization-rule', 'keys', 'list', '--name', $KeyRule,
                '--hybrid-connection-name', $connection.name
            ) + $relayArgs)
        }
        catch {
            Write-Warning "Skipping $($connection.name): its '$KeyRule' authorization rule key could not be read."
            continue
        }
        $entry = [ordered]@{ path = $connection.name; keyrule = $KeyRule; key = $keys.primaryKey }
        if ($connection.userMetadata) {
            $metadata = [string]$connection.userMetadata
            $entry.description = $metadata.Substring(0, [Math]::Min(1024, $metadata.Length))
        }
        $entries += $entry
    }
}
$relay.hybrid_connections = $entries

$document = [ordered]@{ version = 1; relays = @() }
if (Test-Path -LiteralPath $relaysFile) {
    $existing = Get-Content -LiteralPath $relaysFile -Raw | ConvertFrom-Json
    $document.relays = @(@($existing.relays) | Where-Object {
            $existingName = if ($_.PSObject.Properties['name']) { $_.name } else { ($_.namespace -split '\.')[0] }
            $existingName -ne $RelayName
        })
}
$document.relays = @($document.relays) + @($relay)

$temporary = "$relaysFile.tmp"
[System.IO.File]::WriteAllText($temporary, ($document | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporary -Destination $relaysFile -Force

Write-Host "Hydrated $($entries.Count) of $($connections.Count) hybrid connection(s) for relay '$RelayName' into $relaysFile."
$connections |
    Select-Object @{ n = 'Target'; e = { "$RelayName/$($_.name)" } }, @{ n = 'ListenersNow'; e = { $_.listenerCount } } |
    Format-Table -AutoSize
Write-Host "A running privy hub reloads this file automatically."
```

Run it once per Relay namespace. Each run replaces only the entry with the same `-RelayName`, so
several namespaces can share one `relays.json`:

```powershell
.\Sync-PrivyHubRelays.ps1 `
    -SubscriptionId <subscription-id> `
    -ResourceGroup <resource-group> `
    -Namespace <relay-namespace> `
    -RelayName dev

.\Sync-PrivyHubRelays.ps1 `
    -SubscriptionId <other-subscription-id> `
    -ResourceGroup <other-resource-group> `
    -Namespace <other-relay-namespace> `
    -RelayName lab `
    -KeyScope Namespace `
    -KeyRule privy-hub-send `
    -Include '*_linux'
```

- `-KeyScope HybridConnection` (the default) reads the `-KeyRule` key of every Hybrid Connection,
  matching the per-machine `all` rule the listener script creates. Connections without that rule are
  skipped with a warning.
- `-KeyScope Namespace` reads one namespace-level rule instead. A Send-only rule is the least
  privilege the hub needs:
  `az relay namespace authorization-rule create --name privy-hub-send --rights Send ...`.
- `-Include` filters Hybrid Connection names with wildcards.

Keys are copied, not referenced. Re-run the script after rotating a key or adding machines.

### 2. Install the hub task

`Install-PrivyHubTask.ps1` installs the binary into the same hosting folder, writes a console
launcher, and registers the `PrivyHub` logon task. The hub runs without elevation and keeps its
PyInstaller runtime in a separate folder from the listener so neither cleans up the other's files.

```powershell
# Install-PrivyHubTask.ps1
param(
    [string]$TaskName = "PrivyHub",
    [string]$WorkDir = "C:\.temp\privy-hub",
    [string]$RuntimeDir = "C:\ProgramData\privy\hub-rt",
    [string]$PrivyUrl = "https://rakirahman.blob.core.windows.net/public/bins/privy-windows-x86_64.exe",
    [string]$LocalBinary,
    [int]$Port = 7748,
    [string]$RunAsUser = "$env:USERDOMAIN\$env:USERNAME",
    [switch]$UpdateBinary
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Escape-SingleQuoted([string]$value) {
    if ($null -eq $value) { return "" }
    return $value -replace "'", "''"
}

function Protect-Directory([string]$Path) {
    # Folders under C:\ inherit Modify for every authenticated user; keys must not.
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $Path /inheritance:r /grant:r "${user}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls could not restrict access to $Path."
    }
}

function Test-ExeHeader([string]$Path) {
    if (-not (Test-Path $Path)) { return $false }
    if ((Get-Item -LiteralPath $Path).Length -lt 5MB) { return $false }
    $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        return ($fs.ReadByte() -eq 0x4D -and $fs.ReadByte() -eq 0x5A)  # MZ
    }
    finally {
        $fs.Dispose()
    }
}

function Download-Privy([string]$Uri, [string]$Path) {
    $tmp = "$Path.download"
    if (Test-Path $tmp) { Remove-Item -Force $tmp -ErrorAction SilentlyContinue }
    for ($i = 1; $i -le 3; $i++) {
        try {
            Invoke-WebRequest -Uri $Uri -OutFile $tmp -UseBasicParsing
            if (-not (Test-ExeHeader $tmp)) {
                throw "Downloaded file failed integrity checks."
            }
            Move-Item -Path $tmp -Destination $Path -Force
            return
        }
        catch {
            if ($i -eq 3) { throw }
            Start-Sleep -Seconds (2 * $i)
        }
    }
}

function Invoke-Privy([string]$ExePath, [string]$RtPath, [string[]]$Arguments) {
    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    try {
        $env:TEMP = $RtPath
        $env:TMP = $RtPath
        & $ExePath @Arguments
    }
    finally {
        $env:TEMP = $oldTemp
        $env:TMP = $oldTmp
    }
}

function Test-PrivyHub([string]$ExePath, [string]$RtPath) {
    if (-not (Test-Path $ExePath)) { return $false }
    try {
        Invoke-Privy $ExePath $RtPath @('hub', '--help') *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Stop-PrivyFromPath([string]$Path) {
    # Both PyInstaller processes map the executable; wait until it can be replaced.
    for ($i = 0; $i -lt 40; $i++) {
        $running = @(Get-CimInstance Win32_Process -Filter "Name = 'privy.exe'" |
            Where-Object { $_.ExecutablePath -eq $Path })
        foreach ($process in $running) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
        if ($running.Count -eq 0) {
            if (-not (Test-Path -LiteralPath $Path)) { return }
            try {
                [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None').Dispose()
                return
            }
            catch {
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Path is still in use; close the privy console window and run this script again."
}

function Get-DefenderBlock([string]$Path, [datetime]$Since) {
    # Microsoft Defender logs event 1121 when an attack surface reduction rule stops a program.
    try {
        $events = Get-WinEvent -FilterHashtable @{
            LogName   = "Microsoft-Windows-Windows Defender/Operational"
            Id        = 1121
            StartTime = $Since
        } -ErrorAction Stop
    }
    catch {
        return $null
    }
    $name = [regex]::Escape([IO.Path]::GetFileName($Path))
    $blocked = $events | Where-Object { $_.Message -match "Path:[^\r\n]*\\$name" } | Select-Object -First 1
    if (-not $blocked) { return $null }
    $rule = "unknown"
    if ($blocked.Message -match "(?m)^\s*ID:\s*([0-9A-Fa-f-]{36})") { $rule = $Matches[1].ToUpperInvariant() }
    $names = @{
        "C1DB55AB-C21A-4637-BB3F-A12568109D35" = "Use advanced protection against ransomware"
        "01443614-CD74-433A-B99E-2ECDC07BFC25" = "Block executable files from running unless they meet a prevalence, age, or trusted list criterion"
    }
    if ($names.ContainsKey($rule)) { return "'$($names[$rule])' ($rule)" }
    return $rule
}

function Wait-PrivyAllowed([string]$ExePath, [string]$RtPath, [datetime]$Since) {
    # privy.exe is unsigned. Microsoft Defender's ransomware protection refuses to let Windows
    # PowerShell or cmd.exe start a newly downloaded copy ("Access is denied"), but lets PowerShell 7
    # start it, and once it has run it starts from anywhere. Without pwsh.exe, wait for Defender.
    $pwsh = Get-Command pwsh.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $waitStart = Get-Date
    $announced = $false
    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    try {
        $env:TEMP = $RtPath
        $env:TMP = $RtPath
        while ($true) {
            if ($pwsh) {
                try { & $pwsh.Path -NoProfile -NonInteractive -Command "& '$($ExePath -replace "'", "''")' --version" *> $null } catch { }
            }
            $refused = $null
            try {
                & $ExePath --version *> $null
            }
            catch {
                # NativeCommandError means it started and wrote to stderr.
                if ($_.FullyQualifiedErrorId -notlike "NativeCommandError*") {
                    $refused = $_.Exception.Message
                    if ($_.Exception.InnerException) { $refused = $_.Exception.InnerException.Message }
                }
            }
            if (-not $refused) {
                if ($announced) {
                    Write-Host "Microsoft Defender allowed the new privy.exe after $([int]((Get-Date) - $waitStart).TotalSeconds) seconds."
                }
                return
            }
            if (((Get-Date) - $waitStart).TotalMinutes -ge 15) {
                throw "Windows still refuses to start $ExePath after 15 minutes ($refused). Install PowerShell 7 (pwsh.exe) and run this script again, or ask your IT administrator to allow privy.exe."
            }
            if (-not $announced) {
                $rule = Get-DefenderBlock -Path $ExePath -Since $Since
                if ($rule) { $refused += "; attack surface reduction rule $rule" }
                Write-Host "Microsoft Defender refuses to start the new privy.exe ($refused). Waiting up to 15 minutes..."
                $announced = $true
            }
            Start-Sleep -Seconds 15
        }
    }
    finally {
        $env:TEMP = $oldTemp
        $env:TMP = $oldTmp
    }
}

$exePath  = Join-Path $WorkDir "privy.exe"
$staged   = Join-Path $WorkDir "privy.new.exe"
$launcher = Join-Path $WorkDir "start-privy-hub-console.ps1"
$psExe    = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$installStart = Get-Date

New-Item -ItemType Directory -Force -Path $WorkDir, $RuntimeDir | Out-Null
Protect-Directory $WorkDir

# Stage a new executable next to the old one; the running hub keeps serving meanwhile.
$replace = $true
if ($LocalBinary) {
    Copy-Item -LiteralPath $LocalBinary -Destination $staged -Force
}
elseif ($UpdateBinary -or -not (Test-ExeHeader $exePath) -or -not (Test-PrivyHub -ExePath $exePath -RtPath $RuntimeDir)) {
    Download-Privy -Uri $PrivyUrl -Path $staged
}
else {
    $replace = $false
}
if ($replace) {
    Unblock-File -Path $staged -ErrorAction SilentlyContinue
    Wait-PrivyAllowed -ExePath $staged -RtPath $RuntimeDir -Since $installStart
    if (-not (Test-PrivyHub -ExePath $staged -RtPath $RuntimeDir)) {
        throw "The new privy.exe cannot run 'hub --help'. It may predate privy 0.2.0, or endpoint policy blocked it."
    }
}

# Stop a running hub so its executable can be replaced.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
}
Stop-PrivyFromPath $exePath
if ($replace) {
    # A rename keeps the file that Microsoft Defender has just allowed.
    Move-Item -LiteralPath $staged -Destination $exePath -Force
}

Get-ChildItem -Path $RuntimeDir -Directory -Filter "_MEI*" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$launcherContent = @"
`$ErrorActionPreference = 'Stop'
`$Host.UI.RawUI.WindowTitle = 'privy hub'
`$env:TEMP = '$(Escape-SingleQuoted $RuntimeDir)'
`$env:TMP  = '$(Escape-SingleQuoted $RuntimeDir)'
Set-Location '$(Escape-SingleQuoted $WorkDir)'
& '$(Escape-SingleQuoted $exePath)' hub serve --home '$(Escape-SingleQuoted $WorkDir)' --port $Port
"@
Set-Content -Path $launcher -Value $launcherContent -Encoding UTF8

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $RunAsUser
$principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$action = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument "-NoProfile -NoLogo -NoExit -ExecutionPolicy Bypass -File `"$launcher`""

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings | Out-Null
Start-ScheduledTask -TaskName $TaskName

$healthy = $false
for ($i = 0; $i -lt 30 -and -not $healthy; $i++) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 2 | Out-Null
        $healthy = $true
    }
    catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $healthy) {
    throw "The hub did not answer on http://127.0.0.1:$Port/healthz; check the '$TaskName' console window."
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize | Out-String | Write-Host
Invoke-Privy $exePath $RuntimeDir @('hub', 'status', '--home', $WorkDir)

Write-Host ""
Write-Host "The hub is ready at http://127.0.0.1:$Port/ (no authentication)."
Write-Host "Tell your AI assistant to read that page, or register the MCP endpoint with Copilot CLI:"
Write-Host "  copilot mcp add --transport http --timeout 180000 privy-hub http://127.0.0.1:$Port/mcp"
```

```powershell
.\Install-PrivyHubTask.ps1
```

- `-UpdateBinary` downloads the latest release and swaps it in. Like the listener script, it stages
  the download as `privy.new.exe` and clears it with Microsoft Defender through `pwsh.exe` while the
  running hub keeps serving.
- `-LocalBinary <path>` installs a locally built `privy.exe` instead of downloading one.
- `-Port` changes the loopback port (default `7748`).

The installer prints `privy hub status`, which probes every hydrated listener:

```text
privy hub at http://127.0.0.1:7748 (pid 4242)
relays file: C:\.temp\privy-hub\relays.json (1 relay(s))
targets: 3, offline 1, online 2
TARGET                STATUS   HOST      OS                         USER   PRIVY
dev/devbox01_windows  online   devbox01  Windows-11-10.0.26100-SP0  alice  0.3.0
dev/devbox02_windows  online   -         -                          -      older
dev/devbox03_windows  offline  -         -                          -      -
Point an LLM at http://127.0.0.1:7748/ to use these machines.
```

`older` marks a listener that runs a privy release without host details; re-run the listener script
on that machine with `-UpdateBinary`.

### 3. Point your AI assistant at the hub

The hub has no authentication, so there is nothing to configure: tell the assistant to read
`http://127.0.0.1:7748/`. The page lists the machines online right now and shows how to run commands
on them with live output.

To use the hub's tools through MCP instead, register it with Copilot CLI:

```powershell
copilot mcp add --transport http --timeout 180000 privy-hub http://127.0.0.1:7748/mcp
```

New Copilot sessions then see the hub's tools (`list_targets`, `run`, `get_job`, `broadcast`, and
others). Copilot asks before calling a tool unless you allow it; consider allowing the read-only
tools and approving `run`, `broadcast`, and file transfers individually. A registration made for hub
0.2 keeps working: the hub ignores its `Authorization` header.

### Operate the hub

| Task                   | Command                                                                     |
| ---------------------- | --------------------------------------------------------------------------- |
| Show targets           | `C:\.temp\privy-hub\privy.exe hub status --home C:\.temp\privy-hub`         |
| Re-probe now           | add `--refresh`                                                             |
| Add machines or rotate | re-run `Sync-PrivyHubRelays.ps1`; the hub reloads `relays.json` by itself   |
| Restart                | `Stop-ScheduledTask PrivyHub; Start-ScheduledTask PrivyHub`                 |
| Upgrade                | re-run `Install-PrivyHubTask.ps1 -UpdateBinary`                             |
| Review what was run    | `Get-Content C:\.temp\privy-hub\audit.jsonl -Tail 20`                       |
| Watch what runs        | the `privy hub` console window shows every call, command, and its output    |
| Uninstall              | `Unregister-ScheduledTask PrivyHub -Confirm:$false`, then delete the folder |

Stopping the scheduled task closes its console window; kill a lingering `privy.exe` from the hosting
folder if one remains.
