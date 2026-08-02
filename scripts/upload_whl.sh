#!/usr/bin/env bash
#
# Upload the built privy wheel to Azure Blob Storage, overwriting any existing
# blob at the same path. Expects STORAGE_KEY in the environment (load from .env
# with `set -a; source .env; set +a` first).
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
VERSION=$(python3 -c "import re; print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open('src/privy/__init__.py').read())[1])")
WHL_NAME="privy-${VERSION}-py3-none-any.whl"

ACCOUNT_NAME="${PRIVY_STORAGE_ACCOUNT:-rakirahman}"
CONTAINER="${PRIVY_STORAGE_CONTAINER:-public}"
BLOB_NAME="${PRIVY_BLOB_NAME:-whls/${WHL_NAME}}"
WHL_PATH="${PRIVY_WHL_PATH:-dist/${WHL_NAME}}"

if [[ -z "${STORAGE_KEY:-}" ]]; then
  echo "STORAGE_KEY is not set. Run:  set -a; source .env; set +a" >&2
  exit 1
fi

if [[ ! -f "$WHL_PATH" ]]; then
  echo "wheel not found at $WHL_PATH — run 'uv build' first" >&2
  exit 1
fi

echo ">> uploading $WHL_PATH → https://${ACCOUNT_NAME}.blob.core.windows.net/${CONTAINER}/${BLOB_NAME}"
az storage blob upload \
  --account-name "$ACCOUNT_NAME" \
  --account-key "$STORAGE_KEY" \
  --container-name "$CONTAINER" \
  --name "$BLOB_NAME" \
  --file "$WHL_PATH" \
  --overwrite \
  --only-show-errors

echo ">> done"

echo ">> uploading $WHL_PATH → https://${ACCOUNT_NAME}.blob.core.windows.net/${CONTAINER}/${BLOB_NAME}"
az storage blob upload \
  --account-name "$ACCOUNT_NAME" \
  --account-key "$STORAGE_KEY" \
  --container-name "$CONTAINER" \
  --name "$BLOB_NAME" \
  --file "$WHL_PATH" \
  --overwrite \
  --only-show-errors

echo ">> done"
