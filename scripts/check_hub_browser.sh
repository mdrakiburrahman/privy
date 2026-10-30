#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv run --locked --with playwright==1.64.1 python -m playwright install chromium
uv run --locked --with playwright==1.64.1 pytest tests/test_hub_remote_exec_browser.py tests/test_hub_purpose_browser.py -q
