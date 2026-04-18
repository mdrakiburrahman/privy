# privy

Remote Python/bash execution over Azure Relay. Server runs in a Fabric notebook; clients POST code and get back stdout/stderr/exit_code.

## Setup

```bash
SUB="..."
RG="..."
NS="..."
HC="demo"
RULE="demo-listen-send"

az account set --subscription "$SUB"
az relay hyco create -g "$RG" --namespace-name "$NS" -n "$HC" --requires-client-authorization false 2>/dev/null || true
az relay hyco authorization-rule create -g "$RG" --namespace-name "$NS" --hybrid-connection-name "$HC" -n "$RULE" --rights Listen Send 2>/dev/null || true
KEY=$(az relay hyco authorization-rule keys list -g "$RG" --namespace-name "$NS" --hybrid-connection-name "$HC" -n "$RULE" --query primaryKey -o tsv)

jq -n \
  --arg namespace "$NS" \
  --arg path "$HC" \
  --arg keyrule "$RULE" \
  --arg key "$KEY" \
  '{namespace:$namespace, path:$path, keyrule:$keyrule, key:$key}' > credentials.json
```

```bash
cp .env.template .env      # fill in values
set -a; source .env; set +a
uv sync                    # re-run after any pyproject.toml change
```

## Build

```bash
uv build                   # → dist/privy-0.0.1-py3-none-any.whl
```

## Test

```bash
uv run pytest              # 21 tests; e2e ones hit real Relay
```

## Lint

```bash
uv run ruff check .        # static checks
uv run ruff format .       # autoformat
```

## Upload wheel

```bash
./scripts/upload_whl.sh    # az storage blob upload --overwrite
```

## Run server locally (two terminals)

Terminal 1 — server:

```bash
set -a; source .env; set +a
uv run python -c "
import os
from privy import RelayServer
RelayServer(
    namespace=os.environ['PRIVY_RELAY_NAMESPACE'],
    path=os.environ['PRIVY_RELAY_PATH'],
    keyrule=os.environ['PRIVY_RELAY_KEYRULE'],
    key=os.environ['PRIVY_RELAY_KEY'],
).serve_forever()
"
```

Terminal 2 — client:

```bash
set -a; source .env; set +a
uv run python -c "
import os
from privy import RelayClient
c = RelayClient(
    namespace=os.environ['PRIVY_RELAY_NAMESPACE'],
    path=os.environ['PRIVY_RELAY_PATH'],
    keyrule=os.environ['PRIVY_RELAY_KEYRULE'],
    key=os.environ['PRIVY_RELAY_KEY'],
)
print(c.run_bash('echo hello from privy').stdout)
print(c.run_python('import sys; print(sys.version)').stdout)
"
```

## Fabric notebook (server)

```python
%pip install --force-reinstall https://rakirahman.blob.core.windows.net/public/whls/privy-0.0.1-py3-none-any.whl
```

```python
from privy import RelayServer
RelayServer(namespace="...", path="...", keyrule="...", key="...").serve_forever()
```

## Client

```python
import os
from privy import RelayClient

c = RelayClient(
    namespace=os.environ["PRIVY_RELAY_NAMESPACE"],
    path=os.environ["PRIVY_RELAY_PATH"],
    keyrule=os.environ["PRIVY_RELAY_KEYRULE"],
    key=os.environ["PRIVY_RELAY_KEY"],
)

r = c.run_bash("pip install pandas")
r = c.run_python("import pandas; print(pandas.__version__)")
print(r.exit_code, r.stdout, r.stderr)
```
