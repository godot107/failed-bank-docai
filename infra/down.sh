#!/usr/bin/env bash
# Delete EVERYTHING in the dev resource group (asks first).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck source=/dev/null
source config/dev.env
set +a
az resource list -g "$RG" -o table
read -r -p "Delete resource group $RG and all resources above? Type 'dev' to confirm: " ok
[[ "$ok" == "dev" ]] || { echo "aborted"; exit 1; }
az group delete -n "$RG" --yes
echo "Soft-deleted: Key Vault (7 days) and Cognitive Services accounts. Purge before reusing names:"
echo "  az keyvault list-deleted -o table ; az cognitiveservices account list-deleted -o table"
echo "Verify nothing tagged is left: az resource list --tag project=failed-bank-docai -o table"
