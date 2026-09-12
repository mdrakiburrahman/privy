#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
source scripts/lib/environment.sh
trap cleanup_project_environment EXIT

case "${1:-}" in
  "" | --check) ;;
  *)
    echo "Usage: $0 [--check]" >&2
    exit 2
    ;;
esac

load_project_environment "Publishing"

export PRIVY_STORAGE_ACCOUNT="${PRIVY_STORAGE_ACCOUNT:-rakirahman}"
export PRIVY_STORAGE_CONTAINER="${PRIVY_STORAGE_CONTAINER:-public}"
require_environment_values STORAGE_KEY PRIVY_STORAGE_ACCOUNT PRIVY_STORAGE_CONTAINER
validate_optional_environment_values \
  PRIVY_BLOB_NAME \
  PRIVY_WHL_PATH \
  PRIVY_BIN_PATH \
  PRIVY_BIN_BLOB_PREFIX \
  PRIVY_WINDOWS_BIN_PATH
mask_environment_values STORAGE_KEY

LINUX_BINARY="${PRIVY_BIN_PATH:-dist/privy}"
if [[ -s "$LINUX_BINARY" && ! -x "$LINUX_BINARY" ]]; then
  chmod +x "$LINUX_BINARY"
fi
./scripts/verify_publish_artifacts.sh

unset \
  PRIVY_RELAY_TOKEN \
  PRIVY_RELAY_KEY \
  PRIVY_RELAY_SEND_KEY \
  PRIVY_RELAY_LISTEN_KEY

if [[ "${1:-}" == "--check" ]]; then
  echo "Publishing environment and artifacts are valid."
  exit 0
fi

if ! command -v az >/dev/null; then
  echo "Azure CLI is required to publish artifacts." >&2
  exit 1
fi

./scripts/upload_whl.sh
