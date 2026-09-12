#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv sync --locked --group binary
uv build
./scripts/build_binary.sh
./scripts/verify_build_artifacts.sh
