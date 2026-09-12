function ConvertFrom-PrivyEnvironmentText {
    param(
        [Parameter(Mandatory)]
        [string] $Content
    )

    $values = @{}
    $lineNumber = 0
    foreach ($line in $Content -split "\r?\n") {
        $lineNumber++
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        if ($trimmed -notmatch "^(?:export\s+)?(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$") {
            throw "Environment line $lineNumber is not a NAME=VALUE assignment"
        }

        $name = $Matches.name
        $value = $Matches.value.Trim()
        if ($value.Length -ge 2) {
            $first = $value[0]
            $last = $value[$value.Length - 1]
            if (($first -eq "'" -and $last -eq "'") -or ($first -eq '"' -and $last -eq '"')) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        if ($value.Contains("`r") -or $value.Contains("`n")) {
            throw "Environment value $name must be a single line"
        }
        $values[$name] = $value
    }
    return $values
}

function Get-PrivyEnvironmentValues {
    param(
        [string] $EnvFile,
        [string] $Base64Environment = $env:BASE64_ENV
    )

    if ($Base64Environment) {
        try {
            $bytes = [Convert]::FromBase64String($Base64Environment)
            $content = [Text.Encoding]::UTF8.GetString($bytes)
        }
        catch {
            throw "BASE64_ENV is not valid base64: $($_.Exception.Message)"
        }
        return ConvertFrom-PrivyEnvironmentText -Content $content
    }

    if (-not $EnvFile) {
        throw "Relay testing requires -EnvFile or BASE64_ENV"
    }
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        throw "Environment file does not exist: $EnvFile"
    }
    return ConvertFrom-PrivyEnvironmentText -Content (Get-Content -LiteralPath $EnvFile -Raw)
}
