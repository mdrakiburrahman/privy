# privy

Remote Python, Bash, and PowerShell execution, resumable file transfer, and HTTP proxying over Azure
Relay Hybrid Connections.

![Architecture](.imgs/relay-tunnel.png)

Privy can run as a Python package or as a self-contained Linux or Windows x86_64 CLI. The listener
is commonly hosted in a Fabric notebook or another remote compute environment; clients connect
without opening an inbound port on that environment.

## Quick start

Install the package:

```bash
uv sync
```

Configure a Relay connection with either a pre-minted token:

```bash
export PRIVY_RELAY_NAMESPACE=my-relay
export PRIVY_RELAY_PATH=privy
export PRIVY_RELAY_TOKEN='SharedAccessSignature ...'
```

Or a signing rule:

```bash
export PRIVY_RELAY_NAMESPACE=my-relay
export PRIVY_RELAY_PATH=privy
export PRIVY_RELAY_KEYRULE=my-rule
export PRIVY_RELAY_KEY='...'
```

Then start a listener and execute a command:

```bash
uv run privy server
uv run privy client --bash 'uname -a'
```

`privy --help` is the canonical runtime reference. It prints the complete command tree, every option
and environment variable, async behavior, batch schema, transfer commands, and token minting
examples in one deterministic plain-text document.

## Documentation

| Guide                                    | Contents                                                                                      |
| ---------------------------------------- | --------------------------------------------------------------------------------------------- |
| [Installation](INSTALL.md)               | Install the Python package or standalone Linux and Windows executables.                       |
| [Setup](docs/SETUP.md)                   | Provision Azure Relay, configure credentials, and split Listen/Send rules.                    |
| [Server](docs/SERVER.md)                 | Run the listener, expose notebook globals, manage long jobs, and proxy a local service.       |
| [CLI](docs/CLI.md)                       | Complete command behavior, output and exit contracts, dependency manifests, and runtime help. |
| [Python SDK](docs/SDK.md)                | Execute code, submit jobs, run dependency graphs, and transfer files.                         |
| [Authentication](docs/AUTHENTICATION.md) | Injected SAS tokens, providers, validation, expiry, TTLs, and broker flows.                   |
| [File transfer](docs/FILE_TRANSFER.md)   | Chunking, resume, SHA-256 integrity, paths, and overwrite behavior.                           |
| [HTTP proxy](docs/PROXY.md)              | Browse or call a remote HTTP service through the Relay listener.                              |
| [Deployment](docs/DEPLOYMENT.md)         | Build the wheel/CLI, run GCI, publish after merge, and download artifacts.                    |
| [Python package feeds](docs/PYPI.md)     | Use an approved package mirror when direct PyPI access is blocked.                            |
| [Contributing](contrib/README.md)        | Bootstrap the development environment.                                                        |

## Markdown checks

```bash
./scripts/check_markdown.sh --fix
./scripts/check_markdown.sh
```

## Download the standalone CLI

Linux:

```bash
export PRIVY_RELAY_NAMESPACE=my-relay
export PRIVY_RELAY_PATH=my-path
export PRIVY_RELAY_KEYRULE=all
export PRIVY_RELAY_KEY=...

mkdir -p .temp
cd .temp
curl -fsSL https://rakirahman.blob.core.windows.net/public/bins/privy-linux-x86_64 -o privy && chmod +x privy
./privy server -v
```

Windows PowerShell:

```powershell
$env:PRIVY_RELAY_NAMESPACE = "my-relay"
$env:PRIVY_RELAY_PATH = "my-path"
$env:PRIVY_RELAY_KEYRULE = "all"
$env:PRIVY_RELAY_KEY = "..."

$work = "C:\.temp\privy"
New-Item -ItemType Directory -Force $work | Out-Null

$env:TEMP = $work
$env:TMP = $work

$exe = "$work\privy.exe"
Invoke-WebRequest "https://rakirahman.blob.core.windows.net/public/bins/privy-windows-x86_64.exe" -OutFile $exe
Unblock-File $exe

& $exe client --bash "uname -a"
```

See [INSTALL.md](INSTALL.md) for PATH setup, the unsigned Windows binary policy, package-feed
configuration, runtime dependencies, and source builds.

The binary bundles Python for in-process execution. A real `python3` on `PATH` is still required for
`--python --mode subprocess`. Bash requires `bash`; PowerShell uses `pwsh` when available and falls
back to Windows PowerShell. Python `--mode inprocess` needs neither external runtime.

## License

Privy is distributed under the terms of the [MIT License](LICENSE).
