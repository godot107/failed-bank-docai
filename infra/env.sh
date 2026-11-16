#!/usr/bin/env bash
# Write .env (gitignored) from the deployment outputs, without deploying anything.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck source=/dev/null
source config/dev.env
set +a
OUT=$(mktemp)
az deployment group show -g "$RG" -n main --query properties.outputs -o json > "$OUT"
python3 - "$OUT" <<'PY'
import json, re, sys
o = json.load(open(sys.argv[1]))
snake = lambda k: re.sub(r"(?<!^)(?=[A-Z])", "_", k).upper()
lines = [f"{snake(k)}={v['value']}\n" for k, v in o.items()]
# keep keys the deployment doesn't output from the previous .env
import os
if os.path.exists(".env"):
    have = {l.split("=", 1)[0] for l in lines}
    lines += [l if l.endswith("\n") else l + "\n" for l in open(".env") if "=" in l and l.split("=", 1)[0] not in have]
open(".env", "w").writelines(lines)
print("".join(lines), end="")
PY
rm -f "$OUT"
echo "→ .env"
