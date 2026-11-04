FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023
RUN dnf install -y git python3.12 python3.12-pip nodejs22 nodejs22-npm shadow-utils && dnf clean all
ARG CODEX_VERSION=0.166.0
RUN npm install -g @openai/codex@${CODEX_VERSION}
# SigV4 bridge from Codex (stdio MCP) to AgentCore Gateway (IAM auth)
RUN python3.12 -m pip install --no-cache-dir mcp-proxy-for-aws==1.8.0
RUN useradd -m -u 1000 agent
COPY --chown=agent:agent config.toml AGENTS.md /home/agent/.codex/
COPY server.py gateway-mcp.sh /app/
USER agent
ENV CODEX_HOME=/home/agent/.codex WORKSPACE=/mnt/workspace ARCHIVE=/mnt/archive
EXPOSE 8080
CMD ["python3.12", "/app/server.py"]
