[CmdletBinding()]
param(
    [switch] $SkipSync
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$binary = Join-Path $repoRoot "dist\privy.exe"

. (Join-Path $PSScriptRoot "lib\windows-binary.ps1")

Push-Location $repoRoot
try {
    if (-not $SkipSync) {
        Write-Host ">> syncing Windows binary build dependencies"
        & uv sync --locked --group binary
        if ($LASTEXITCODE -ne 0) {
            throw "uv sync failed with exit code $LASTEXITCODE"
        }
    }

    Write-Host ">> building $binary"
    $runArguments = if ($SkipSync) {
        @("run", "--no-sync")
    }
    else {
        @("run", "--locked", "--group", "binary")
    }
    & uv @runArguments pyinstaller --clean --noconfirm privy.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    Assert-PrivyWindowsX64Binary -Path $binary

    Write-Host ">> smoke testing $binary"
    Invoke-PrivyWindowsSmokeTests `
        -Path $binary `
        -RuntimeTemp (Join-Path $repoRoot ".temp\privy-smoke-runtime")

    $sizeMiB = [math]::Round((Get-Item -LiteralPath $binary).Length / 1MB, 1)
    Write-Host ">> done: $binary ($sizeMiB MiB)"
}
finally {
    Pop-Location
}
