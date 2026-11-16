#!/usr/bin/env bash
# Entra app registration for Loan Desk Q&A sign-in (App Service Authentication). Single-tenant, ID-token sign-in,
# no client secret. Exposes api://<appId> with a user_impersonation scope and pre-authorizes the Azure CLI, so
# `az account get-access-token --resource api://<appId>` works for smoke tests and the eval without a browser.
# Tenant-level object: NOT in the resource group, so infra/down.sh doesn't remove it; run `infra/ask_auth.sh delete`.
#   infra/ask_auth.sh          create or update; writes ASK_CLIENT_ID to .env
#   infra/ask_auth.sh delete   remove it
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source config/dev.env; source .env; set +a
NAME=fbd-loan-desk-ask
SUFFIX=${LAKE_ACCOUNT#stfbd}
HOST="func-fbd-${SUFFIX}.azurewebsites.net"
AZ_CLI=04b07795-8ddb-461a-bbee-02f9e1bf7b46   # Microsoft Azure CLI (first-party public client)

APP_ID=$(az ad app list --display-name "$NAME" --query "[0].appId" -o tsv)
if [[ "${1:-}" == "delete" ]]; then
  [[ -n "$APP_ID" ]] && az ad app delete --id "$APP_ID" && echo "deleted $NAME ($APP_ID)"
  sed -i '/^ASK_CLIENT_ID=/d' .env
  exit 0
fi
if [[ -z "$APP_ID" ]]; then
  APP_ID=$(az ad app create --display-name "$NAME" --sign-in-audience AzureADMyOrg \
           --web-redirect-uris "https://${HOST}/.auth/login/aad/callback" --enable-id-token-issuance true \
           --query appId -o tsv)
  echo "created $NAME: $APP_ID"
fi
OBJ=$(az ad app show --id "$APP_ID" --query id -o tsv)
SCOPE_ID=$(az ad app show --id "$APP_ID" --query "api.oauth2PermissionScopes[0].id" -o tsv)
SCOPE_ID=${SCOPE_ID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}
BODY=$(python3 - "$APP_ID" "$SCOPE_ID" "$AZ_CLI" <<'PY'
import json, sys
app, scope, cli = sys.argv[1:]
print(json.dumps({"identifierUris": [f"api://{app}"], "tags": ["project=failed-bank-docai", "environment=dev", "managed-by=script"],
  "api": {"requestedAccessTokenVersion": 2,
          "oauth2PermissionScopes": [{"id": scope, "value": "user_impersonation", "type": "User", "isEnabled": True,
             "adminConsentDisplayName": "Use Loan Desk Q&A", "adminConsentDescription": "Ask Loan Desk Q&A questions",
             "userConsentDisplayName": "Use Loan Desk Q&A", "userConsentDescription": "Ask Loan Desk Q&A questions"}],
          "preAuthorizedApplications": [{"appId": cli, "delegatedPermissionIds": [scope]}]}}))
PY
)
# Graph needs the scope to exist before an app can be pre-authorized for it: two PATCHes
FIRST=$(python3 -c 'import json,sys; b=json.loads(sys.argv[1]); b["api"]["preAuthorizedApplications"]=[]; print(json.dumps(b))' "$BODY")
az rest --method patch --url "https://graph.microsoft.com/v1.0/applications/${OBJ}" --body "$FIRST" -o none
az rest --method patch --url "https://graph.microsoft.com/v1.0/applications/${OBJ}" --body "$BODY" -o none
az ad app update --id "$APP_ID" --web-redirect-uris "https://${HOST}/.auth/login/aad/callback"
az ad sp show --id "$APP_ID" -o none 2>/dev/null || az ad sp create --id "$APP_ID" -o none
sed -i '/^ASK_CLIENT_ID=/d' .env && echo "ASK_CLIENT_ID=${APP_ID}" >> .env
echo "ASK_CLIENT_ID=${APP_ID} → .env (redirect https://${HOST}/.auth/login/aad/callback)"
