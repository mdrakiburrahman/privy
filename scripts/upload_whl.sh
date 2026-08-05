#!/usr/bin/env bash
#
# Upload the built privy wheel and the self-contained binary to Azure Blob
# Storage, overwriting any existing blobs at the same paths. Expects STORAGE_KEY
# in the environment (load from .env with `set -a; source .env; set +a` first).
#
# Build both artifacts first:
#   uv build                    → dist/privy-<version>-py3-none-any.whl
#   ./scripts/build_binary.sh   → dist/privy
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
VERSION=$(python3 -c "import re; print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open('src/privy/__init__.py').read())[1])")
WHL_NAME="privy-${VERSION}-py3-none-any.whl"

ACCOUNT_NAME="${PRIVY_STORAGE_ACCOUNT:-rakirahman}"
CONTAINER="${PRIVY_STORAGE_CONTAINER:-public}"
BLOB_NAME="${PRIVY_BLOB_NAME:-whls/${WHL_NAME}}"
WHL_PATH="${PRIVY_WHL_PATH:-dist/${WHL_NAME}}"

BIN_PATH="${PRIVY_BIN_PATH:-dist/privy}"
BIN_PREFIX="${PRIVY_BIN_BLOB_PREFIX:-bins}"
BIN_VERSIONED="${BIN_PREFIX}/privy-${VERSION}-linux-x86_64"
BIN_LATEST="${BIN_PREFIX}/privy-linux-x86_64"

if [[ -z "${STORAGE_KEY:-}" ]]; then
  echo "STORAGE_KEY is not set. Run:  set -a; source .env; set +a" >&2
  exit 1
fi

if [[ ! -f "$WHL_PATH" ]]; then
  echo "wheel not found at $WHL_PATH — run 'uv build' first" >&2
  exit 1
fi

if [[ ! -f "$BIN_PATH" ]]; then
  echo "binary not found at $BIN_PATH — run './scripts/build_binary.sh' first" >&2
  exit 1
fi

upload() {
  local file="$1" blob="$2"
  echo ">> uploading $file → https://${ACCOUNT_NAME}.blob.core.windows.net/${CONTAINER}/${blob}"
  az storage blob upload \
    --account-name "$ACCOUNT_NAME" \
    --account-key "$STORAGE_KEY" \
    --container-name "$CONTAINER" \
    --name "$blob" \
    --file "$file" \
    --overwrite \
    --only-show-errors
}

upload "$WHL_PATH" "$BLOB_NAME"
upload "$BIN_PATH" "$BIN_VERSIONED"
upload "$BIN_PATH" "$BIN_LATEST"

echo ">> done"
