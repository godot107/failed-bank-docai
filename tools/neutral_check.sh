#!/usr/bin/env bash
# Pre-commit / pre-publish check: tracked files must not contain any term in private/neutral_terms.txt
# (one regex per line, kept out of git). Exit 1 and list hits if any are found.
set -euo pipefail
cd "$(dirname "$0")/.."
terms=private/neutral_terms.txt
[ -s "$terms" ] || { echo "missing $terms (one term per line)"; exit 2; }
if git ls-files -z | xargs -0 grep -nIiE -f "$terms" --; then echo "neutrality check FAILED"; exit 1; fi
echo "neutrality check OK"
