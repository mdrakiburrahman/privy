#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

MARKDOWN_GLOBS=("**/*.md" ".github/**/*.md")

case "${1:-}" in
  "")
    npx --yes prettier@3.9.6 --check "${MARKDOWN_GLOBS[@]}"
    npx --yes markdownlint-cli2@0.23.2
    ;;
  --fix)
    npx --yes prettier@3.9.6 --write "${MARKDOWN_GLOBS[@]}"
    npx --yes markdownlint-cli2@0.23.2 --fix
    ;;
  *)
    echo "Usage: $0 [--fix]" >&2
    exit 2
    ;;
esac
