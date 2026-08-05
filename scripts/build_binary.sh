#!/usr/bin/env bash
#
# Build the self-contained `privy` binary with PyInstaller.
#
#   ./scripts/build_binary.sh   →  dist/privy
#
# The result is a single Linux executable that needs no Python on the target
# box. It is glibc-linked, so build on the oldest distro you intend to run on.
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo ">> syncing binary build dependencies"
uv sync --group binary

echo ">> building dist/privy"
uv run --group binary pyinstaller --clean --noconfirm privy.spec

BIN="dist/privy"
if [[ ! -x "$BIN" ]]; then
  echo "build produced no executable at $BIN" >&2
  exit 1
fi

echo ">> smoke test"
"./$BIN" --version
"./$BIN" client --help >/dev/null
"./$BIN" server --help >/dev/null
"./$BIN" proxy --help >/dev/null

echo ">> done: $BIN ($(du -h "$BIN" | cut -f1))"
