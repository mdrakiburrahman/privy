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
            return
        }
        Start-Sleep -Seconds 1
    }

    throw "Windows binary client did not reach the launched server within $StartupTimeoutSeconds seconds. Last response: $lastClientOutput"
}
finally {
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
    Remove-Item -LiteralPath $stdoutLog, $stderrLog -Force -ErrorAction SilentlyContinue
}
