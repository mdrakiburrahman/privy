# Install Privy

Privy is available as a Python package and as self-contained command-line executables for Linux
x86_64 and Windows x86_64.

## Windows x86_64

Download the latest executable in PowerShell:

```powershell
$installDirectory = Join-Path $HOME "bin"
$binary = Join-Path $installDirectory "privy.exe"

New-Item -ItemType Directory -Force $installDirectory | Out-Null
Invoke-WebRequest `
  -Uri "https://rakirahman.blob.core.windows.net/public/bins/privy-windows-x86_64.exe" `
  -OutFile $binary
Unblock-File -LiteralPath $binary
& $binary --help
```

Privy is a hobby project and its Windows executable is intentionally not Authenticode-signed.
Windows may therefore mark the downloaded file as coming from the internet. `Unblock-File` removes
that mark from this file only; do not disable SmartScreen, application control, or PowerShell
execution policy system-wide.

To make `privy` available in future PowerShell sessions, add its directory to the user PATH:

```powershell
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$entries = @($userPath -split ";" | Where-Object { $_ })
if ($installDirectory -notin $entries) {
    [Environment]::SetEnvironmentVariable(
        "Path",
        (($entries + $installDirectory) -join ";"),
        "User"
    )
}
```

Open a new PowerShell session after changing PATH.

Some managed Windows environments also enforce application-control rules on DLLs extracted by
single-file applications. Those policies are separate from the unsigned-download warning and must be
handled through the organization's approved allow-list process rather than by disabling system
protections.

## Linux x86_64

```bash
curl -fsSL \
  https://rakirahman.blob.core.windows.net/public/bins/privy-linux-x86_64 \
  -o privy
chmod +x privy
./privy --help
```

## Python package

From a source checkout:

```bash
uv sync
uv run privy --help
```

The release workflow also publishes a versioned `py3-none-any` wheel under:

```text
https://rakirahman.blob.core.windows.net/public/whls/privy-<version>-py3-none-any.whl
```

Networks that require a private or corporate Python package mirror should follow
[Python package feed](docs/PYPI.md) rather than committing an internal index to the repository.

## Runtime dependencies

The standalone executables bundle Python for `--python --mode inprocess`.

- `--python --mode subprocess` requires `python3` or `python` on the listener's PATH.
- Bash execution requires `bash` on the listener's PATH.
- PowerShell execution prefers PowerShell 7 (`pwsh`) and falls back to Windows PowerShell
  (`powershell.exe`) on the listener's PATH.
- The packaged Linux server additionally requires `unshare` from util-linux and unprivileged user
  namespaces for credential isolation.
- The Windows server does not provide Linux's PID/proc namespace isolation. It still removes Relay
  secrets from its process environment after startup.

## Build from source

Linux:

```bash
./scripts/build_binary.sh
```

Windows:

```powershell
.\scripts\build_binary.ps1
```

The resulting files are `dist/privy` and `dist\privy.exe`, respectively. PyInstaller must run on the
target operating system; these executables are not cross-compiled.
