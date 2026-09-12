#!/usr/bin/env bash
set -euo pipefail

for name in LINUX_GCI_RESULT WINDOWS_GCI_RESULT; do
  result="${!name:-missing}"
  if [[ "$result" != "success" ]]; then
    echo "${name} was ${result}; all platform GCI jobs must succeed." >&2
    exit 1
  fi
done

echo "Linux and Windows GCI succeeded."
