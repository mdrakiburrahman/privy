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

[ -n "$PACKAGES" ] && sudo apt-get update -qq && sudo apt-get install -yqq $PACKAGES

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
echo "uv: $(uv --version)"
echo "az: $(az version -o tsv 2>/dev/null | head -1)"