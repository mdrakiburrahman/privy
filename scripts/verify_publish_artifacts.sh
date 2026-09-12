#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

./scripts/verify_build_artifacts.sh

WINDOWS_BINARY="${PRIVY_WINDOWS_BIN_PATH:-dist/privy.exe}"
if [[ ! -s "$WINDOWS_BINARY" ]]; then
  echo "Windows CLI is missing or empty: $WINDOWS_BINARY" >&2
  exit 1
fi

WINDOWS_BINARY_INFO="$(file "$WINDOWS_BINARY")"
if [[ "$WINDOWS_BINARY_INFO" != *"PE32+"* || "$WINDOWS_BINARY_INFO" != *"x86-64"* ]]; then
  echo "Windows CLI is not an x86_64 PE executable: $WINDOWS_BINARY_INFO" >&2
  exit 1
fi

echo "Verified Windows CLI: $WINDOWS_BINARY"
