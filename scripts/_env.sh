#!/usr/bin/env bash
# Shared bootstrap: make sure a local venv with a new enough boto3 exists.
# Sourced by deploy.sh / ask.sh / exec.sh / test.sh / cleanup.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "Creating virtualenv (one-time, ~20s) ..."
  python3 -m venv "$VENV"
  "$PY" -m pip install --quiet --upgrade pip
fi

# Capability probe instead of a version check: S3 Files client + Runtime filesystem mounts.
if ! "$PY" - >/dev/null 2>&1 <<'PY'
import boto3, sys
s = boto3.session.Session()
ok = "s3files" in s.get_available_services()
m = s.client("bedrock-agentcore-control", region_name="us-east-1").meta.service_model
ok = ok and "filesystemConfigurations" in m.operation_model("CreateAgentRuntime").input_shape.members
sys.exit(0 if ok else 1)
PY
then
  echo "Installing boto3 with AgentCore + S3 Files support ..."
  "$PY" -m pip install --quiet --upgrade -r "$REPO_ROOT/requirements.txt"
fi

export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
