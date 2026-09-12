function Assert-PrivyWindowsX64Binary {
    param(
        [Parameter(Mandatory)]
        [string] $Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Windows CLI is missing: $Path"
    }

    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $stream = [System.IO.File]::OpenRead($resolvedPath)
    $reader = [System.IO.BinaryReader]::new($stream)
    try {
        if ($stream.Length -lt 70 -or $reader.ReadUInt16() -ne 0x5A4D) {
            throw "Windows CLI is not a PE executable: $resolvedPath"
        }

        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        if ($peOffset -lt 64 -or $peOffset -gt ($stream.Length - 6)) {
            throw "Windows CLI has an invalid PE header offset: $resolvedPath"
        }

        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) {
            throw "Windows CLI has an invalid PE signature: $resolvedPath"
        }
        if ($reader.ReadUInt16() -ne 0x8664) {
            throw "Windows CLI is not an x86_64 executable: $resolvedPath"
        }
    }
    finally {
        $reader.Dispose()
        $stream.Dispose()
    }

    if ((Get-Item -LiteralPath $resolvedPath).Length -eq 0) {
        throw "Windows CLI is empty: $resolvedPath"
    }
}

function Invoke-PrivyWindowsSmokeTests {
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter(Mandatory)]
        [string] $RuntimeTemp
    )

    $commands = @(
        @{ Label = "--version"; Arguments = @("--version") }
        @{ Label = "client --help"; Arguments = @("client", "--help") }
        @{ Label = "server --help"; Arguments = @("server", "--help") }
        @{ Label = "proxy --help"; Arguments = @("proxy", "--help") }
        @{ Label = "file --help"; Arguments = @("file", "--help") }
        @{ Label = "token --help"; Arguments = @("token", "--help") }
    )

    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    New-Item -ItemType Directory -Force -Path $RuntimeTemp | Out-Null
    try {
        $env:TEMP = $RuntimeTemp
        $env:TMP = $RuntimeTemp
        foreach ($command in $commands) {
            & $Path @($command.Arguments) *> $null
            if ($LASTEXITCODE -ne 0) {
                throw "Windows CLI smoke test failed: privy $($command.Label)"
            }
        }
    }
    finally {
        $env:TEMP = $oldTemp
        $env:TMP = $oldTmp
        Remove-Item -LiteralPath $RuntimeTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
