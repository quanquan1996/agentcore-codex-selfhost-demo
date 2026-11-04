#!/usr/bin/env bash
# Ask Codex something. Follow-up questions continue the same conversation;
# add --new to start a new session.
set -euo pipefail
source "$(dirname "$0")/scripts/_env.sh"
exec "$PY" "$REPO_ROOT/scripts/invoke.py" ask "$@"
