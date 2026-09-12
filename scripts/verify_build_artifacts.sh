#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
source scripts/lib/release.sh

VERSION="$(privy_version)"
WHEEL="dist/privy-${VERSION}-py3-none-any.whl"
BINARY="dist/privy"

if [[ ! -s "$WHEEL" ]]; then
  echo "Expected wheel is missing or empty: $WHEEL" >&2
  exit 1
fi
if [[ ! -x "$BINARY" || ! -s "$BINARY" ]]; then
  echo "Static CLI is missing, empty, or not executable: $BINARY" >&2
  exit 1
fi

BINARY_INFO="$(file "$BINARY")"
if [[ "$BINARY_INFO" != *"ELF 64-bit"* || "$BINARY_INFO" != *"x86-64"* ]]; then
  echo "CLI is not a Linux x86_64 ELF binary: $BINARY_INFO" >&2
  exit 1
fi
if [[ "$BINARY_INFO" != *"statically linked"* ]]; then
  echo "CLI is not statically linked: $BINARY_INFO" >&2
  exit 1
fi

echo "Verified $WHEEL and $BINARY"
