$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$deployScript = Join-Path $PSScriptRoot "..\scripts\deploy_infra.ps1"
$global:PrivyInfraCalls = @()
$global:PrivyInfraFailAzure = $false
$global:PrivyInfraExistingServer = $false
$global:PrivyInfraDatabaseState = "none"
$global:PrivyInfraValidatedSku = $null
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
        "sql db list" {
            if ($global:PrivyInfraDatabaseState -eq "none") { '[]' }
            elseif ($global:PrivyInfraDatabaseState -eq "standard") {
                @{
                    name="privy"; currentServiceObjectiveName="S0"
                    requestedServiceObjectiveName="S0"
                    currentBackupStorageRedundancy="Local"; collation="SQL_Latin1_General_CP1_CI_AS"
                    sku=@{name="S0"; tier="Standard"}
                } | ConvertTo-Json -Depth 6
            }
            else {
                $database = @{
                    name="privy"; currentServiceObjectiveName="HS_Gen5_8"
                    requestedServiceObjectiveName="HS_Gen5_8"
                    currentBackupStorageRedundancy="Local"; collation="SQL_Latin1_General_CP1_CI_AS"
                    sku=@{name="HS_Gen5"; tier="Hyperscale"; family="Gen5"; capacity=8}
                    highAvailabilityReplicaCount=1; readScale="Enabled"; zoneRedundant=$false
                }
                switch ($global:PrivyInfraDatabaseState) {
                    "memoryOptimizedHyperscale" {
                        $database.currentServiceObjectiveName = "HS_MOPRMS_16"
                        $database.requestedServiceObjectiveName = "HS_MOPRMS_16"
                        $database.sku = @{name="HS_MOPRMS"; tier="Hyperscale"; family="8IH"; capacity=16}
                    }
                    "businessCritical" {
                        $database.currentServiceObjectiveName = "BC_Gen5_16"
                        $database.requestedServiceObjectiveName = "BC_Gen5_16"
                        $database.sku = @{name="BC_Gen5"; tier="BusinessCritical"; family="Gen5"; capacity=16}
                        $database.Remove("highAvailabilityReplicaCount")
                    }
                    "businessCriticalMissingZone" {
                        $database.currentServiceObjectiveName = "BC_Gen5_16"
                        $database.requestedServiceObjectiveName = "BC_Gen5_16"
                        $database.sku = @{name="BC_Gen5"; tier="BusinessCritical"; family="Gen5"; capacity=16}
                        $database.Remove("highAvailabilityReplicaCount")
                        $database.Remove("zoneRedundant")
                    }
                    "businessCriticalNullZone" {
                        $database.currentServiceObjectiveName = "BC_Gen5_16"
                        $database.requestedServiceObjectiveName = "BC_Gen5_16"
                        $database.sku = @{name="BC_Gen5"; tier="BusinessCritical"; family="Gen5"; capacity=16}
                        $database.Remove("highAvailabilityReplicaCount")
                        $database.zoneRedundant = $null
                    }
                    "businessCriticalStringZone" {
                        $database.currentServiceObjectiveName = "BC_Gen5_16"
                        $database.requestedServiceObjectiveName = "BC_Gen5_16"
                        $database.sku = @{name="BC_Gen5"; tier="BusinessCritical"; family="Gen5"; capacity=16}
                        $database.Remove("highAvailabilityReplicaCount")
                        $database.zoneRedundant = "false"
                    }
                    "businessCriticalWrongZone" {
                        $database.currentServiceObjectiveName = "BC_Gen5_16"
                        $database.requestedServiceObjectiveName = "BC_Gen5_16"
                        $database.sku = @{name="BC_Gen5"; tier="BusinessCritical"; family="Gen5"; capacity=16}
                        $database.Remove("highAvailabilityReplicaCount")
                        $database.zoneRedundant = $true
                    }
                    "businessCriticalDisabledReadScale" {
                        $database.currentServiceObjectiveName = "BC_Gen5_16"
                        $database.requestedServiceObjectiveName = "BC_Gen5_16"
                        $database.sku = @{name="BC_Gen5"; tier="BusinessCritical"; family="Gen5"; capacity=16}
                        $database.Remove("highAvailabilityReplicaCount")
                        $database.readScale = "Disabled"
                    }
                    "directHyperscaleToBusinessCritical" {
                        $database.requestedServiceObjectiveName = "BC_Gen5_16"
                    }
                }
                if ($global:PrivyInfraDatabaseState -eq "wrongHyperscale") {
                    $database.highAvailabilityReplicaCount = 0
                    $database.readScale = "Disabled"
                }
                switch ($global:PrivyInfraDatabaseState) {
                    "missingZone" { $database.Remove("zoneRedundant") }
                    "nullZone" { $database.zoneRedundant = $null }
                    "wrongZone" { $database.zoneRedundant = $true }
                    "stringZone" { $database.zoneRedundant = "false" }
                    "pendingSku" { $database.requestedServiceObjectiveName = "HS_Gen5_16" }
                    "wrongCurrentSku" { $database.currentServiceObjectiveName = "HS_Gen5_16" }
                }
                @($database) | ConvertTo-Json -Depth 6
            }
        }
        "deployment sub validate" {
            $parameterIndex = [Array]::IndexOf($args, "--parameters")
            $parameters = Get-Content -LiteralPath $args[$parameterIndex + 1].Substring(1) -Raw | ConvertFrom-Json
            $global:PrivyInfraValidatedSku = $parameters.databaseSku.value
            if ($global:PrivyInfraValidatedSku -notin @("S0", "S1", "S2", "S3", "HS_Gen5_8", "HS_MOPRMS_16", "BC_Gen5_16")) {
                throw "Unsupported database SKU reached validation."
            }
            '{}'
        }
        "deployment sub create" {
            $parameterIndex = [Array]::IndexOf($args, "--parameters")
            $parameters = Get-Content -LiteralPath $args[$parameterIndex + 1].Substring(1) -Raw | ConvertFrom-Json
            if ($parameters.databaseSku.value -ne "HS_Gen5_8") { throw "Deployment did not select the Hyperscale default." }
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
                        databaseSku=@{value="HS_Gen5_8"}
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
$mainTemplate = Get-Content -LiteralPath (Join-Path $PSScriptRoot "main.bicep") -Raw
$sqlTemplate = Get-Content -LiteralPath (Join-Path $PSScriptRoot "sql.bicep") -Raw
if ($mainTemplate -notmatch "@allowed\(\['S0', 'S1', 'S2', 'S3', 'HS_Gen5_8', 'HS_MOPRMS_16', 'BC_Gen5_16'\]\)\s*param databaseSku string = 'HS_Gen5_8'") {
    throw "Bicep SKU choices/default do not preserve Standard support and the benchmark baseline."
}
if ($sqlTemplate -notmatch "family: isMemoryOptimizedHyperscale \? '8IH' : 'Gen5'" -or
    $sqlTemplate -notmatch "name: 'BC_Gen5'\s+tier: 'BusinessCritical'\s+family: 'Gen5'\s+capacity: 16" -or
    $sqlTemplate -notmatch "(?s)properties: isHyperscale \? \{.*?zoneRedundant: false\s*\} : isBusinessCritical \? \{\s*collation: 'SQL_Latin1_General_CP1_CI_AS'\s+requestedBackupStorageRedundancy: 'Local'\s+readScale: 'Enabled'\s+zoneRedundant: false\s*\} : \{\s*collation: 'SQL_Latin1_General_CP1_CI_AS'\s+requestedBackupStorageRedundancy: 'Local'\s*\}" -or
    $sqlTemplate -notmatch "highAvailabilityReplicaCount: 1" -or
    $sqlTemplate -notmatch "readScale: 'Enabled'" -or
    $sqlTemplate -notmatch "zoneRedundant: false") {
    throw "Bicep is missing exact memory-optimized, Business Critical, or Hyperscale properties."
}
if ($sqlTemplate -notmatch "(?s)properties: isHyperscale \? \{.*?highAvailabilityReplicaCount: 1.*?\} : isBusinessCritical \? \{[^}]*\}") {
    throw "Business Critical properties must not include the Hyperscale-only HA replica count."
}
try {
    Assert-Fails { & $deployScript -ClientIPv4Addresses "1.2.3.4" -ValidateOnly } "parameter"
    Assert-Fails { & $deployScript -SettingsPath ".\appsettings.yaml" } "absolute"
    Assert-Fails { & $deployScript -SettingsPath "C:\.temp\privy-hub\appsettings.yaml" } "protected"
    Assert-Fails { & $deployScript -TestDatabaseName removed } "parameter"
    Assert-Fails { & $deployScript -TestDatabaseSku Basic } "parameter"
    Assert-Fails { & $deployScript -TestSettingsPath "C:\isolated\removed.yaml" } "parameter"
    Assert-Fails { & $deployScript -DatabaseSku HS_MOPRMS_8 } "parameter"
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
    $global:PrivyInfraDatabaseState = "wrongHyperscale"
    Assert-Fails { & $deployScript -ValidateOnly } "SKU/HA/read-scale/zone"
    foreach ($state in @("missingZone", "nullZone", "wrongZone", "stringZone", "wrongCurrentSku", "pendingSku")) {
        $global:PrivyInfraDatabaseState = $state
        $global:PrivyInfraCalls = @()
        $expectedFailure = if ($state -eq "pendingSku") { "requested SKU differs" } else { "SKU/HA/read-scale/zone" }
        Assert-Fails { & $deployScript -ValidateOnly } $expectedFailure
        if (@($global:PrivyInfraCalls | Where-Object { $_[0] -eq "deployment" }).Count) {
            throw "Unsafe database metadata reached deployment: $state"
        }
    }
    $global:PrivyInfraDatabaseState = "standard"
    & $deployScript -DatabaseSku S0 -ValidateOnly
    if ($global:PrivyInfraValidatedSku -ne "S0") { throw "Standard SKU override was not passed to validation." }
    $global:PrivyInfraDatabaseState = "hyperscale"
    $global:PrivyInfraCalls = @()
    & $deployScript -ValidateOnly
    if (@($global:PrivyInfraCalls | Where-Object { $_[0] -eq "deployment" -and $_[2] -eq "validate" }).Count -ne 1) {
        throw "Matching current/requested Hyperscale metadata did not reach validation."
    }
    if ($global:PrivyInfraValidatedSku -ne "HS_Gen5_8") { throw "Hyperscale default was not preserved." }
    $global:PrivyInfraDatabaseState = "memoryOptimizedHyperscale"
    $global:PrivyInfraCalls = @()
    & $deployScript -DatabaseSku HS_MOPRMS_16 -ValidateOnly
    if ($global:PrivyInfraValidatedSku -ne "HS_MOPRMS_16") {
        throw "Memory-optimized Hyperscale override did not pass its exact SKU to validation."
    }
    $global:PrivyInfraDatabaseState = "businessCritical"
    $global:PrivyInfraCalls = @()
    & $deployScript -DatabaseSku BC_Gen5_16 -ValidateOnly
    if ($global:PrivyInfraValidatedSku -ne "BC_Gen5_16") {
        throw "Business Critical override did not pass its exact SKU to validation."
    }
    foreach ($state in @("businessCriticalMissingZone", "businessCriticalNullZone", "businessCriticalStringZone", "businessCriticalWrongZone", "businessCriticalDisabledReadScale")) {
        $global:PrivyInfraDatabaseState = $state
        $global:PrivyInfraCalls = @()
        Assert-Fails { & $deployScript -DatabaseSku BC_Gen5_16 -ValidateOnly } "SKU/HA/read-scale/zone"
        if (@($global:PrivyInfraCalls | Where-Object { $_[0] -eq "deployment" }).Count) {
            throw "Unsafe Business Critical metadata reached deployment: $state"
        }
    }
    $global:PrivyInfraDatabaseState = "directHyperscaleToBusinessCritical"
    $global:PrivyInfraCalls = @()
    Assert-Fails { & $deployScript -DatabaseSku BC_Gen5_16 -ValidateOnly } "Direct Hyperscale-to-Business Critical"
    if (@($global:PrivyInfraCalls | Where-Object { $_[0] -eq "deployment" }).Count) {
        throw "Unsupported direct Hyperscale-to-Business Critical change reached deployment."
    }
    $global:PrivyInfraDatabaseState = "hyperscale"
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
    Remove-Variable -Name PrivyInfraCalls, PrivyInfraFailAzure, PrivyInfraExistingServer, PrivyInfraDatabaseState, PrivyInfraWideFirewall, PrivyInfraAllowAll -Scope Global
}
