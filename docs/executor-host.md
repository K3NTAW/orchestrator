# Running the executor on kenta-server

The executor runs `orchestrator serve` in its own container (Ubuntu 24.04, Docker) so Claude Code, Codex,
uv, git and gh have a current userland independent of the host. Credentials live in files on the host,
bind-mounted read-write into the container. This is a one-time setup per host; do it once, then the
container restarts on its own.

## Prerequisites

- Docker Engine with BuildKit, and Docker Compose v2.24+ (needed for the `env_file` long syntax used here).
- A host user that will own the credential directories (any user is fine; its uid/gid drive the build).
- Network egress to `claude.ai`, `github.com`, `astral.sh`, `cli.github.com` for the image build, and to
  Anthropic's and OpenAI's APIs at runtime.
- Build natively on each host's own architecture -- kenta-server builds amd64 natively, an Apple Silicon
  laptop builds arm64 natively for smoke tests. Never build under emulation: on Apple Silicon, QEMU
  emulation of amd64 segfaults running the Claude Code installer's Bun binary (no AVX under emulation).

## 1. Align UID/GID

The container's `orch` user is created at build time with `--build-arg UID`/`--build-arg GID`, so files it
writes into the bind-mounted credential directories are readable/writable by the same host user that owns
those directories. On the host, write a compose `.env` file (gitignored and dockerignored; `docker compose`
reads it automatically from the directory it runs in):

```bash
printf 'ORCH_UID=%s\nORCH_GID=%s\n' "$(id -u)" "$(id -g)" > .env
```

`docker-compose.executor.yml` reads `ORCH_UID`/`ORCH_GID` from that file (default `1000`/`1000` if unset)
and passes them as build args. Use the same values below wherever a `chown` needs to match the container's
`orch` user.

## 2. Create the credential, work and config directories

Credentials live under `~/orch-creds` (the compose file mounts everything under `ORCH_CREDS`, which
defaults to that path):

```bash
export ORCH_CREDS=${ORCH_CREDS:-$HOME/orch-creds}
mkdir -p "$ORCH_CREDS"/claude-a "$ORCH_CREDS"/claude-b "$ORCH_CREDS"/codex "$ORCH_CREDS"/gh ./work ./config
source .env
chown -R "$ORCH_UID:$ORCH_GID" "$ORCH_CREDS" ./work ./config
```

The four credential directories mount to `/home/orch/.claude-a`, `/home/orch/.claude-b`, `/home/orch/.codex`
and `/home/orch/.config/gh` inside the container — the same paths `.orchestrator/protected-paths.txt` lists
as protected (alongside the `~/orch-creds` root itself), so no agent running on this host can read or
overwrite them through the guardrails hook. If you use a root other than `~/orch-creds`, add that path to
`.orchestrator/protected-paths.txt` too — the hook only expands `~`, never `${ORCH_CREDS}`, so the file must
name the concrete path you actually chose.

`./work` is where the executor clones and works in target repos (mounted to `/work`); `./config` holds the
`repos.toml` and `pool.toml` the container reads (see step 5).

## 3. Mint the Claude Code OAuth tokens (on your laptop, not the server)

Claude Code's OAuth tokens are minted interactively, so run this on a machine where you can complete a
browser login, once per account:

```bash
CLAUDE_CONFIG_DIR=~/.claude-a claude setup-token
CLAUDE_CONFIG_DIR=~/.claude-b claude setup-token
```

Each command prints a token. Paste them into `executor.env` (see step 4) — never into a committed file:

```
CLAUDE_OAUTH_TOKEN_A=<paste here>
CLAUDE_OAUTH_TOKEN_B=<paste here>
```

## 4. Write executor.env

Create `executor.env` next to `docker-compose.executor.yml` (it's gitignored and dockerignored; never commit
it) *before* running any `docker compose run`/`up` command — the compose file requires it (`env_file` is
marked `required: true`). Start from this template, which lists the variable names only:

```
ORCH_SERVICE_TOKEN=<paste here>
CLAUDE_OAUTH_TOKEN_A=<paste here>
CLAUDE_OAUTH_TOKEN_B=<paste here>
ORCH_NOTIFY_URL=<paste here>
GH_READ_TOKEN=<paste here>
GH_WRITE_TOKEN=<paste here>
```

