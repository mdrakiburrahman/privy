# Authentication

Privy supports Azure Relay SAS signing keys for compatibility and injected short-lived SAS tokens for least-privilege consumers.

Both sides can use injected tokens:

| Role | Azure Relay rule | Privy entry point |
| --- | --- | --- |
| Listener/server | `Listen` | `RelayServer(..., token=...)` or `privy server --token ...` |
| Consumer/client | `Send` | `RelayClient(..., token=...)`, `ProxyClientServer(..., token=...)`, or the matching CLI commands |

## Credential shapes

All three entry points accept exactly one shape:

```python
RelayClient(namespace="...", path="...", token="SharedAccessSignature ...")
RelayServer(namespace="...", path="...", token=token_provider)
ProxyClientServer(namespace="...", path="...", token="SharedAccessSignature ...")
```

Or:

```python
RelayClient(
    namespace="...",
    path="...",
    keyrule="privy-send",
    key="...",
    ttl_seconds=3600,
)
```

`token` may be a string or a zero-argument callable returning a string. It is mutually exclusive with `keyrule` and `key`.

CLI equivalents:

```text
PRIVY_RELAY_TOKEN / --token
PRIVY_RELAY_KEYRULE / --keyrule
PRIVY_RELAY_KEY / --key
PRIVY_RELAY_TTL_SECONDS / --ttl-seconds
```

## Broker-to-consumer flow

Keep signing keys on a trusted broker, CI job, or operator machine. Configure role-specific credentials:

```bash
export PRIVY_RELAY_NAMESPACE=my-relay
export PRIVY_RELAY_PATH=privy

export PRIVY_RELAY_SEND_KEYRULE=privy-send
export PRIVY_RELAY_SEND_KEY='...'

export PRIVY_RELAY_LISTEN_KEYRULE=privy-listen
export PRIVY_RELAY_LISTEN_KEY='...'
```

Mint and hand off only the bounded token:

```bash
export PRIVY_RELAY_TOKEN="$(privy token mint --rights send --ttl 30m)"
privy client --python 'print(42)'
```

For a listener:

```bash
export PRIVY_RELAY_TOKEN="$(privy token mint --rights listen --ttl 2h)"
privy server
```

Explicit `--keyrule` and `--key` override the selected role variables for minting.

SAS rights are not an independent token field. Azure enforces the rights configured on the authorization rule whose key signed the token. `--rights` selects that rule's environment variables; it cannot turn a Listen key into a Send-only token.

## Generate tokens after Entra authentication

An operator can use an Entra-authenticated Azure CLI session to retrieve the existing authorization-rule key, then mint a bounded SAS token locally. The Entra identity needs permission to list that rule's keys.

Listener token:

```bash
az login
export PRIVY_RELAY_NAMESPACE=my-relay
export PRIVY_RELAY_PATH=privy
export PRIVY_RELAY_LISTEN_KEYRULE=privy-listen
export PRIVY_RELAY_LISTEN_KEY="$(
  az relay hyco authorization-rule keys list \
    --resource-group privy \
    --namespace-name "$PRIVY_RELAY_NAMESPACE" \
    --hybrid-connection-name "$PRIVY_RELAY_PATH" \
    --name "$PRIVY_RELAY_LISTEN_KEYRULE" \
    --query primaryKey \
    --output tsv
)"
export PRIVY_RELAY_TOKEN="$(privy token mint --rights listen --ttl 2h)"
unset PRIVY_RELAY_LISTEN_KEY
privy server
```

Consumer token:

```bash
export PRIVY_RELAY_SEND_KEYRULE=privy-send
export PRIVY_RELAY_SEND_KEY="$(
  az relay hyco authorization-rule keys list \
    --resource-group privy \
    --namespace-name "$PRIVY_RELAY_NAMESPACE" \
    --hybrid-connection-name "$PRIVY_RELAY_PATH" \
    --name "$PRIVY_RELAY_SEND_KEYRULE" \
    --query primaryKey \
    --output tsv
)"
export PRIVY_RELAY_TOKEN="$(privy token mint --rights send --ttl 30m)"
unset PRIVY_RELAY_SEND_KEY
privy client --python 'print(42)'
```

`az relay ... keys list` uses the operator's Entra access token for Azure Resource Manager authorization. The Relay signing key remains necessary to produce a SAS signature; the Entra token does not itself become a Relay SAS token.

## Exchange an Entra JWT through a token broker

