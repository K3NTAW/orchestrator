# Executor container: runs `orchestrator serve` on kenta-server so Claude Code, Codex, uv, git and gh
# have a current userland independent of the host, while credentials live in host-mounted files
# (see docker-compose.executor.yml). UID/GID match the host user that owns the mounted credential
# dirs so bind mounts stay writable inside the container.
#
# Integrity: the codex and gh tarballs are each verified against a pinned sha256 (CODEX_SHA256_*,
# GH_SHA256_*) -- the codex release's own codex-package_SHA256SUMS only covers the "-package-" bundle,
# not the plain arch tarball used here, so there's no published checksum file to check against; gh's
# release does publish one (gh_<version>_checksums.txt) and the pinned values above come from it. The uv
# and Claude Code installer scripts are downloaded to a file and sha256sum'd into the build log for audit
# before running -- neither vendor publishes a checksum file for the script itself to verify against.
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
ARG GH_VERSION=2.101.0
# sha256 of gh_${GH_VERSION}_linux_amd64.tar.gz / gh_${GH_VERSION}_linux_arm64.tar.gz, from that
# release's own gh_${GH_VERSION}_checksums.txt on https://github.com/cli/cli/releases. Bump alongside
# GH_VERSION.
ARG GH_SHA256_AMD64=9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8
ARG GH_SHA256_ARM64=b57e8063f18862647c9d22727c32e9da1b963f8bf9db648fe123a6975695640f
# Optional pins for the uv/Claude Code installer *scripts* themselves (astral-sh/Anthropic don't publish
# checksums for these, unlike the codex tarball above). When set, the build verifies the downloaded
# script against it before running; when empty, the build only logs the hash for manual audit.
ARG UV_INSTALL_SHA256=""
ARG CLAUDE_INSTALL_SHA256=""

