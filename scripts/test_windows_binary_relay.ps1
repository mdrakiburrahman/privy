[CmdletBinding()]
param(
    [string] $BinaryPath = "dist\privy.exe",
    [string] $EnvFile,
    [ValidateRange(5, 300)]
    [int] $StartupTimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$binary = if ([System.IO.Path]::IsPathRooted($BinaryPath)) {
    $BinaryPath
}
else {
    Join-Path $repoRoot $BinaryPath
}
$runtimeTemp = Join-Path $repoRoot ".temp\privy-relay-runtime"
$logRoot = Join-Path $repoRoot ".temp"
$logId = [Guid]::NewGuid().ToString("N")
$stdoutLog = Join-Path $logRoot "privy-server-$logId.stdout.log"
$stderrLog = Join-Path $logRoot "privy-server-$logId.stderr.log"
$hubStdoutLog = Join-Path $logRoot "privy-hub-$logId.stdout.log"
$hubStderrLog = Join-Path $logRoot "privy-hub-$logId.stderr.log"
$requiredVariables = @(
    "PRIVY_RELAY_NAMESPACE"
    "PRIVY_RELAY_PATH"
    "PRIVY_RELAY_KEYRULE"
    "PRIVY_RELAY_KEY"
)

. (Join-Path $PSScriptRoot "lib\environment.ps1")
. (Join-Path $PSScriptRoot "lib\windows-binary.ps1")

function Get-RedactedLog {
    param(
        [string] $Path,
        [hashtable] $Secrets
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    $text = Get-Content -LiteralPath $Path -Raw
    foreach ($secret in $Secrets.Values) {
        if ($secret) {
            $text = $text.Replace([string] $secret, "[REDACTED]")
        }
    }
    return $text.Trim()
}

function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try {
        return ([System.Net.IPEndPoint] $listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Test-PrivyHub {
    param(
        [string] $Binary,
        [hashtable] $RelayValues,
        [string] $HubHome,
        [string] $ListenerId
    )

    New-Item -ItemType Directory -Force -Path $HubHome | Out-Null
    $relays = @{
        version = 1
        relays  = @(
            @{
                name               = "gci"
                namespace          = $RelayValues["PRIVY_RELAY_NAMESPACE"]
                hybrid_connections = @(
                    @{
                        path    = $RelayValues["PRIVY_RELAY_PATH"]
                        keyrule = $RelayValues["PRIVY_RELAY_KEYRULE"]
                        key     = $RelayValues["PRIVY_RELAY_KEY"]
                    }
                )
            }
        )
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $HubHome "relays.json"),
        ($relays | ConvertTo-Json -Depth 6),
        [System.Text.UTF8Encoding]::new($false)
    )

    $port = Get-FreeLoopbackPort
    $url = "http://127.0.0.1:$port"
    $script:hub = Start-Process `
        -FilePath $Binary `
        -ArgumentList @("hub", "serve", "--home", "`"$HubHome`"", "--port", "$port") `
        -PassThru `
        -NoNewWindow `
        -RedirectStandardOutput $hubStdoutLog `
        -RedirectStandardError $hubStderrLog

    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    while ($true) {
        if ($script:hub.HasExited) {
            $hubError = Get-RedactedLog -Path $hubStderrLog -Secrets $RelayValues
            throw "Windows binary hub exited before serving (exit $($script:hub.ExitCode)): $hubError"
        }
        try {
            $null = Invoke-RestMethod -Uri "$url/healthz" -TimeoutSec 5
            break
        }
        catch {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "Windows binary hub did not become healthy at $url within $StartupTimeoutSeconds seconds"
            }
            Start-Sleep -Milliseconds 500
        }
    }

    $targetId = "gci/$($RelayValues['PRIVY_RELAY_PATH'])"
    $quickstart = Invoke-RestMethod -Uri "$url/" -TimeoutSec 120
    if ("$quickstart" -notmatch [regex]::Escape("| $targetId |")) {
        throw "Windows binary hub quickstart does not list $targetId as online"
    }
    $targets = Invoke-RestMethod -Uri "$url/v1/targets?refresh=true" -TimeoutSec 120
    $target = @($targets.targets | Where-Object { $_.id -eq $targetId })[0]
    if (-not $target -or $target.status -ne "online") {
        throw "Windows binary hub reports $targetId as '$($target.status)': $($target.reason)"
    }

    $body = @{
        target   = $targetId
        language = "powershell"
        code     = 'Write-Output $env:PRIVY_TEST_SERVER_ID'
        wait_s   = 60
    } | ConvertTo-Json
    $job = Invoke-RestMethod `
        -Method Post `
        -Uri "$url/v1/jobs" `
        -ContentType "application/json" `
        -Body $body `
        -TimeoutSec 120
    if ($job.state -ne "succeeded" -or "$($job.stdout)".Trim() -ne $ListenerId) {
        throw "Windows binary hub job did not reach the launched listener: state=$($job.state) error=$($job.error)"
    }

    $stream = @(& curl.exe -sN --max-time 120 "$url/v1/jobs?stream=true&target=$targetId" --data-binary 'Write-Output $env:PRIVY_TEST_SERVER_ID')
    if ($LASTEXITCODE -ne 0 -or $stream -notcontains $ListenerId -or $stream[-1] -notmatch ' state=succeeded exit=0 ') {
        throw "Windows binary hub stream did not carry the listener's output: $($stream -join ' | ')"
    }
    Test-PrivyNativeTerminals -Url $url -TargetId $targetId
    Write-Host "Windows binary hub probed the listener and ran PowerShell on it through the Relay."
}

function Test-PrivyNativeTerminals {
    param([string] $Url, [string] $TargetId)

    $detail = Invoke-RestMethod -Uri "$Url/v1/targets/$([Uri]::EscapeDataString($TargetId))?refresh=true" -TimeoutSec 120
    $capability = $detail.listener_info.capabilities.terminal
    if ($capability.version -ne 1 -or "powershell" -notin $capability.shells) {
        throw "Windows binary listener does not advertise native PowerShell terminals"
    }
    foreach ($shell in @("powershell", "bash")) {
        if ($shell -notin $capability.shells) {
            continue
        }
        $owner = [Guid]::NewGuid().ToString("N")
        $id = [Guid]::NewGuid().ToString("N")
        $base = "$Url/v1/terminals/$id"
        try {
            $open = @{ owner = $owner; request_id = $id; target = $TargetId; shell = $shell; cols = 90; rows = 28 }
            $result = Invoke-RestMethod -Method Post -Uri "$Url/v1/terminals" -ContentType "application/json" -Body ($open | ConvertTo-Json) -TimeoutSec 120
            if (-not $result.ok -or $result.state -ne "running") {
                throw "Frozen $shell terminal did not start"
            }
            $commands = if ($shell -eq "powershell") {
                @('$PrivyNative="persisted"', 'Write-Output ("NATIVE=" + $PrivyNative)')
            }
            else {
                @('PRIVY_NATIVE=persisted', 'printf "NATIVE=%s\n" "$PRIVY_NATIVE"')
            }
            for ($seq = 0; $seq -lt $commands.Count; $seq++) {
                $inputBody = @{
                    owner = $owner; seq = $seq; user_input = $true
                    data_b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($commands[$seq] + "`r"))
                }
                $result = Invoke-RestMethod -Method Post -Uri "$base/write" -ContentType "application/json" -Body ($inputBody | ConvertTo-Json) -TimeoutSec 120
                if (-not $result.ok) {
                    throw "Frozen $shell terminal rejected input"
                }
            }
            $output = ""
            $carry = ""
            $offset = 0
            $deadline = [DateTime]::UtcNow.AddSeconds(45)
            while (-not $output.Contains("NATIVE=persisted") -and [DateTime]::UtcNow -lt $deadline) {
                $read = @{ owner = $owner; offset = $offset; wait_s = 1 } | ConvertTo-Json
                $result = Invoke-RestMethod -Method Post -Uri "$base/read" -ContentType "application/json" -Body $read -TimeoutSec 120
                if (-not $result.ok) {
                    throw "Frozen $shell terminal could not be read"
                }
                $chunk = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($result.data_b64))
                $output += $chunk
                $carry += $chunk
                $queries = [regex]::Matches($carry, [regex]::Escape(([string][char]27 + "[6n"))).Count
                if ($queries) {
                    $reply = @{
                        owner = $owner; seq = $seq; user_input = $false
                        data_b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((([string][char]27 + "[1;1R") * $queries)))
                    }
                    $ack = Invoke-RestMethod -Method Post -Uri "$base/write" -ContentType "application/json" -Body ($reply | ConvertTo-Json) -TimeoutSec 120
                    if (-not $ack.ok) {
                        throw "Frozen $shell terminal rejected its device response"
                    }
                    $seq++
                }
                if ($carry.Length -gt 3) {
                    $carry = $carry.Substring($carry.Length - 3)
                }
                $offset = $result.offset
                if ($result.state -ne "running" -and -not $output.Contains("NATIVE=persisted")) {
                    throw "Frozen $shell terminal exited before preserving state"
                }
            }
            if (-not $output.Contains("NATIVE=persisted")) {
                throw "Frozen $shell terminal did not preserve state across separate input requests"
            }
            Write-Host "Frozen $shell terminal preserved state through the Relay."
        }
        finally {
            $close = @{ owner = $owner; target = $TargetId } | ConvertTo-Json
            $result = Invoke-RestMethod -Method Post -Uri "$base/close" -ContentType "application/json" -Body $close -TimeoutSec 120
            if (-not $result.ok) {
                throw "Frozen $shell terminal cleanup failed"
            }
        }
    }
}

Assert-PrivyWindowsX64Binary -Path $binary
$configuredValues = Get-PrivyEnvironmentValues -EnvFile $EnvFile
$relayValues = @{}
foreach ($name in $requiredVariables) {
    $value = $configuredValues[$name]
    if (-not $value) {
        throw "Relay environment is missing $name"
    }
    $relayValues[$name] = $value
}

$savedEnvironment = @{}
foreach ($name in @($requiredVariables + "PRIVY_TEST_SERVER_ID" + "TEMP" + "TMP")) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

$server = $null
$hub = $null
$lastClientOutput = ""
$listenerId = [Guid]::NewGuid().ToString("N")
New-Item -ItemType Directory -Force -Path $runtimeTemp, $logRoot | Out-Null
try {
    foreach ($name in $requiredVariables) {
        [Environment]::SetEnvironmentVariable($name, $relayValues[$name], "Process")
    }
    $env:PRIVY_TEST_SERVER_ID = $listenerId
    $env:TEMP = $runtimeTemp
    $env:TMP = $runtimeTemp

    $server = Start-Process `
        -FilePath $binary `
        -ArgumentList @("server") `
        -PassThru `
        -NoNewWindow `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog

    [Environment]::SetEnvironmentVariable("PRIVY_TEST_SERVER_ID", $savedEnvironment["PRIVY_TEST_SERVER_ID"], "Process")
    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $expectedPrefix = "$listenerId|X64|Microsoft Windows"
    $code = @"
Write-Output (
    `$env:PRIVY_TEST_SERVER_ID + '|' +
    [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture + '|' +
    [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
)
"@

    $roundTripSucceeded = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($server.HasExited) {
            $serverError = Get-RedactedLog -Path $stderrLog -Secrets $relayValues
            throw "Windows binary server exited before listening (exit $($server.ExitCode)): $serverError"
        }

        $clientOutput = & $binary client --powershell $code --timeout-s 10 2>&1
        $clientExitCode = $LASTEXITCODE
        $lastClientOutput = ($clientOutput | Out-String).Trim()
        if ($clientExitCode -eq 0 -and $lastClientOutput.StartsWith($expectedPrefix)) {
            Write-Host "Windows binary PowerShell server/client Relay round trip succeeded."
            $roundTripSucceeded = $true
            break
        }
        Start-Sleep -Seconds 1
    }

    if (-not $roundTripSucceeded) {
        throw "Windows binary client did not reach the launched server within $StartupTimeoutSeconds seconds. Last response: $lastClientOutput"
    }

    Test-PrivyHub `
        -Binary $binary `
        -RelayValues $relayValues `
        -HubHome (Join-Path $runtimeTemp "hub") `
        -ListenerId $listenerId
}
finally {
    if ($hub) {
        if (-not $hub.HasExited) {
            $hub.Kill($true)
        }
        $hub.WaitForExit()
        $hub.Dispose()
    }
    if ($server) {
        if (-not $server.HasExited) {
            $server.Kill($true)
        }
        $server.WaitForExit()
        $server.Dispose()
    }
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
    Remove-Item -LiteralPath $runtimeTemp -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stdoutLog, $stderrLog, $hubStdoutLog, $hubStderrLog -Force -ErrorAction SilentlyContinue
}
