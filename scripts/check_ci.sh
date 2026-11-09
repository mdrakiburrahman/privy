#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv sync --locked --group dev
uv run --locked ruff check .
uv run --locked ruff format --check .
./scripts/check_markdown.sh
uv run --locked pytest -m "not e2e"