Fill in the values:

- `CLAUDE_OAUTH_TOKEN_A`/`_B`: printed by the `claude setup-token` commands in step 3.
- `ORCH_SERVICE_TOKEN`: the bearer the kgpt module presents to `/goals`; generate it yourself (e.g.
  `openssl rand -hex 32`) — it isn't minted by any of the CLIs here.
- `ORCH_NOTIFY_URL`, `GH_READ_TOKEN`: as used elsewhere in this orchestrator's config.
- `GH_WRITE_TOKEN`: read by `pool.toml`'s `[secrets.planner]` — the Planner uses it to open the goal PR
  (`goal/<parent>` → `main`) at the end of a run. Mint a fine-grained GitHub personal access token scoped
  to just the target repo(s), with only Contents (read/write) and Pull requests (read/write) permissions —
  not a classic token with broader `repo` scope.

The image also bakes a default git identity (`orchestrator-executor <orchestrator@localhost>`) for the
`git commit` calls the executor runs inside cloned target repos. To use a different identity, add these
optional variables to `executor.env` — git honours them over the baked default:

```
GIT_AUTHOR_NAME=<paste here>
GIT_AUTHOR_EMAIL=<paste here>
GIT_COMMITTER_NAME=<paste here>
GIT_COMMITTER_EMAIL=<paste here>
```

## 5. Configure repos.toml and pool.toml in ./config

The container reads `repos.toml` and `pool.toml` from `./config` on the host (mounted read-only at
`/opt/orchestrator/.orchestrator/repos.toml` and `/opt/orchestrator/.orchestrator/pool.toml` — the paths
`orchestrator serve` and `goals.start` read by default). Create them there, not by editing this repo's own
`.orchestrator/` directly:

```bash
cp .orchestrator/repos.toml.example ./config/repos.toml
cp .orchestrator/pool.toml ./config/pool.toml
```

Edit `./config/pool.toml` so each Claude account points at the env var holding its token, and any role
secret uses the `env:` form so it reads from this process's environment instead of shelling out to a
Keychain wrapper (there's no Keychain on a headless host):

```toml
[[claude_accounts]]
id = "A"
config_dir = "~/.claude-a"
oauth_token_env = "CLAUDE_OAUTH_TOKEN_A"   # see C-O2a

[[claude_accounts]]
id = "B"
config_dir = "~/.claude-b"
oauth_token_env = "CLAUDE_OAUTH_TOKEN_B"

[secrets.scout]
GH_READ_TOKEN = "env:GH_READ_TOKEN"
[secrets.review]
GH_READ_TOKEN = "env:GH_READ_TOKEN"
```

Edit `./config/repos.toml` to add the target repo:

```toml
[kgpt]
path = "/work/kgpt"
git_url = "https://github.com/<org>/kgpt.git"
```

After editing either file, `docker compose -f docker-compose.executor.yml restart` picks up the change —
no rebuild needed, since they're bind-mounted, not baked into the image.

This only covers the *executor's* own config. A repo that `goals.start` has already installed into (i.e.
already has a committed `.orchestrator/pool.toml` from a prior goal) keeps using its own committed file, not
this one — `install()` never overwrites an existing `pool.toml`. To change settings for an already-installed
target repo, open a PR on that repo, not on the host.

## 6. Find the docker network and export ORCH_NET

The executor joins the same docker network the kgpt module runs on rather than publishing ports. Find that
network's name on the kgpt host — it's the kgpt compose project's default network:

```bash
docker network ls
```

Export it before any `docker compose` command against this file:

```bash
export ORCH_NET=<network name from above>
```

## 7. Log in Codex and gh inside the container

`codex login` and `gh auth login` write into `~/.codex` and `~/.config/gh` respectively, which are the
directories bind-mounted in step 2 — run them once through the container so the resulting auth files land in
the mounted volume instead of the container's throwaway filesystem. This needs `.env` (`ORCH_UID`/
`ORCH_GID`), `ORCH_CREDS` and `ORCH_NET` exported and `executor.env`/`./config` already in place (steps
1–6):

