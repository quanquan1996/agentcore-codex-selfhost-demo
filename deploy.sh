#!/usr/bin/env bash
# Create (or update) every resource of the demo. Safe to re-run.
set -euo pipefail
source "$(dirname "$0")/scripts/_env.sh"
exec "$PY" "$REPO_ROOT/scripts/deploy.py" "$@"
