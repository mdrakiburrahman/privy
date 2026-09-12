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

if [[ "${GCI_EVENT_NAME:-local}" == "pull_request" ]]; then
  require_environment_values GCI_REPOSITORY GCI_HEAD_REPOSITORY
  if [[ "$GCI_HEAD_REPOSITORY" != "$GCI_REPOSITORY" ]]; then
    environment_error \
      "GCI uses repository secrets and only runs for branches in $GCI_REPOSITORY; fork pull requests are not eligible."
    exit 1
  fi
fi

if [[ "${1:-}" != "--check" ]]; then
  uv sync --locked --group dev
fi

load_project_environment "GCI"
RELAY_VARIABLES=(
  PRIVY_RELAY_NAMESPACE
  PRIVY_RELAY_PATH
  PRIVY_RELAY_KEYRULE
  PRIVY_RELAY_KEY
)
require_environment_values "${RELAY_VARIABLES[@]}"
mask_environment_values "${RELAY_VARIABLES[@]}"
unset STORAGE_KEY

if [[ "${1:-}" == "--check" ]]; then
  echo "GCI environment is valid."
  exit 0
fi

PRIVY_REQUIRE_E2E=1 uv run --locked pytest -m e2e
