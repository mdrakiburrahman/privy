# Contributing

For build, test, packaging, GCI, and publishing commands, see the
[deployment guide](../docs/DEPLOYMENT.md).

## How to use, on a Windows machine by installing WSL

1. Windows pre-reqs

   ```powershell
   winget install -e --id Microsoft.VisualStudioCode
   ```

1. Clone the repo on Windows, then check out and push the branch you want to work on:

   ```powershell
   git clone https://github.com/mdrakiburrahman/privy.git
   ```

1. From an **elevated PowerShell 7** prompt in the checkout, install [Task](https://taskfile.dev)
   and run [`bootstrap-dev-env.yaml`](bootstrap-dev-env.yaml):

   > ⚠️ This **deletes every WSL distribution** on the machine before recreating `Ubuntu-24.04`, and
   > removes Docker Desktop if you have it installed. Push any work inside WSL first.

   ```powershell
   winget install Task.Task
   $env:PATH = [Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH', 'User')
   task --taskfile contrib/bootstrap-dev-env.yaml
   ```

   Without any prompts, this recreates WSL on the drive with the most free space and clones your
   current branch into `/workspaces/privy` with this checkout's git identity. It then runs
   [`bootstrap-dev-env.sh`](bootstrap-dev-env.sh), which installs all tools idempotently and signs
   in the Azure CLI through your signed-in browser with single sign-on (no clicks), then opens the
   repo in VS Code connected to WSL. After a restart, run
   `task --taskfile contrib/bootstrap-dev-env.yaml up` instead; it keeps WSL and reopens VS Code.

1. Install the Python extension pack from VS Code's WSL terminal (optional):

   ```bash
   code --install-extension donjayamanne.python-extension-pack
   ```

1. Install recommended developer tooling (optional):

```bash
curl -fsSL https://gh.io/copilot-install | bash
$HOME/.local/bin/copilot --yolo
```

1. Login to github:

   ```bash
   gh auth login
   ```

1. If `uv` (or other tools installed by the bootstrapper) isn't found in a shell that was already
   open before the bootstrap ran, reload your shell config to pick up the updated `PATH`:

   ```bash
   source ~/.bashrc
   ```
