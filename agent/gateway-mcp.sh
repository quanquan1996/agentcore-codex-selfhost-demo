#!/bin/sh
# Codex starts this as a stdio MCP server. Requests are signed with the
# runtime execution role; no token is stored in the container.
exec mcp-proxy-for-aws "$GATEWAY_URL" --service bedrock-agentcore --region "$AWS_REGION"
