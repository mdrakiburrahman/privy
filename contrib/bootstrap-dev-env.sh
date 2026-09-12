#!/bin/bash
#
#
#       Sets up a dev env with all pre-reqs. 
#
#       This script is idempotent, it will only attempt to install 
#       dependencies if not exists.
#
# ---------------------------------------------------------------------------------------
#

export REPO_ROOT=$(git rev-parse --show-toplevel)
export DEBIAN_FRONTEND=noninteractive
export PATH=$(echo "$PATH" | tr ':' '\n' | grep -v "/mnt/c" | tr '\n' ':' | sed 's/:$//')

PACKAGES=""
command -v python &>/dev/null || PACKAGES="python3 python-is-python3 python3-venv"
command -v pip &>/dev/null || PACKAGES="$PACKAGES python3-pip"
command -v curl &>/dev/null || PACKAGES="$PACKAGES curl"
command -v gh &>/dev/null || PACKAGES="$PACKAGES gh"
command -v jq &>/dev/null || PACKAGES="$PACKAGES jq"
command -v file &>/dev/null || PACKAGES="$PACKAGES file"

[ -n "$PACKAGES" ] && sudo apt-get update -qq && sudo apt-get install -yqq $PACKAGES

NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)
[[ "$NODE_MAJOR" =~ ^[0-9]+$ ]] || NODE_MAJOR=0
if (( NODE_MAJOR < 22 )) || ! command -v npm &>/dev/null || ! command -v npx &>/dev/null; then
  echo "Node.js 22+ with npm/npx not found, installing Node.js 24..."
  if ! (set -o pipefail; curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -); then
    echo "Failed to configure the NodeSource Node.js 24 repository." >&2
    exit 1
  fi
  sudo apt-get install -yqq nodejs
else
  echo "Node.js already installed at: $(command -v node)"
fi

NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)
[[ "$NODE_MAJOR" =~ ^[0-9]+$ ]] || NODE_MAJOR=0
if (( NODE_MAJOR < 22 )) || ! command -v npm &>/dev/null || ! command -v npx &>/dev/null; then
  echo "Node.js 22+, npm, and npx are required but were not installed successfully." >&2
  exit 1
fi

command -v uv &>/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; source "$HOME/.local/bin/env" 2>/dev/null || true; }

AZ_PATH=$(which az 2>/dev/null)
if [[ -z "$AZ_PATH" || "$AZ_PATH" == *"/mnt/c"* ]]; then
  echo "Native Linux Azure CLI not found, installing..."
  curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
  export PATH="$HOME/bin:$PATH"
  [[ -f "$HOME/.bashrc" ]] && source "$HOME/.bashrc"
else
  echo "Native Linux Azure CLI already installed at: $AZ_PATH"
fi
az account get-access-token --query "expiresOn" -o tsv >/dev/null 2>&1
if [[ $? -ne 0 ]]; then
    echo "az is not logged in, logging in..."
    az login >/dev/null
fi

[[ ":$PATH:" != *":$HOME/.local/bin:"* ]] && export PATH="$HOME/.local/bin:$PATH"
code --install-extension donjayamanne.python-extension-pack

echo "Done"
echo "Python: $(python --version)"
echo "Node.js: $(node --version)"
echo "npm: $(npm --version)"
echo "uv: $(uv --version)"
echo "az: $(az version -o tsv 2>/dev/null | head -1)"
