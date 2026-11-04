#!/usr/bin/env bash
# Run a shell command in the same microVM as ./ask.sh, without the model.
set -euo pipefail
source "$(dirname "$0")/scripts/_env.sh"
exec "$PY" "$REPO_ROOT/scripts/invoke.py" exec "$@"
