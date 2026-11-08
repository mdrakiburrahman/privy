[CmdletBinding()]
param(
    [string] $SubscriptionId = "ce859648-30e1-4135-9d0f-8358aebfe789",
    [string] $ResourceGroupName = "privy",
    [string] $Location = "canadacentral",
    [ValidatePattern('^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')]
    [string] $ServerName = "privy-sql",
    [ValidatePattern('^[A-Za-z][A-Za-z0-9_]{0,127}$')]
    [string] $DatabaseName = "privy",
    [ValidateSet("S0", "S1", "S2", "S3")]
    [string] $DatabaseSku = "S0",
    [string] $SettingsPath,
    [switch] $ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$template = Join-Path $repoRoot "infra\main.bicep"

function Invoke-Azure {
    param([string[]] $Arguments)
    $result = & az @Arguments --subscription $SubscriptionId --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI '$($Arguments[0..([Math]::Min(2, $Arguments.Length - 1))] -join ' ')' failed with exit code $LASTEXITCODE."
    }
    if ($result) { return ($result -join "`n" | ConvertFrom-Json) }
}

function Assert-IsolatedSettingsPath {
    param([string] $Path)
    if (-not $Path) { return }
    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw "Settings paths must be explicit absolute paths."
    }
    $fullPath = [IO.Path]::GetFullPath($Path)
    $forbidden = @(
        "C:\.temp\privy-hub",
        "C:\Users\mdrrahman\privy",
        (Join-Path $HOME ".privy")
    )
    if ($env:PRIVY_HUB_HOME) { $forbidden += $env:PRIVY_HUB_HOME }
    foreach ($root in $forbidden) {
        $root = [IO.Path]::GetFullPath($root).TrimEnd('\', '/')
        if ($fullPath -eq $root -or $fullPath.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Settings path targets a protected or default hub home: $fullPath"
        }
    }
    if (Test-Path -LiteralPath $fullPath) {
        throw "Refusing to overwrite existing settings: $fullPath"
    }
}

function Write-HubSettings {
    param([string] $Path, [string] $Server, [string] $Database)
    if (-not $Path) { return }
    $content = @"
version: 1
storage:
  mode: azure_sql
  server: '$Server'
  database: '$Database'
  driver: 'ODBC Driver 18 for SQL Server'
  retention_days: 7
"@
    $parent = Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($content + "`n")
        $stream.Write($bytes, 0, $bytes.Length)
    }
    finally { $stream.Dispose() }
}

Assert-IsolatedSettingsPath $SettingsPath

$account = Invoke-Azure @("account", "show")
if ($account.user.type -ne "user") { throw "Deployment requires the signed-in user identity as Entra admin." }
# Read only the identity claims locally; never emit or persist access tokens.
$token = Invoke-Azure @("account", "get-access-token", "--resource", "https://management.azure.com/")
try {
    $payload = $token.accessToken.Split('.')[1].Replace('-', '+').Replace('_', '/')
    $payload = $payload.PadRight($payload.Length + ((4 - $payload.Length % 4) % 4), '=')
    $claims = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload)) | ConvertFrom-Json
    $objectId = [guid]::Parse($claims.oid).ToString()
    $tenantId = [guid]::Parse($claims.tid).ToString()
    if ($tenantId -ne $account.tenantId) { throw "Token tenant does not match subscription tenant." }
}
finally { Remove-Variable token, payload -ErrorAction SilentlyContinue }

