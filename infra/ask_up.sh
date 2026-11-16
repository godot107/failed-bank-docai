#!/usr/bin/env bash
# Deploy Loan Desk Q&A (infra/ask.bicep) on top of main.bicep, then write its outputs to .env.
#   infra/ask_up.sh             deploy (run infra/ask_auth.sh first)
#   infra/ask_up.sh --what-if   preview only
# After the first deploy: src/agent.py create (saves AGENT_ID), infra/ask_up.sh again (passes it to the app),
# tools/run_sql.py sql/006_ask_access.sql (the app's SQL user), app/deploy.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source config/dev.env; source .env; set +a
: "${ASK_CLIENT_ID:?run infra/ask_auth.sh first}"
FOUNDRY=$(az cognitiveservices account list -g "$RG" --query "[?kind=='AIServices'].name | [0]" -o tsv)
LOGWS=$(az monitor log-analytics workspace list -g "$RG" --query "[0].name" -o tsv)
# Foundry projects need allowProjectManagement on the account (also set in main.bicep, so up.sh keeps it)
[[ "$(az resource show -g "$RG" -n "$FOUNDRY" --resource-type Microsoft.CognitiveServices/accounts \
      --query properties.allowProjectManagement -o tsv)" == "true" ]] || \
  az resource update -g "$RG" -n "$FOUNDRY" --resource-type Microsoft.CognitiveServices/accounts \
      --set properties.allowProjectManagement=true -o none
PARAMS=(foundryName="$FOUNDRY" logWorkspaceName="$LOGWS" sqlServerFqdn="$SQL_SERVER" sqlDatabase="$SQL_DATABASE"
        appClientId="$ASK_CLIENT_ID" agentId="${AGENT_ID:-}" ownerTag="${OWNER_TAG:-owner}"
        operatorObjectId="${OPERATOR_OBJECT_ID:-$(az ad signed-in-user show --query id -o tsv)}")
if [[ "${1:-}" == "--what-if" ]]; then
  az deployment group what-if -g "$RG" -n ask -f infra/ask.bicep -p "${PARAMS[@]}"
  exit 0
fi
az deployment group create -g "$RG" -n ask -f infra/ask.bicep -p "${PARAMS[@]}" -o none
OUT=$(az deployment group show -g "$RG" -n ask --query properties.outputs -o json)
python3 - "$OUT" <<'PY'
import json, re, sys
o = json.loads(sys.argv[1])
snake = lambda k: re.sub(r"(?<!^)(?=[A-Z])", "_", k).upper()
new = {f"ASK_{snake(k)}" if not k.startswith("foundry") else snake(k): v["value"] for k, v in o.items()}
lines = [l for l in open(".env").read().splitlines() if l.split("=", 1)[0] not in new]
open(".env", "w").write("\n".join(lines + [f"{k}={v}" for k, v in new.items()]) + "\n")
print("\n".join(f"{k}={v}" for k, v in new.items()))
PY
echo "→ .env"
