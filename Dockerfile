# Executor container: runs `orchestrator serve` on kenta-server so Claude Code, Codex, uv, git and gh
# have a current userland independent of the host, while credentials live in host-mounted files
# (see docker-compose.executor.yml). UID/GID match the host user that owns the mounted credential
# dirs so bind mounts stay writable inside the container.
FROM ubuntu:24.04

ARG UID=1000
ARG GID=1000
# Bump these together with the comment above each install step when a new release ships.
ARG UV_VERSION=0.12.16
ARG CLAUDE_VERSION=2.1.276
ARG CODEX_VERSION=0.155.0

ENV HOME=/home/orch \
    DEBIAN_FRONTEND=noninteractive \
    PATH=/home/orch/.local/bin:/usr/local/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git gnupg python3 \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Reuse an existing group/user at that id (renaming it to orch) instead of failing outright -- the host
# uid/gid picked up via `id -u`/`id -g` can collide with a base-image system account.
RUN set -eux; \
    existing_group="$(getent group "$GID" | cut -d: -f1)"; \
    if [ -n "$existing_group" ]; then groupmod -n orch "$existing_group"; else groupadd -g "$GID" orch; fi; \
    existing_user="$(getent passwd "$UID" | cut -d: -f1)"; \
    if [ -n "$existing_user" ]; then usermod -l orch -d /home/orch -m -g orch "$existing_user"; \
    else useradd -m -u "$UID" -g orch -d /home/orch -s /bin/bash orch; fi

# uv: official installer, pinned via the version segment in the URL (astral-sh/uv supports this).
# Bump: change UV_VERSION above to a tag from https://github.com/astral-sh/uv/releases.
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | env UV_INSTALL_DIR=/usr/local/bin sh

# Codex CLI: static musl binary from the openai/codex GitHub release (no linux-gnu asset is published).
# Bump: change CODEX_VERSION above to a version from https://github.com/openai/codex/releases (tag is
# "rust-v<version>"; the download URL below adds that prefix).
RUN curl -fsSL -o /tmp/codex.tar.gz \
      "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz" \
    && tar -xzf /tmp/codex.tar.gz -C /tmp \
    && install -m 0755 /tmp/codex-x86_64-unknown-linux-musl /usr/local/bin/codex \
    && rm -rf /tmp/codex*

COPY --chown=orch:orch . /opt/orchestrator
WORKDIR /opt/orchestrator

USER orch

# Claude Code: native installer (not npm), documented at https://code.claude.com/docs/en/setup. The
# installer accepts a version argument (`bash -s <version>`) — pinned here via CLAUDE_VERSION.
# Bump: change CLAUDE_VERSION above to a version from `claude --version` upstream or the release notes.
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_VERSION}" \
    && claude --version

RUN uv sync --frozen

# No args (the compose service) -> serve. Args given (e.g. the version-check smoke test in this task's
# acceptance criteria) -> run them instead, so `docker run --rm <image> sh -c '...'` works directly.
ENTRYPOINT ["/bin/sh", "-c", "if [ \"$#\" -gt 0 ]; then exec \"$@\"; else exec uv run orchestrator serve --host 0.0.0.0 --port 8090; fi", "--"]