```bash
docker compose -f docker-compose.executor.yml run --rm orchestrator-executor codex login --device-auth
docker compose -f docker-compose.executor.yml run --rm orchestrator-executor gh auth login
```

Both are interactive (device-auth flow / browser or token paste) — follow the prompts.

## 8. Generate the git credential helper config

`gh auth setup-git` writes the credential helper entries `git push`/`git clone` over https need into
`~/.gitconfig`. `docker-compose.executor.yml` mounts that file **read-only** into the container (so no
agent running inside it can rewrite git's credential configuration at runtime), which means the file has
to exist on the host, produced through a one-off invocation, before `gh` can be used for git operations
through the compose service:

1. Create the (empty) file the mount will point at, so Docker bind-mounts a regular file rather than
   creating a directory at that path:

   ```bash
   touch "$ORCH_CREDS/gitconfig"
   ```

2. Run `gh auth setup-git` through the image built in step 7, bypassing compose's read-only declaration
   with a plain `docker run` that mounts the same host path read-write for this one invocation only (this
   needs step 7's `gh auth login` to have already populated `$ORCH_CREDS/gh`):

   ```bash
   docker run --rm \
     --user "${ORCH_UID}:${ORCH_GID}" \
     -e HOME=/home/orch \
     -v "$ORCH_CREDS/gh:/home/orch/.config/gh" \
     -v "$ORCH_CREDS/gitconfig:/home/orch/.gitconfig" \
     orchestrator-executor gh auth setup-git
   ```

3. From here on, `docker compose ... up` mounts `$ORCH_CREDS/gitconfig` read-only at `/home/orch/.gitconfig`
   (step 9). Re-run steps 1–2 (skip the `touch` if the file already exists) if you rotate the `gh` token
   with a host or protocol `gh auth setup-git` needs to add a new entry for.

## 9. Bring it up

```bash
docker compose -f docker-compose.executor.yml up -d --build
```

No ports are published — the container is reached on the home docker network (`ORCH_NET`) by the kgpt
module, not from the host's own network namespace.

## 10. Smoke test

No ports are published, so the host can't reach the container by its own network namespace. From the host,
go through `docker compose exec`:

```bash
docker compose -f docker-compose.executor.yml exec orchestrator-executor curl -f http://localhost:8090/healthz
```

From any other container on the same docker network (e.g. the kgpt module), reach it by service name
directly, replacing `$ORCH_SERVICE_TOKEN` with the value you put in `executor.env`:

```bash
curl -f http://orchestrator-executor:8090/healthz

curl -f -X POST http://orchestrator-executor:8090/goals \
  -H "Authorization: Bearer $ORCH_SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"repo": "kgpt", "goal": "add a README badge", "requester": "smoke-test"}'
```

A scratch repo (empty or throwaway) is safer for this than a real one — the goal actually launches a
Planner session against it.

## Rotating a token

- **Claude OAuth token (A or B)**: rerun `CLAUDE_CONFIG_DIR=~/.claude-a claude setup-token` (or `-b`) on your
  laptop, update `CLAUDE_OAUTH_TOKEN_A`/`_B` in `executor.env`, then `docker compose -f
  docker-compose.executor.yml up -d` to restart the container with the new value.
- **ORCH_SERVICE_TOKEN**: generate a new value, update `executor.env` and the kgpt module's config together
  (both sides must agree), then restart the container. The old token stops working the moment the container
  restarts, so coordinate the kgpt-side update first.
- **Codex / gh**: rerun the `docker compose ... run --rm orchestrator-executor codex login --device-auth` or
  `gh auth login` command from step 7; it overwrites the auth file in the mounted volume in place, no
  restart needed.

## When a goal doesn't start

`POST /goals` returning `422` means `goals.start` refused — the response body's `reason` field says why
(also logged server-side, credentials redacted). If it returns `201` but nothing seems to happen, check the
Planner's own log inside the target repo:

```
<repo>/.orchestrator/runs/planner-<goal_id>.log
```

`<goal_id>` is in the `201` response body. If the container itself won't start, `docker compose -f
docker-compose.executor.yml logs -f orchestrator-executor` shows the `uv run orchestrator serve` output
directly.
