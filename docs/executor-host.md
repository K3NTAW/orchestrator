# Running the executor on kenta-server

The executor runs `orchestrator serve` in its own container (Ubuntu 24.04, Docker) so Claude Code, Codex,
uv, git and gh have a current userland independent of the host. Credentials live in files on the host,
bind-mounted read-write into the container. This is a one-time setup per host; do it once, then the
container restarts on its own.

## Prerequisites

- Docker Engine and the `docker compose` plugin on the host.
- A host user that will own the credential directories (any user is fine; its uid/gid drive the build).
- Network egress to `claude.ai`, `github.com`, `astral.sh`, `cli.github.com` for the image build, and to
  Anthropic's and OpenAI's APIs at runtime.

## 1. Align UID/GID

The container's `orch` user is created at build time with `--build-arg UID`/`--build-arg GID`, so files it
writes into the bind-mounted credential directories are readable/writable by the same host user that owns
those directories. On the host:

```bash
id -u   # note this value
id -g   # note this value
export UID
export GID
```

`docker-compose.executor.yml` reads `UID`/`GID` from the environment (default `1000`/`1000` if unset) and
passes them as build args.

## 2. Create the credential directories

Pick a root directory for credentials, e.g. `~/orch-creds`, and export it as `ORCH_CREDS` — the compose file
mounts everything under it:

```bash
export ORCH_CREDS=~/orch-creds
mkdir -p "$ORCH_CREDS"/claude-a "$ORCH_CREDS"/claude-b "$ORCH_CREDS"/codex "$ORCH_CREDS"/gh
chown -R "$UID:$GID" "$ORCH_CREDS"
```

These four directories mount to `/home/orch/.claude-a`, `/home/orch/.claude-b`, `/home/orch/.codex` and
`/home/orch/.config/gh` inside the container — the same paths `.orchestrator/protected-paths.txt` lists as
protected, so no agent running on this host can read or overwrite them through the guardrails hook.

## 3. Mint the Claude Code OAuth tokens (on your laptop, not the server)

Claude Code's OAuth tokens are minted interactively, so run this on a machine where you can complete a
browser login, once per account:

```bash
CLAUDE_CONFIG_DIR=~/.claude-a claude setup-token
CLAUDE_CONFIG_DIR=~/.claude-b claude setup-token
```

Each command prints a token. Paste them into `executor.env` (see step 5) — never into a committed file:

```
CLAUDE_OAUTH_TOKEN_A=<paste here>
CLAUDE_OAUTH_TOKEN_B=<paste here>
```

## 4. Log in Codex and gh inside the container

`codex login` and `gh auth login` write into `~/.codex` and `~/.config/gh` respectively, which are the
directories bind-mounted in step 2 — run them once through the container so the resulting auth files land in
the mounted volume instead of the container's throwaway filesystem:

```bash
docker compose -f docker-compose.executor.yml run --rm orchestrator-executor codex login --device-auth
docker compose -f docker-compose.executor.yml run --rm orchestrator-executor gh auth login
```

Both are interactive (device-auth flow / browser or token paste) — follow the prompts.

## 5. Write executor.env

Create `executor.env` next to `docker-compose.executor.yml` (it's gitignored and dockerignored; never commit
it). It holds:

```
ORCH_SERVICE_TOKEN=<paste here>
CLAUDE_OAUTH_TOKEN_A=<paste here>
CLAUDE_OAUTH_TOKEN_B=<paste here>
ORCH_NOTIFY_URL=<paste here>
GH_READ_TOKEN=<paste here>
```

`ORCH_SERVICE_TOKEN` is the bearer the kgpt module presents to `/goals`; generate it yourself (e.g.
`openssl rand -hex 32`) — it isn't minted by any of the CLIs above.

## 6. Configure pool.toml and repos.toml on the host repo

In the repo checked out at the path the executor works from, `.orchestrator/pool.toml` needs each Claude
account to point at the env var holding its token, and any role secret in the `env:` form so it reads from
this process's environment instead of shelling out to a Keychain wrapper (there's no Keychain on a headless
host):

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

Add the target repo to `.orchestrator/repos.toml` (create it from `.orchestrator/repos.toml.example` if it
doesn't exist yet):

```toml
[kgpt]
path = "/work/kgpt"
git_url = "https://github.com/<org>/kgpt.git"
```

## 7. Bring it up

```bash
docker compose -f docker-compose.executor.yml up -d --build
```

No ports are published — the container is reached on the home docker network by the kgpt module, not from
the host's own network namespace.

## 8. Smoke test

From the host (or any container on the same docker network), replacing `$ORCH_SERVICE_TOKEN` with the value
you put in `executor.env`:

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
  `gh auth login` command from step 4; it overwrites the auth file in the mounted volume in place, no
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
