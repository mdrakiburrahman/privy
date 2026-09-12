# privy

Remote Python and Bash execution, resumable file transfer, and HTTP proxying over Azure Relay Hybrid Connections.

![Architecture](.imgs/relay-tunnel.png)

Privy can run as a Python package or as a self-contained Linux x86_64 CLI. The listener is commonly hosted in a Fabric notebook or another remote compute environment; clients connect without opening an inbound port on that environment.

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

`privy --help` is the canonical runtime reference. It prints the complete command tree, every option and environment variable, async behavior, batch schema, transfer commands, and token minting examples in one deterministic plain-text document.

## Documentation

| Guide | Contents |
| --- | --- |
| [Setup](docs/SETUP.md) | Provision Azure Relay, configure credentials, and split Listen/Send rules. |
| [Server](docs/SERVER.md) | Run the listener, expose notebook globals, manage long jobs, and proxy a local service. |
| [CLI](docs/CLI.md) | Complete command behavior, output and exit contracts, dependency manifests, and runtime help. |
| [Python SDK](docs/SDK.md) | Execute code, submit jobs, run dependency graphs, and transfer files. |
| [Authentication](docs/AUTHENTICATION.md) | Injected SAS tokens, providers, validation, expiry, TTLs, and broker flows. |
| [File transfer](docs/FILE_TRANSFER.md) | Chunking, resume, SHA-256 integrity, paths, and overwrite behavior. |
| [HTTP proxy](docs/PROXY.md) | Browse or call a remote HTTP service through the Relay listener. |
| [Deployment](docs/DEPLOYMENT.md) | Build the wheel/CLI, run GCI, publish after merge, and download artifacts. |
| [Contributing](contrib/README.md) | Bootstrap the development environment. |

## Download the standalone CLI

```bash
curl -fsSL https://rakirahman.blob.core.windows.net/public/bins/privy-linux-x86_64 -o privy
chmod +x privy
./privy --help
```

The binary bundles Python for in-process execution. A real `python3` on `PATH` is still required for `--python --mode subprocess`; Bash and `--mode inprocess` work without it.

## License

Privy is distributed under the terms of the [MIT License](LICENSE).
