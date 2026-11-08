$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$deployScript = Join-Path $PSScriptRoot "..\scripts\deploy_infra.ps1"
$global:PrivyInfraCalls = @()
$global:PrivyInfraFailAzure = $false
$global:PrivyInfraExistingServer = $false
$global:PrivyInfraWideFirewall = $false
$global:PrivyInfraAllowAll = $false
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
            '[{"name":"privy-sql","location":"canadacentral","minimalTlsVersion":"1.2","publicNetworkAccess":"Enabled"}]'
        }
        "sql server ad-admin" { ,@(@{sid=$objectId; tenantId=$tenant}) | ConvertTo-Json -AsArray }
        "sql server ad-only-auth" { '{"azureAdOnlyAuthentication":true}' }
        "sql server firewall-rule" {
            if ($global:PrivyInfraWideFirewall) { '[{"name":"unexpected-range","startIpAddress":"0.0.0.0","endIpAddress":"255.255.255.255"}]' }
            elseif ($global:PrivyInfraAllowAll) { '[{"name":"privy-allow-all","startIpAddress":"0.0.0.0","endIpAddress":"255.255.255.255"}]' }
            else { '[]' }
        }
        "sql db list" { '[]' }
        "deployment sub validate" { '{}' }
        "deployment sub create" {
            $parameterIndex = [Array]::IndexOf($args, "--parameters")
            $parameters = Get-Content -LiteralPath $args[$parameterIndex + 1].Substring(1) -Raw | ConvertFrom-Json
            if ($parameters.PSObject.Properties.Name -contains "clientIPv4Addresses") { throw "Deployment accepts client IPs." }
            if (@($parameters.PSObject.Properties.Name | Where-Object { $_ -like "*test*" }).Count) {
                throw "Deployment includes removed test parameters."
            }
            @{
                id="/test/deployment"
                properties=@{
                    provisioningState="Succeeded"
                    outputs=@{
                        serverFqdn=@{value="test.database.windows.net"}
                        serverResourceId=@{value="/test/server"}
                        databaseResourceId=@{value="/test/privy"}
                        databaseSku=@{value="S0"}
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
try {
    Assert-Fails { & $deployScript -ClientIPv4Addresses "1.2.3.4" -ValidateOnly } "parameter"
    Assert-Fails { & $deployScript -SettingsPath ".\appsettings.yaml" } "absolute"
    Assert-Fails { & $deployScript -SettingsPath "C:\.temp\privy-hub\appsettings.yaml" } "protected"
    Assert-Fails { & $deployScript -TestDatabaseName removed } "parameter"
    Assert-Fails { & $deployScript -TestDatabaseSku Basic } "parameter"
    Assert-Fails { & $deployScript -TestSettingsPath "C:\isolated\removed.yaml" } "parameter"
    if ($global:PrivyInfraCalls.Count -ne 0) { throw "Invalid input reached Azure." }

    $global:PrivyInfraFailAzure = $true
    Assert-Fails { & $deployScript -ValidateOnly } "exit code 17"
    $global:PrivyInfraFailAzure = $false
    $global:PrivyInfraCalls = @()
    & $deployScript -SettingsPath $production -ValidateOnly
    if (Test-Path -LiteralPath $production) { throw "Validation wrote settings." }
    if (@($global:PrivyInfraCalls | Where-Object { $_[2] -eq "create" }).Count) { throw "Validation deployed resources." }

    $global:PrivyInfraExistingServer = $true
    $global:PrivyInfraWideFirewall = $true
    Assert-Fails { & $deployScript } "firewall bypass"
    $global:PrivyInfraWideFirewall = $false
    $global:PrivyInfraCalls = @()
    $result = & $deployScript -SettingsPath $production | ConvertFrom-Json
    if ($result.provisioningState -ne "Succeeded") { throw "Incorrect deployment state." }
    if (@($result.PSObject.Properties.Name | Where-Object { $_ -like "*test*" }).Count) {
        throw "Deployment includes removed test outputs."
    }
    if ($result.firewallRule -ne "privy-allow-all" -or $result.startIpAddress -ne "0.0.0.0" -or $result.endIpAddress -ne "255.255.255.255") {
        throw "Incorrect allow-all firewall output."
    }
    $global:PrivyInfraAllowAll = $true
    & $deployScript -ValidateOnly
    & $deployScript | ConvertFrom-Json | Out-Null
    $expected = @"
version: 1
storage:
  mode: azure_sql
  server: 'test.database.windows.net'
  database: 'privy'
  driver: 'ODBC Driver 18 for SQL Server'
  retention_days: 7
"@
    if ((Get-Content -LiteralPath $production -Raw).Trim() -ne $expected.Trim()) { throw "Incorrect settings schema." }
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
    Assert-Fails { & $deployScript -SettingsPath $production } "overwrite"
    Write-Host "Infrastructure script tests passed (guards, native failures, validation, existing-server safety, settings schema, explicit subscription)."
}
finally {
    foreach ($file in @($production)) {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
    }
    foreach ($directory in @((Split-Path $production), $testRoot)) {
        if (Test-Path -LiteralPath $directory) { Remove-Item -LiteralPath $directory }
    }
    Remove-Variable -Name PrivyInfraCalls, PrivyInfraFailAzure, PrivyInfraExistingServer, PrivyInfraWideFirewall, PrivyInfraAllowAll -Scope Global
}
