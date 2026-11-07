$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$deployScript = Join-Path $PSScriptRoot "..\scripts\deploy_infra.ps1"
$global:PrivyInfraCalls = @()
$global:PrivyInfraFailAzure = $false
$global:PrivyInfraExistingServer = $false
$global:PrivyInfraWideFirewall = $false
$tenant = "11111111-1111-1111-1111-111111111111"
$objectId = "22222222-2222-2222-2222-222222222222"
$claims = @{tid=$tenant; oid=$objectId} | ConvertTo-Json -Compress
$payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($claims)).TrimEnd('=').Replace('+','-').Replace('/','_')

function az {
    $global:PrivyInfraCalls += ,@($args)
    $global:LASTEXITCODE = if ($global:PrivyInfraFailAzure) { 17 } else { 0 }
    if ($global:PrivyInfraFailAzure) { return }
    $command = $args[0..2] -join " "
    switch -Wildcard ($command) {
        "account show *" { @{tenantId=$tenant; user=@{type="user"; name="test@example.invalid"}} | ConvertTo-Json }
        "account get-access-token *" { @{accessToken="header.$payload.signature"} | ConvertTo-Json }
        "group list *" {
            if ($global:PrivyInfraExistingServer) { '[{"name":"privy","location":"canadacentral"}]' } else { '[]' }
        }
        "sql server list" {
            '[{"name":"privy-ce859648-sql","location":"canadacentral","minimalTlsVersion":"1.2","publicNetworkAccess":"Enabled"}]'
        }
        "sql server ad-admin" { ,@(@{sid=$objectId; tenantId=$tenant}) | ConvertTo-Json -AsArray }
        "sql server ad-only-auth" { '{"azureAdOnlyAuthentication":true}' }
        "sql server firewall-rule" {
            if ($global:PrivyInfraWideFirewall) { '[{"startIpAddress":"0.0.0.0","endIpAddress":"255.255.255.255"}]' } else { '[]' }
        }
        "sql db list" { '[]' }
        "deployment sub validate" { '{}' }
        "deployment sub create" {
            @{
                id="/test/deployment"
                properties=@{
                    provisioningState="Succeeded"
                    outputs=@{
                        serverFqdn=@{value="test.database.windows.net"}
                        serverResourceId=@{value="/test/server"}
                        databaseResourceId=@{value="/test/privy"}
                        testDatabaseResourceId=@{value="/test/privy_test"}
                        databaseSku=@{value="S0"}
                        testDatabaseSku=@{value="Basic"}
                    }
                }
            } | ConvertTo-Json -Depth 6
        }
        default { throw "Unexpected Azure command: $command" }
    }
}

function Assert-Fails {
    param([scriptblock] $Action, [string] $Message)
    $caught = $false
    try { & $Action | Out-Null }
    catch {
        $caught = $true
        if ($_.Exception.Message -notlike "*$Message*") { throw }
    }
    if (-not $caught) { throw "Expected failure containing: $Message" }
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("privy-infra-test-" + [guid]::NewGuid())
$production = Join-Path $testRoot "production\appsettings.yaml"
$test = Join-Path $testRoot "test\appsettings.yaml"
try {
    foreach ($ip in @("0.0.0.0", "10.1.2.3", "127.0.0.1", "1.2.3.256", "1.2.3.4/24", "::1", "1.2.003.4")) {
        Assert-Fails { & $deployScript -ClientIPv4Addresses $ip -ValidateOnly } "IPv4"
    }
    Assert-Fails { & $deployScript -ClientIPv4Addresses "1.2.3.4" -SettingsPath ".\appsettings.yaml" } "absolute"
    Assert-Fails { & $deployScript -ClientIPv4Addresses "1.2.3.4" -SettingsPath "C:\.temp\privy-hub\appsettings.yaml" } "protected"
    Assert-Fails { & $deployScript -ClientIPv4Addresses "1.2.3.4" -DatabaseName privy -TestDatabaseName privy } "different names"
    if ($global:PrivyInfraCalls.Count -ne 0) { throw "Invalid input reached Azure." }

    $global:PrivyInfraFailAzure = $true
    Assert-Fails { & $deployScript -ClientIPv4Addresses "1.2.3.4" -ValidateOnly } "exit code 17"
    $global:PrivyInfraFailAzure = $false
    $global:PrivyInfraCalls = @()
    & $deployScript -ClientIPv4Addresses "1.2.3.4" -SettingsPath $production -ValidateOnly
    if (Test-Path -LiteralPath $production) { throw "Validation wrote settings." }
    if (@($global:PrivyInfraCalls | Where-Object { $_[2] -eq "create" }).Count) { throw "Validation deployed resources." }

    $global:PrivyInfraExistingServer = $true
    $global:PrivyInfraWideFirewall = $true
    Assert-Fails { & $deployScript -ClientIPv4Addresses "1.2.3.4" } "firewall bypass"
    $global:PrivyInfraWideFirewall = $false
    $global:PrivyInfraCalls = @()
    $result = & $deployScript -ClientIPv4Addresses @("1.2.3.4", "5.6.7.8") -SettingsPath $production -TestSettingsPath $test | ConvertFrom-Json
    if ($result.provisioningState -ne "Succeeded") { throw "Incorrect deployment state." }
    $expected = @"
version: 1
storage:
  mode: azure_sql
  server: 'test.database.windows.net'
  database: 'privy_test'
  driver: 'ODBC Driver 18 for SQL Server'
  retention_days: 7
"@
    if ((Get-Content -LiteralPath $test -Raw).Trim() -ne $expected.Trim()) { throw "Incorrect settings schema." }
    if ((Get-Content -LiteralPath $production -Raw) -notmatch "database: 'privy'") { throw "Production settings use test database." }
    foreach ($call in $global:PrivyInfraCalls) {
        $subscriptionIndex = [Array]::IndexOf($call, "--subscription")
        if ($subscriptionIndex -lt 0 -or $call[$subscriptionIndex + 1] -ne "ce859648-30e1-4135-9d0f-8358aebfe789") {
            throw "Azure call omitted explicit subscription."
        }
        if ($call[0] -eq "deployment" -and $call[2] -eq "create") {
            $nameIndex = [Array]::IndexOf($call, "--name")
            if ($call[$nameIndex + 1] -notmatch '^privy-sql-\d{14}$') { throw "Malformed deployment name." }
        }
    }
    Assert-Fails { & $deployScript -ClientIPv4Addresses "1.2.3.4" -TestSettingsPath $test } "overwrite"
    Write-Host "Infrastructure script tests passed (guards, native failures, validation, existing-server safety, settings schema, explicit subscription)."
}
finally {
    foreach ($file in @($production, $test)) {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
    }
    foreach ($directory in @((Split-Path $production), (Split-Path $test), $testRoot)) {
        if (Test-Path -LiteralPath $directory) { Remove-Item -LiteralPath $directory }
    }
    Remove-Variable -Name PrivyInfraCalls, PrivyInfraFailAzure, PrivyInfraExistingServer, PrivyInfraWideFirewall -Scope Global
}
