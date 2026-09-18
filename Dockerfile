# Executor container: runs `orchestrator serve` on kenta-server so Claude Code, Codex, uv, git and gh
# have a current userland independent of the host, while credentials live in host-mounted files
# (see docker-compose.executor.yml). UID/GID match the host user that owns the mounted credential
# dirs so bind mounts stay writable inside the container.
#
# Integrity: the codex tarball is verified against a pinned sha256 (CODEX_SHA256_AMD64/_ARM64) -- the
# release's own codex-package_SHA256SUMS only covers the "-package-" bundle, not the plain arch tarball
# used here, so there's no published checksum file to check against. The uv and Claude Code installer
# scripts are downloaded to a file and sha256sum'd into the build log for audit before running -- neither
# vendor publishes a checksum file for the script itself to verify against.
# No --platform pin: this digest is the multi-arch manifest list, so Docker picks the matching entry for
# whatever host builds it (amd64 on kenta-server, arm64 native on the laptop for smoke tests -- never
# emulate). The codex asset below is selected per-arch separately, since it has no multi-arch manifest.
# Bump: `docker manifest inspect ubuntu:24.04` and take the Docker-Content-Digest of the index itself
# (the top-level response), not one of the per-platform manifests it lists.
FROM ubuntu:24.04@sha256:b3cc40b72b93588182b5410f723c7aaf142363311c2aa993d8a453ddcbb3ae15

ARG UID=1000
ARG GID=1000
# Bump these together with the comment above each install step when a new release ships.
ARG UV_VERSION=0.12.16
ARG CLAUDE_VERSION=2.1.276
ARG CODEX_VERSION=0.155.0
# sha256 of codex-x86_64-unknown-linux-musl.tar.gz / codex-aarch64-unknown-linux-musl.tar.gz for
# CODEX_VERSION above. Bump alongside CODEX_VERSION: download the new release's matching tarball(s)
# and `sha256sum` them.
ARG CODEX_SHA256_AMD64=e415cc3adb94ade16e8d44b4dd58a9201cc34b2ee51a5d6eddf2a3a00aecb6c0
ARG CODEX_SHA256_ARM64=8b4a9c356916c515f7c93f918a01b8fa1371bcc9758addbfa723b85fbec5694b

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
# Downloaded to a file first and sha256sum'd into the build log for an audit trail -- astral-sh doesn't
# publish a separate checksum for the installer script itself.
# Bump: change UV_VERSION above to a tag from https://github.com/astral-sh/uv/releases.
RUN curl -LsSf -o /tmp/uv-install.sh "https://astral.sh/uv/${UV_VERSION}/install.sh" \
    && sha256sum /tmp/uv-install.sh \
    && env UV_INSTALL_DIR=/usr/local/bin sh /tmp/uv-install.sh \
    && rm -f /tmp/uv-install.sh

# Codex CLI: static musl binary from the openai/codex GitHub release (no linux-gnu asset is published).
# Asset picked by build architecture, like the gh apt line above; the tarball is verified against the
# pinned CODEX_SHA256_* checksum for that architecture before it's installed.
# Bump: change CODEX_VERSION above to a version from https://github.com/openai/codex/releases (tag is
# "rust-v<version>"; the download URL below adds that prefix), and CODEX_SHA256_AMD64/_ARM64 (see above).
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) codex_arch=x86_64-unknown-linux-musl; codex_sha256="$CODEX_SHA256_AMD64" ;; \
      arm64) codex_arch=aarch64-unknown-linux-musl; codex_sha256="$CODEX_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/codex.tar.gz \
      "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${codex_arch}.tar.gz"; \
    echo "${codex_sha256}  /tmp/codex.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/codex.tar.gz -C /tmp; \
    install -m 0755 "/tmp/codex-${codex_arch}" /usr/local/bin/codex; \
    rm -rf /tmp/codex*

COPY --chown=orch:orch . /opt/orchestrator
WORKDIR /opt/orchestrator

USER orch

# Claude Code: native installer (not npm), documented at https://code.claude.com/docs/en/setup. The
# installer accepts a version argument (`bash -s <version>`) — pinned here via CLAUDE_VERSION.
# Downloaded to a file first and sha256sum'd into the build log for an audit trail -- Anthropic doesn't
# publish a separate checksum for the installer script itself.
# Bump: change CLAUDE_VERSION above to a version from `claude --version` upstream or the release notes.
RUN curl -fsSL -o /tmp/claude-install.sh https://claude.ai/install.sh \
    && sha256sum /tmp/claude-install.sh \
    && bash /tmp/claude-install.sh "${CLAUDE_VERSION}" \
    && rm -f /tmp/claude-install.sh \
    && claude --version

RUN uv sync --frozen

# No args (the compose service) -> serve. Args given (e.g. the version-check smoke test in this task's
# acceptance criteria) -> run them instead, so `docker run --rm <image> sh -c '...'` works directly.
ENTRYPOINT ["/bin/sh", "-c", "if [ \"$#\" -gt 0 ]; then exec \"$@\"; else exec uv run orchestrator serve --host 0.0.0.0 --port 8090; fi", "--"]
