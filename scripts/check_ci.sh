#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv sync --locked --group dev --extra sql
uv run --locked --extra sql ruff check .
uv run --locked --extra sql ruff format --check .
./scripts/check_markdown.sh
uv run --locked --extra sql pytest -m "not e2e"
