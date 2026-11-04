#!/usr/bin/env bash
# Delete every resource the demo created (about 10 minutes: the VPC waits for
# network interfaces to be released).
set -euo pipefail
source "$(dirname "$0")/scripts/_env.sh"
if [ ! -f "$REPO_ROOT/.state.json" ]; then
  echo "Nothing to clean up (.state.json not found)."
  exit 0
fi
echo "This deletes all codex-demo-* resources listed in .state.json (region $(python3 -c "import json;print(json.load(open('$REPO_ROOT/.state.json')).get('region'))"))."
if [ "${1:-}" != "--yes" ]; then
  read -r -p "Type 'delete' to confirm: " CONFIRM
  [ "$CONFIRM" = "delete" ] || { echo "Aborted."; exit 1; }
fi
"$PY" "$REPO_ROOT/scripts/cleanup.py"
rm -f "$REPO_ROOT/.session.json"