$groups = @(Invoke-Azure @("group", "list"))
$group = @($groups | Where-Object name -EQ $ResourceGroupName)
$groupLocation = if ($group.Count) { $group[0].location } else { $Location }
if ($group.Count) {
    $servers = @(Invoke-Azure @("sql", "server", "list", "--resource-group", $ResourceGroupName))
    $existing = @($servers | Where-Object name -EQ $ServerName)
    if ($existing.Count) {
        $server = $existing[0]
        if ($server.location -ne $Location -or $server.minimalTlsVersion -ne "1.2" -or $server.publicNetworkAccess -ne "Enabled") {
            throw "Existing target server location/network/TLS differs; review it explicitly before deployment."
        }
        $admin = @(Invoke-Azure @("sql", "server", "ad-admin", "list", "--resource-group", $ResourceGroupName, "--server", $ServerName))
        $onlyEntra = Invoke-Azure @("sql", "server", "ad-only-auth", "get", "--resource-group", $ResourceGroupName, "--name", $ServerName)
        if ($admin.Count -ne 1 -or $admin[0].sid -ne $objectId -or $admin[0].tenantId -ne $tenantId -or -not $onlyEntra.azureAdOnlyAuthentication) {
            throw "Existing server administrator/authentication differs; refusing to change it automatically."
        }
        $rules = @(Invoke-Azure @("sql", "server", "firewall-rule", "list", "--resource-group", $ResourceGroupName, "--server", $ServerName))
        foreach ($rule in $rules) {
            $allowAll = $rule.name -eq "privy-allow-all" -and
                $rule.startIpAddress -eq "0.0.0.0" -and $rule.endIpAddress -eq "255.255.255.255"
            if (-not $allowAll -and ($rule.startIpAddress -ne $rule.endIpAddress -or $rule.startIpAddress -eq "0.0.0.0")) {
                throw "Existing target server has a range or Azure-services firewall bypass; review separately."
            }
        }
        $databases = @(Invoke-Azure @("sql", "db", "list", "--resource-group", $ResourceGroupName, "--server", $ServerName))
        foreach ($target in @(@{Name=$DatabaseName; Sku=$DatabaseSku})) {
            $existingDb = @($databases | Where-Object name -EQ $target.Name)
            if ($existingDb.Count -and ($existingDb[0].currentServiceObjectiveName -ne $target.Sku -or
                $existingDb[0].currentBackupStorageRedundancy -ne "Local" -or
                $existingDb[0].collation -ne "SQL_Latin1_General_CP1_CI_AS")) {
                throw "Existing database $($target.Name) SKU/backup/collation differs; refusing automatic changes."
            }
        }
    }
}

$parameters = @{
    resourceGroupName = @{value=$ResourceGroupName}
    resourceGroupLocation = @{value=$groupLocation}
    location = @{value=$Location}
    serverName = @{value=$ServerName}
    databaseName = @{value=$DatabaseName}
    databaseSku = @{value=$DatabaseSku}
    administratorObjectId = @{value=$objectId}
    administratorLogin = @{value=$account.user.name}
    tenantId = @{value=$tenantId}
}
$parameterFile = Join-Path ([IO.Path]::GetTempPath()) ("privy-infra-" + [guid]::NewGuid() + ".json")
try {
    $parameters | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $parameterFile -Encoding utf8
    $arguments = @("deployment", "sub", "validate", "--location", $Location,
        "--template-file", $template, "--parameters", "@$parameterFile")
    Invoke-Azure $arguments | Out-Null
    if ($ValidateOnly) {
        Write-Host "Azure Bicep deployment validation passed; no resources or settings written."
        return
    }
    $arguments[2] = "create"
    $arguments += @("--name", ("privy-sql-" + (Get-Date -Format "yyyyMMddHHmmss")))
    $deployment = Invoke-Azure $arguments
    if ($deployment.properties.provisioningState -ne "Succeeded") {
        throw "Deployment did not succeed: $($deployment.properties.provisioningState)"
    }
    $outputs = $deployment.properties.outputs
    Write-HubSettings $SettingsPath $outputs.serverFqdn.value $DatabaseName
    [pscustomobject]@{
        deploymentId = $deployment.id
        provisioningState = $deployment.properties.provisioningState
        serverFqdn = $outputs.serverFqdn.value
        serverResourceId = $outputs.serverResourceId.value
        databaseResourceId = $outputs.databaseResourceId.value
        databaseSku = $outputs.databaseSku.value
        settingsPath = $SettingsPath
        firewallRule = "privy-allow-all"
        startIpAddress = "0.0.0.0"
        endIpAddress = "255.255.255.255"
    } | ConvertTo-Json -Depth 4
}
finally { Remove-Item -LiteralPath $parameterFile -Force -ErrorAction Stop }
