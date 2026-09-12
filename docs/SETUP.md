# Setup

## Prerequisites

- An Azure subscription and Azure CLI login.
- Permission to create an Azure Relay namespace, Hybrid Connection, and authorization rules.
- Python plus `uv`, or the standalone Linux x86_64 binary.

## Provision Azure Relay

The following commands are safe to rerun after replacing the example values:

```bash
SUB='<subscription-id>'
RG='privy'
NS='privy-relay'
HC='privy'
LISTEN_RULE='privy-listen'
SEND_RULE='privy-send'

az account set --subscription "$SUB"
az group create --name "$RG" --location eastus
az relay namespace create --resource-group "$RG" --name "$NS" --location eastus
az relay hyco create \
  --resource-group "$RG" \
  --namespace-name "$NS" \
  --name "$HC" \
  --requires-client-authorization true

az relay hyco authorization-rule create \
  --resource-group "$RG" \
  --namespace-name "$NS" \
  --hybrid-connection-name "$HC" \
  --name "$LISTEN_RULE" \
  --rights Listen

az relay hyco authorization-rule create \
  --resource-group "$RG" \
  --namespace-name "$NS" \
  --hybrid-connection-name "$HC" \
  --name "$SEND_RULE" \
  --rights Send
```

Use a Listen-only rule on the server and a Send-only rule on clients. A compromised consumer can
then send work but cannot register a rogue listener.

## Use signing keys directly

Fetch the rule key on the appropriate machine:

```bash
KEY=$(az relay hyco authorization-rule keys list \
  --resource-group "$RG" \
  --namespace-name "$NS" \
  --hybrid-connection-name "$HC" \
  --name "$SEND_RULE" \
  --query primaryKey \
  --output tsv)

cat > .env <<EOF
PRIVY_RELAY_NAMESPACE=$NS
PRIVY_RELAY_PATH=$HC
PRIVY_RELAY_KEYRULE=$SEND_RULE
PRIVY_RELAY_KEY=$KEY
EOF
chmod 600 .env
```

Load it only into the process that needs it:

```bash
set -a
source .env
set +a
```

Signing keys are long-lived credentials. Prefer the broker flow in
[Authentication](AUTHENTICATION.md) for remote or shared machines.

## Use a pre-minted token

A consumer only needs:

```bash
export PRIVY_RELAY_NAMESPACE="$NS"
export PRIVY_RELAY_PATH="$HC"
export PRIVY_RELAY_TOKEN='SharedAccessSignature ...'
```

The Python constructors accept the same shape through `token=...`. A token and `keyrule`/`key` are
mutually exclusive.

Both roles support tokens: give the listener a Listen-rule token and each consumer a Send-rule
token. See [Authentication](AUTHENTICATION.md) for direct generation from an Entra-authenticated
Azure CLI key lookup and for an Entra JWT token-broker exchange.

## Next steps

- [Authentication](AUTHENTICATION.md) explains minting and refreshing short-lived tokens.
- [Server](SERVER.md) starts the remote listener.
- [CLI](CLI.md) runs commands from the client.
