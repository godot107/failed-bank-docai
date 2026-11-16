#!/usr/bin/env bash
# Package and deploy Loan Desk Q&A to the Function App (remote build installs app/requirements.txt), then smoke-test
# the whole path: sign-in → app → agent → SQL.   app/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source config/dev.env; source .env; set +a
: "${ASK_APP_NAME:?run infra/ask_up.sh first}"
BUILD=$(mktemp -d)
cp app/function_app.py app/host.json app/requirements.txt "$BUILD"/
cp -r app/static "$BUILD"/static
cp src/agent.py src/common.py src/guardrails.py src/prescribe.py src/keying.py "$BUILD"/   # agent tools, SQL, guardrails, /manual parsing
(cd "$BUILD" && zip -qr app.zip .)
# Flex Consumption takes config-zip; with basic-auth publishing off the CLI's wait can hang, so cap it and check ourselves
az functionapp deployment source config-zip -g "$RG" -n "$ASK_APP_NAME" --src "$BUILD/app.zip" \
  --build-remote true --timeout 420 -o none || echo "(CLI wait ended; checking the app directly)"
for _ in $(seq 1 30); do   # 401 = up, behind sign-in
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "${ASK_APP_URL}/api/health")" == "401" ]] && break
  sleep 10
done
rm -rf "$BUILD"
TOKEN=$(az account get-access-token --resource "api://${ASK_CLIENT_ID}" --query accessToken -o tsv)
BODY=$(mktemp)
CODE=$(curl -s -m 240 -o "$BODY" -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
       -X POST "${ASK_APP_URL}/api/ask" -d '{"question":"How many loans does each bank have in the loan view?"}')
if [[ "$CODE" == "200" ]]; then echo "smoke test: /api/ask 200"; else
  echo "smoke test failed: /api/ask returned $CODE: $(head -c 300 "$BODY")"; rm -f "$BODY"; exit 1; fi
rm -f "$BODY"
echo "deployed → ${ASK_APP_URL}/desk"
