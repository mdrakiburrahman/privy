#!/usr/bin/env bash

PRIVY_ENV_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PRIVY_TEMP_ENV_FILE=""

environment_error() {
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    printf '::error::%s\n' "$*" >&2
  else
    printf '%s\n' "$*" >&2
  fi
}

cleanup_project_environment() {
  if [[ -n "$PRIVY_TEMP_ENV_FILE" ]]; then
    rm -f -- "$PRIVY_TEMP_ENV_FILE"
    PRIVY_TEMP_ENV_FILE=""
  fi
}

load_project_environment() {
  local context="$1"
  local env_file="$PRIVY_ENV_REPO_ROOT/.env"

  if [[ -n "${BASE64_ENV:-}" ]]; then
    local temp_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
    if [[ ! -d "$temp_root" ]]; then
      environment_error "Temporary directory does not exist: $temp_root"
      return 1
    fi

    umask 077
    PRIVY_TEMP_ENV_FILE="$(mktemp "${temp_root%/}/privy-env.XXXXXX")"
    if ! printf '%s' "$BASE64_ENV" | base64 --decode >"$PRIVY_TEMP_ENV_FILE"; then
      cleanup_project_environment
      environment_error "$context BASE64_ENV is not valid base64."
      return 1
    fi
    env_file="$PRIVY_TEMP_ENV_FILE"
    unset BASE64_ENV
  elif [[ ! -f "$env_file" ]]; then
    environment_error "$context requires BASE64_ENV or $env_file."
    return 1
  fi

  set +x
  set +u
  set -a
  if ! source "$env_file" >/dev/null 2>&1; then
    set +a
    set -u
    cleanup_project_environment
    environment_error "$context environment could not be loaded."
    return 1
  fi
  set +a
  set -u
}

require_environment_values() {
  local name value
  for name in "$@"; do
    if [[ ! -v "$name" || -z "${!name}" ]]; then
      environment_error "$name is missing or empty."
      return 1
    fi
    value="${!name}"
    if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
      environment_error "$name must be a single-line value."
      return 1
    fi
  done
}

validate_optional_environment_values() {
  local name value
  for name in "$@"; do
    if [[ -v "$name" && -n "${!name}" ]]; then
      value="${!name}"
      if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
        environment_error "$name must be a single-line value."
        return 1
      fi
    fi
  done
}

mask_environment_values() {
  [[ "${GITHUB_ACTIONS:-}" == "true" ]] || return 0

  local name
  for name in "$@"; do
    printf '::add-mask::%s\n' "${!name}"
  done
}
