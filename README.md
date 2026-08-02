# privy

Remote Python/bash execution over Azure Relay. Server runs in a Fabric notebook; clients POST code and get back stdout/stderr/exit_code.

## Setup

```bash
SUB="ce859648-30e1-4135-9d0f-8358aebfe789"
RG="fabricbenchmark"
NS="fabricbenchmark"
HC="dbt"
RULE="dbt-listen-send"

az account set --subscription "$SUB"
az group create -n "$RG" -l eastus 2>/dev/null || true
az relay namespace create -g "$RG" -n "$NS" -l eastus 2>/dev/null || true
az relay hyco create -g "$RG" --namespace-name "$NS" -n "$HC" --requires-client-authorization true 2>/dev/null || true
az relay hyco authorization-rule create -g "$RG" --namespace-name "$NS" --hybrid-connection-name "$HC" -n "$RULE" --rights Listen Send 2>/dev/null || true
KEY=$(az relay hyco authorization-rule keys list -g "$RG" --namespace-name "$NS" --hybrid-connection-name "$HC" -n "$RULE" --query primaryKey -o tsv)

cat > .env <<EOF
PRIVY_RELAY_NAMESPACE=$NS
PRIVY_RELAY_PATH=$HC
PRIVY_RELAY_KEYRULE=$RULE
PRIVY_RELAY_KEY=$KEY
EOF
```

```bash
source ~/.bashrc
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
RelayServer(namespace="...", path="...", keyrule="...", key="...", inprocess_globals={"spark": spark, "sc": sc}).serve_forever()
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

Or:

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
print(c.run_python('spark.sql(\"SHOW DATABASES\").show(truncate=False)', mode='inprocess').stdout)
"
```

# Browse a Fabric served API/UI locally

In the Fabric notebook cell where you start Privy, use `proxy_target`:

```python
from privy import RelayServer
RelayServer(
    namespace="...", path="...", keyrule="...", key="...",
    proxy_target="http://127.0.0.1:8080",
).serve_forever()
```

On your laptop — start the local proxy:

```bash
set -a; source .env; set +a
uv run python -c "
import os
from privy import ProxyClientServer

proxy = ProxyClientServer(
    namespace=os.environ['PRIVY_RELAY_NAMESPACE'],
    path=os.environ['PRIVY_RELAY_PATH'],
    keyrule=os.environ['PRIVY_RELAY_KEYRULE'],
    key=os.environ['PRIVY_RELAY_KEY'],
    local_port=3000,
)
proxy.serve_forever()
"
```

Open http://localhost:3000 in your browser and browse the UI!

## Stress test (32 concurrent calls)

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
import os, time
from concurrent.futures import ThreadPoolExecutor
from privy import RelayClient
c = RelayClient(**{k[12:].lower(): v for k, v in os.environ.items() if k.startswith('PRIVY_RELAY_')})
NUM_CALLS = 10
def worker(t):
    for i in range(NUM_CALLS):
        t0 = time.time(); c.run_bash('echo hi'); print(f'thread {t:02d} call {i:02d}: {time.time()-t0:.3f}s')
with ThreadPoolExecutor(32) as ex: list(ex.map(worker, range(32)))
"
```