#!/usr/bin/env bash
#
# Build the self-contained `privy` binary with PyInstaller, then fold glibc into
# it with staticx so it runs on distros older than this build box.
#
#   ./scripts/build_binary.sh   →  dist/privy
#
# Set PRIVY_SKIP_STATICX=1 to keep the plain (glibc-linked) PyInstaller build.
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

BIN="dist/privy"

echo ">> syncing binary build dependencies"
uv sync --group binary

echo ">> building $BIN"
uv run --group binary pyinstaller --clean --noconfirm privy.spec

if [[ ! -x "$BIN" ]]; then
  echo "build produced no executable at $BIN" >&2
  exit 1
fi

# PyInstaller links against the build box's glibc, so the binary fails on any
# older distro with "GLIBC_2.xx not found". staticx bundles libc and the loader
# into the executable, making it portable. It needs patchelf, which we fetch
# standalone if the system does not have it.
if [[ "${PRIVY_SKIP_STATICX:-0}" != "1" ]]; then
  PATCHELF_VERSION="0.18.0"
  PATCHELF_DIR=".build/patchelf-${PATCHELF_VERSION}"
  if ! command -v patchelf >/dev/null 2>&1; then
    if [[ ! -x "${PATCHELF_DIR}/bin/patchelf" ]]; then
      echo ">> fetching patchelf ${PATCHELF_VERSION}"
      mkdir -p "$PATCHELF_DIR"
      curl -fsSL "https://github.com/NixOS/patchelf/releases/download/${PATCHELF_VERSION}/patchelf-${PATCHELF_VERSION}-x86_64.tar.gz" \
        | tar xz -C "$PATCHELF_DIR"
    fi
    PATH="${PWD}/${PATCHELF_DIR}/bin:${PATH}"
    export PATH
  fi

  echo ">> statifying $BIN (staticx)"
  uv run --group binary staticx "$BIN" "${BIN}.static"
  mv "${BIN}.static" "$BIN"
  chmod +x "$BIN"
fi

echo ">> smoke test"
"./$BIN" --version
"./$BIN" client --help >/dev/null
"./$BIN" server --help >/dev/null
"./$BIN" proxy --help >/dev/null
if file "$BIN" | grep -q "statically linked"; then
  echo "   statically linked — portable across glibc versions"
else
  echo "   WARNING: dynamically linked — only runs on glibc >= this box's" >&2
fi

echo ">> done: $BIN ($(du -h "$BIN" | cut -f1))"