For remote consumers, place the key lookup and SAS signing behind a trusted broker:

1. The server or consumer obtains an Entra JWT whose audience is the broker API.
2. It sends that JWT to the broker and requests `listen` or `send`, the Relay path, and a bounded TTL.
3. The broker validates signature, issuer, audience, tenant, expiry, and caller authorization.
4. The broker retrieves or securely stores the corresponding existing Relay authorization-rule key.
5. The broker calls `create_sas_token(...)` (or the same logic as `privy token mint`) and returns only the SAS token.
6. The caller passes the result through `token=` or `PRIVY_RELAY_TOKEN`.

Framework-neutral broker logic:

```python
from privy import create_sas_token

def exchange(entra_jwt: str, *, role: str, namespace: str, path: str) -> str:
    principal = validate_entra_jwt(
        entra_jwt,
        expected_audience="api://privy-token-broker",
    )
    authorize(principal, role=role, namespace=namespace, path=path)
    keyrule, key = relay_key_store.get(role, namespace, path)
    ttl_seconds = 30 * 60 if role == "send" else 2 * 60 * 60
    return create_sas_token(namespace, path, keyrule, key, ttl_seconds)
```

The broker functions above are application-specific placeholders. Use a standard Entra JWT validation library and an explicit authorization policy; never decode a JWT without verifying it. Do not accept a Relay signing key from the caller, return it to the caller, or log either credential.

Azure Relay does not provide a built-in "Entra JWT to SAS" exchange endpoint. The broker is the trusted exchange boundary: Entra authenticates and authorizes the caller, while the existing Relay key signs the short-lived SAS token.

## Validation

Before every dial, privy:

1. Parses `sr`, `sig`, `se`, and `skn`.
2. Rejects malformed or expired tokens with an actionable error.
3. Verifies that `sr` covers `http://<relay-fqdn>/<hybrid-connection-path>`.
4. Keeps the token and token-bearing URL out of normal and verbose diagnostics.

An expired token reports its UTC expiration rather than surfacing an opaque Relay 401. A listener that cannot obtain a fresh token exits with CLI code `3`.

## Providers and listener expiry

Callables are resolved for every client request, proxy request, and listener connection:

```python
def token_provider() -> str:
    return secret_store.read("current-privy-token")

server = RelayServer(
    namespace="my-relay",
    path="privy",
    token=token_provider,
)
```

Azure may close a listener control channel at token expiry. Reconnection calls the provider again. If it still returns an expired token, the listener exits with code `3` instead of looping forever.

## TTLs

`privy token mint --ttl` accepts seconds or an `s`, `m`, `h`, or `d` suffix:

```bash
privy token mint --rights send --ttl 900
privy token mint --rights send --ttl 30m
privy token mint --rights listen --ttl 2h
```

The key-based constructors and connection CLI use `ttl_seconds` / `--ttl-seconds`. Their compatibility default remains 48 hours.

Very short tokens are sensitive to machine clock skew. Use a practical lifetime and synchronize host clocks.

## Least privilege

- Use a Listen-only rule for listeners.
- Use a Send-only rule for clients and local proxies.
- Do not copy the signing key to consumer machines when a token is sufficient.
- Store `.env` with restrictive permissions and never commit it.
- Rotate a rule key if it is exposed; SAS has no per-token revocation list.

When `privy server` resolves credentials on POSIX, it re-executes itself with a sanitized command line and environment, carrying the resolved configuration through a short-lived inherited pipe that is closed before accepting work. On Linux it also marks the listener non-dumpable so same-user subprocesses cannot recover credentials from `/proc/$PPID/environ` or process memory. The packaged CLI additionally starts the listener in a private PID/proc namespace so the credential-bearing StaticX and PyInstaller launchers are not visible to remote code; this requires `unshare` from util-linux, enabled unprivileged user namespaces, and a non-root service account. Subprocess execution strips Relay token/key values plus `STORAGE_KEY` and `BASE64_ENV` from every child environment.

The server cannot erase secrets retained by a separate launching shell or process. For a dedicated host, prefer a service manager that supplies credentials directly to privy, or replace an interactive shell with `exec privy server` after loading the secret. Do not place `--token` or `--key` values in reusable shell history.

For SDK-hosted listeners, prefer passing credentials from a secret provider without leaving them in `os.environ`. `mode="inprocess"` executes trusted Python inside the listener interpreter and is not a security sandbox; code granted that mode can inspect process memory and Python objects even when environment variables are scrubbed.
