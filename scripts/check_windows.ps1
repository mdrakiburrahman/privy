[CmdletBinding()]
param(
    [switch] $SkipSync
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Push-Location $repoRoot
try {
    $gitBashDirectory = Join-Path $env:ProgramFiles "Git\bin"
    if (Test-Path -LiteralPath (Join-Path $gitBashDirectory "bash.exe") -PathType Leaf) {
        $env:PATH = "$gitBashDirectory;$env:PATH"
    }

    if (-not $SkipSync) {
        & uv sync --locked --group dev --group binary
        if ($LASTEXITCODE -ne 0) {
            throw "uv sync failed with exit code $LASTEXITCODE"
        }
    }

    $runArguments = if ($SkipSync) {
        @("run", "--no-sync")
    }
    else {
        @("run", "--locked")
    }

    & uv @runArguments ruff check .
    if ($LASTEXITCODE -ne 0) {
        throw "ruff check failed with exit code $LASTEXITCODE"
    }
    & uv @runArguments ruff format --check .
    if ($LASTEXITCODE -ne 0) {
        throw "ruff format check failed with exit code $LASTEXITCODE"
    }
    & uv @runArguments pytest -m "not e2e"
    if ($LASTEXITCODE -ne 0) {
        throw "pytest failed with exit code $LASTEXITCODE"
    }

    & (Join-Path $PSScriptRoot "build_binary.ps1") -SkipSync
}
finally {
    Pop-Location
}
