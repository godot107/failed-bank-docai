#!/usr/bin/env bash
# Create/refresh the dev resource group + infra/main.bicep, then write endpoints to .env (gitignored).
#   infra/up.sh              deploy
#   infra/up.sh --what-if    show what would change, deploy nothing
# Person- and identity-specific values come from the shell, never from git:
#   ALERT_EMAIL         budget + alert email (required)
#   OWNER_TAG           value of the 'owner' tag (default: owner)
#   OPERATOR_OBJECT_ID  Entra object ID given data-plane roles + SQL admin (default: signed-in az user)
#   OPERATOR_LOGIN      login shown as SQL admin (default: signed-in az user)
#   CLIENT_IPS          JSON array of IPs allowed through the SQL firewall (default: this machine's public IP)
#   DI_SKU              F0 (default, free) or S0
#   DEPLOY_AI           true to add the Foundry model for text flags (default false)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck source=/dev/null
source config/dev.env
set +a
: "${ALERT_EMAIL:?set ALERT_EMAIL (budget/alert email) in your shell}"
OWNER_TAG=${OWNER_TAG:-owner}
TAGS=(project=failed-bank-docai environment="$FBD_ENV" owner="$OWNER_TAG" cost-center=personal managed-by=bicep)

az account show --query "{sub:name,quota:subscriptionPolicies.quotaId,spendingLimit:subscriptionPolicies.spendingLimit}" -o table
export OPERATOR_OBJECT_ID=${OPERATOR_OBJECT_ID:-$(az ad signed-in-user show --query id -o tsv)}
export OPERATOR_LOGIN=${OPERATOR_LOGIN:-$(az account show --query user.name -o tsv)}
export CLIENT_IPS=${CLIENT_IPS:-"[\"$(curl -s https://api.ipify.org)\"]"}
export ALERT_EMAIL OWNER_TAG

if [[ "${1:-}" == "--what-if" ]]; then
  if [ "$(az group exists -n "$RG")" = "false" ]; then
    echo "resource group $RG does not exist: a deploy would create it and every resource in main.bicep"
    az bicep build -f infra/main.bicep --stdout > /dev/null && echo "main.bicep compiles"
    exit 0
  fi
  az deployment group what-if -g "$RG" -n main -p infra/params/dev.bicepparam
  exit 0
fi

az group create -n "$RG" -l "$LOCATION" --tags "${TAGS[@]}" -o none
az deployment group create -g "$RG" -n main -p infra/params/dev.bicepparam -o none
infra/env.sh