ENV HOME=/home/orch \
    DEBIAN_FRONTEND=noninteractive \
    PATH=/home/orch/.local/bin:/usr/local/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git gnupg python3 \
    && rm -rf /var/lib/apt/lists/*

# gh: static binary from the cli/cli GitHub release, verified against the pinned CODEX-style
# GH_SHA256_* checksum above -- not the cli.github.com apt repo, which floats on whatever build it
# currently serves with no per-artifact hash to pin against. Asset picked by build architecture, like
# the codex block below; the build then asserts `gh --version` prints GH_VERSION so a bad pin or a
# renamed binary fails the build instead of shipping silently.
# Bump: change GH_VERSION above to a version from https://github.com/cli/cli/releases (tag is
# "v<version>"), and GH_SHA256_AMD64/_ARM64 from that release's gh_<version>_checksums.txt.
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) gh_arch=linux_amd64; gh_sha256="$GH_SHA256_AMD64" ;; \
      arm64) gh_arch=linux_arm64; gh_sha256="$GH_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_${gh_arch}.tar.gz"; \
    echo "${gh_sha256}  /tmp/gh.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/gh.tar.gz -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_VERSION}_${gh_arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh*; \
    gh --version | grep -qF "gh version ${GH_VERSION}"

# Baked default identity for the `git commit` calls goals.py runs inside cloned target repos --
# overridable per the runbook's executor.env template (GIT_AUTHOR_NAME/EMAIL, GIT_COMMITTER_NAME/EMAIL;
# git honours those env vars over --system config).
RUN git config --system user.name "orchestrator-executor" \
    && git config --system user.email "orchestrator@localhost"

# Reuse an existing group/user at that id (renaming it to orch) instead of failing outright -- the host
# uid/gid picked up via `id -u`/`id -g` can collide with a base-image system account.
RUN set -eux; \
    existing_group="$(getent group "$GID" | cut -d: -f1)"; \
    if [ -n "$existing_group" ]; then groupmod -n orch "$existing_group"; else groupadd -g "$GID" orch; fi; \
    existing_user="$(getent passwd "$UID" | cut -d: -f1)"; \
    if [ -n "$existing_user" ]; then usermod -l orch -d /home/orch -m -g orch "$existing_user"; \
    else useradd -m -u "$UID" -g orch -d /home/orch -s /bin/bash orch; fi

# Pre-create orch's XDG-ish dirs before anything mounts into them (the gh bind-mount in
# docker-compose.executor.yml lands at /home/orch/.config/gh) so the mount parent is orch-owned.
RUN install -d -o orch -g orch /home/orch/.config /home/orch/.local /home/orch/.cache

# uv: official installer, pinned via the version segment in the URL (astral-sh/uv supports this).
# Downloaded to a file first; verified against UV_INSTALL_SHA256 when set, else just sha256sum'd into
# the build log for an audit trail -- astral-sh doesn't publish a separate checksum for the installer
# script itself.
# Bump: change UV_VERSION above to a tag from https://github.com/astral-sh/uv/releases.
RUN curl -LsSf -o /tmp/uv-install.sh "https://astral.sh/uv/${UV_VERSION}/install.sh" \
    && if [ -n "$UV_INSTALL_SHA256" ]; then \
         echo "${UV_INSTALL_SHA256}  /tmp/uv-install.sh" | sha256sum -c -; \
       else \
         sha256sum /tmp/uv-install.sh; \
       fi \
    && env UV_INSTALL_DIR=/usr/local/bin sh /tmp/uv-install.sh \
    && rm -f /tmp/uv-install.sh

# Codex CLI: static musl binary from the openai/codex GitHub release (no linux-gnu asset is published).
# Asset picked by build architecture, like the gh block above; the tarball is verified against the
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

USER orch

# Claude Code: native installer (not npm), documented at https://code.claude.com/docs/en/setup. The
# installer accepts a version argument (`bash -s <version>`) — pinned here via CLAUDE_VERSION.
# Downloaded to a file first; verified against CLAUDE_INSTALL_SHA256 when set, else just sha256sum'd
# into the build log for an audit trail -- Anthropic doesn't publish a separate checksum for the
# installer script itself.
# Bump: change CLAUDE_VERSION above to a version from `claude --version` upstream or the release notes.
RUN curl -fsSL -o /tmp/claude-install.sh https://claude.ai/install.sh \
    && if [ -n "$CLAUDE_INSTALL_SHA256" ]; then \
         echo "${CLAUDE_INSTALL_SHA256}  /tmp/claude-install.sh" | sha256sum -c -; \
       else \
         sha256sum /tmp/claude-install.sh; \
       fi \
    && bash /tmp/claude-install.sh "${CLAUDE_VERSION}" \
    && rm -f /tmp/claude-install.sh \
    && claude --version

USER root

# Source copied last (below every installer layer above) so editing it doesn't bust the installer
# layers' cache and re-download them. Left root:root (no --chown) so orch can run the orchestrator but
# not rewrite its own code or hooks; only the venv below is handed back to orch.
COPY . /opt/orchestrator
WORKDIR /opt/orchestrator

# --frozen (here and in the entrypoint below) refuses to update or regenerate uv.lock -- /opt/orchestrator
# is root-owned (see COPY above) so orch could not write a regenerated lock back to disk at runtime
# anyway; --frozen makes that explicit and fails fast on a lock mismatch instead of uv silently trying
# and hitting a permission error, or drifting onto unpinned dependency versions.
# UV_CACHE_DIR keeps this root-run sync out of /home/orch/.cache -- writing there as root would leave
# root-owned cache entries orch can't add to at runtime.
RUN UV_CACHE_DIR=/root/.cache/uv uv sync --frozen && chown -R orch:orch /opt/orchestrator/.venv

USER orch

# No args (the compose service) -> serve. Args given (e.g. the version-check smoke test in this task's
# acceptance criteria) -> run them instead, so `docker run --rm <image> sh -c '...'` works directly.
ENTRYPOINT ["/bin/sh", "-c", "if [ \"$#\" -gt 0 ]; then exec \"$@\"; else exec uv run --frozen orchestrator serve --host 0.0.0.0 --port 8090; fi", "--"]
