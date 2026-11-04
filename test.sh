#!/usr/bin/env bash
# End-to-end check: five steps, PASS/FAIL each. Takes about 3 minutes.
set -euo pipefail
source "$(dirname "$0")/scripts/_env.sh"
exec "$PY" "$REPO_ROOT/scripts/e2e_test.py" "$@"
