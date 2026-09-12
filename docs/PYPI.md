# Python package feeds

Some corporate networks block direct access to PyPI and provide an approved PEP 503-compatible
mirror instead. Privy supports that setup through uv's standard environment variables or local
configuration. With neither configured, uv continues to use public PyPI by default.

## Configure a mirror with environment variables

Environment variables are the simplest option for developer workstations and CI. Name the index so
its credentials can be supplied separately from its URL.

PowerShell:

```powershell
$env:UV_DEFAULT_INDEX = "company=https://packages.example.com/organization/project/_packaging/feed/pypi/simple/"
$env:UV_NATIVE_TLS = "true"
$env:UV_INDEX_COMPANY_USERNAME = "<username>"
$env:UV_INDEX_COMPANY_PASSWORD = "<token-or-password>"

uv sync --locked

Remove-Item Env:UV_DEFAULT_INDEX, Env:UV_NATIVE_TLS
Remove-Item Env:UV_INDEX_COMPANY_USERNAME, Env:UV_INDEX_COMPANY_PASSWORD
```

Bash:

```bash
export UV_DEFAULT_INDEX='company=https://packages.example.com/organization/project/_packaging/feed/pypi/simple/'
export UV_NATIVE_TLS=true
export UV_INDEX_COMPANY_USERNAME='<username>'
export UV_INDEX_COMPANY_PASSWORD='<token-or-password>'

uv sync --locked

unset UV_DEFAULT_INDEX UV_NATIVE_TLS
unset UV_INDEX_COMPANY_USERNAME UV_INDEX_COMPANY_PASSWORD
```

`UV_DEFAULT_INDEX` replaces public PyPI for that process. If it is unset, uv falls back to its
normal public PyPI default. The mirror must provide or upstream every required package.

For a repository whose checked-in lockfile records public PyPI, changing the default index can make
`uv sync --locked` reject the lock rather than rewriting its package sources. Preserve the public
lockfile by exporting its pinned requirements without local index configuration, then install those
requirements from the injected mirror:

```powershell
New-Item -ItemType Directory -Force .temp | Out-Null
uv export --frozen --no-config --no-hashes --no-default-groups --group binary `
  --output-file .temp\binary-requirements.txt
uv venv
uv pip sync .temp\binary-requirements.txt
```

The requirements remain version-pinned while downloads use the mirror configured in the current
environment. Do not run `uv lock` against a private mirror when the resulting lockfile is intended
for a public repository because registry URLs are recorded in the lock.

## Configure a persistent local mirror

Create `uv.toml` in the repository root:

```toml
native-tls = true

[[index]]
name = "company"
url = "https://packages.example.com/organization/project/_packaging/feed/pypi/simple/"
default = true
authenticate = "always"
```

`uv.toml` is gitignored because feed selection is environment-specific. It contains no credentials,
and it affects only developers who create the file. Do not commit an internal feed URL, username,
access token, or password to this public repository.

- `default = true` replaces public PyPI with the approved mirror. The mirror must provide or
  upstream every required package.
- `native-tls = true` lets uv use corporate certificate authorities installed in the operating
  system trust store.
- `authenticate = "always"` makes uv resolve credentials before contacting a feed that requires
  authentication.

Confirm that the file will not be committed:

```bash
git check-ignore -v uv.toml
```

## Supply credentials separately

Keep credentials outside `uv.toml`. For an index named `company`, uv recognizes
`UV_INDEX_COMPANY_USERNAME` and `UV_INDEX_COMPANY_PASSWORD`.

As an alternative, use `uv auth login` or an organization-approved credential provider so the
credential is stored outside the checkout. Follow the feed provider's instructions for the required
username and token type.

## CI configuration

Inject `UV_DEFAULT_INDEX`, `UV_NATIVE_TLS`, and the named-index credential variables from the CI
system's variable and secret stores only for commands that access the feed. Never upload local uv
configuration, credential files, or decoded secret payloads as build artifacts.

## Troubleshooting

- **TLS or certificate errors:** retain `native-tls = true` and verify that the corporate root
  certificate is installed on the machine.
- **401 or 403 responses:** verify the feed URL, credential scope, and named-index environment
  variable prefix.
- **Package unavailable:** confirm that the mirror contains or upstreams public dependencies and all
  locked versions.
- **Unexpected public PyPI access:** ensure the mirror has `default = true` rather than being
  configured only as an additional index.
