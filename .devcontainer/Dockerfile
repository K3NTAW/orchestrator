FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep jq python3 ca-certificates curl && rm -rf /var/lib/apt/lists/* \
 && npm i -g @openai/codex
RUN useradd -m exec
USER exec
WORKDIR /workspace
# The task worktree is mounted at /workspace; no host credentials are mounted.
# Network allowlist (package registries + git host) is enforced by the container runtime/firewall, not here.
