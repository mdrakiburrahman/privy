# Deployment

## Development checks

```bash
./scripts/check_ci.sh
```

Real Relay tests load the local `.env`:

```bash
./scripts/run_gci.sh
```

The E2E suite starts a local listener, communicates through the configured real Hybrid Connection,
and covers execution, long jobs, tokens, file transfer, dependency graphs, and the CLI.

## Build artifacts

```bash
./scripts/build_release.sh
```

Outputs:

```text
dist/privy-<version>-py3-none-any.whl
dist/privy
```

The binary build runs version and command-help smoke checks. It uses PyInstaller plus staticx so the
target does not need Python and can use an older glibc.

Build the native Windows x86_64 executable on Windows:

```powershell
.\scripts\build_binary.ps1
```

Output:

```text
dist\privy.exe
```

The PowerShell build verifies the PE architecture and runs the same version and command-help smoke
checks. PyInstaller artifacts are built on their target operating system rather than cross-compiled.

Running the packaged CLI as a Linux listener requires `unshare` from util-linux, enabled
unprivileged user namespaces, and a dedicated non-root account. The listener uses a private PID/proc
namespace so remote execution cannot inspect credential-bearing binary launcher processes; it fails
closed when launched as root.

## Configure GitHub Actions

Use two GitHub Environments, each with an environment secret named `BASE64_ENV`:

- `gci` is available to same-repository PR branches. Its encoded environment contains only
  `PRIVY_RELAY_NAMESPACE`, `PRIVY_RELAY_PATH`, `PRIVY_RELAY_KEYRULE`, and `PRIVY_RELAY_KEY`.
- `production` is restricted to `main`. Its encoded environment is the full deployment `.env`,
  including `STORAGE_KEY` and optional storage destination overrides.

Create the relay-only GCI secret from the local `.env`:

```bash
set -a
source .env
set +a

{
  printf 'PRIVY_RELAY_NAMESPACE=%q\n' "$PRIVY_RELAY_NAMESPACE"
  printf 'PRIVY_RELAY_PATH=%q\n' "$PRIVY_RELAY_PATH"
  printf 'PRIVY_RELAY_KEYRULE=%q\n' "$PRIVY_RELAY_KEYRULE"
  printf 'PRIVY_RELAY_KEY=%q\n' "$PRIVY_RELAY_KEY"
} | base64 --wrap=0 | gh secret set BASE64_ENV \
      --env gci \
      --repo mdrakiburrahman/privy
```

Set the protected publishing secret:

```bash
base64 --wrap=0 .env | gh secret set BASE64_ENV \
  --env production \
  --repo mdrakiburrahman/privy
```

Storage destination overrides are optional:

```text
PRIVY_STORAGE_ACCOUNT
PRIVY_STORAGE_CONTAINER
PRIVY_BLOB_NAME
PRIVY_BIN_BLOB_PREFIX
```

Do not keep a repository-level `BASE64_ENV`: branch workflows could request it directly. The
`production` environment's deployment branch policy must allow only `main`.

The repository is public. Secret-backed GCI intentionally accepts only branches from this repository
and fails fork PRs. Restrict repository write access to trusted contributors because same-repository
PR workflows can execute code with the relay E2E secret.

## Gated CI

`.github/workflows/gci.yml` runs two required jobs for PRs targeting `main`:

1. `ci` runs `scripts/check_ci.sh`, builds the wheel and Linux executable with
   `scripts/build_release.sh`, verifies them, and uploads them for review. It receives no secrets.
2. `windows-ci` builds and smoke-tests `privy.exe` on `windows-latest`, verifies that it is an
   x86_64 PE executable, and uploads it for the secret-backed test.
3. `gci-linux` rejects fork-based PRs and runs `scripts/run_gci.sh` with the relay-only `gci`
   environment's `BASE64_ENV`.
4. `gci-windows` downloads the exact artifact from `windows-ci`, starts one binary server process,
   and invokes a separate binary client process through the Relay. It runs after `gci-linux` so the
   shared Hybrid Connection cannot route the request to the wrong listener.
5. The final `gci` job preserves the repository's required status-check name and fails unless both
   platform GCI jobs succeed.

PR builds never upload to Azure Storage.

Only the Relay-backed `gci` jobs are serialized because they share one Hybrid Connection. Non-secret
`ci` jobs remain parallel across PRs.

`main` is configured to require up-to-date CI and GCI results plus a pull request. No approving
review is required, and repository admins may bypass the rule.

## Publish after merge

`.github/workflows/publish.yml` runs for every push to `main`. It:

1. Builds and verifies the wheel/Linux executable on Ubuntu and the Windows executable on Windows.
2. Transfers those artifacts to one Ubuntu publish job.
3. Verifies all expected artifact types before making an upload.
4. Runs `scripts/publish_release.sh` with the `main`-restricted `production` environment's
   `BASE64_ENV`.

It does not rerun Ruff, pytest, or E2E after merge. GCI is the test gate.

Publishing has no arbitrary-ref manual dispatch. Production storage credentials are available only
to the workflow triggered from `main`.

Every workflow `run` step invokes a checked-in script. The same scripts run locally, and environment
wrappers accept `.env` locally or decoded `BASE64_ENV` in Actions. Use `--check` with
`scripts/run_gci.sh` or `scripts/publish_release.sh` to validate configuration without executing E2E
tests or uploading artifacts.

Default upload destinations:

```text
https://rakirahman.blob.core.windows.net/public/whls/privy-<version>-py3-none-any.whl
https://rakirahman.blob.core.windows.net/public/bins/privy-<version>-linux-x86_64
https://rakirahman.blob.core.windows.net/public/bins/privy-linux-x86_64
https://rakirahman.blob.core.windows.net/public/bins/privy-<version>-windows-x86_64.exe
https://rakirahman.blob.core.windows.net/public/bins/privy-windows-x86_64.exe
```

All uploads use overwrite semantics.

## Manual upload

```bash
set -a
source .env
set +a

uv build
./scripts/build_binary.sh
# Copy dist/privy.exe from a native Windows build.
./scripts/upload_whl.sh
```

The script fails when `STORAGE_KEY`, the wheel, the Linux CLI, or the Windows CLI is missing.
