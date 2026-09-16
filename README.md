# orchestrator

Tri-model orchestrator: Planner (Claude Code, Fable 5.1) · scouts (Opus/Sonnet/Haiku via `claude -p`, two Max accounts) ·
Executor (GPT-6 Astra via `codex exec`, sandboxed) · cross-model review · serial merge queue · hook-enforced gates.
Spec: the blueprint this repo implements (v2.0, 2026-09). Layout follows its §9.

## Use on a project
This repo *is* the §9 layout. Drop it into a project (copy, or `git subtree add`), or point `ORCH_ROOT` at it and run the
Planner from the project root. Worktrees live in `wt/<task-id>`; the bus lives in `.orchestrator/`.

## Phase 0 (you, once)
```bash
CLAUDE_CONFIG_DIR=~/.claude-a claude   # /login account A, /model -> confirm ids in .orchestrator/pool.toml
CLAUDE_CONFIG_DIR=~/.claude-b claude   # /login account B
codex login                            # ChatGPT 20x (install: curl -fsSL https://chatgpt.com/codex/install.sh | sh)
uv sync                                # python deps (mcp)
uv run python -m unittest -v           # 9 tests: bus rules, pool selection, hooks, merge queue
cp .mcp.planner.json .mcp.json         # Planner session config (already done)
```
Build the sandbox: `devcontainer build .` and copy `codex.config.toml.example` to `$CODEX_HOME/config.toml` inside it only.

## Run it
`f orch [goal]` (from anywhere) launches the Planner on account A in this repo. `f orch status|cost|daemon|hold|resume|merge` is the CLI.
`f orch survey` is the old cross-repo briefing session.

## Access model (decided 2026-09-16)
Full access, guardrails as a hard floor, human only at PR approval:
- Planner and all Claude workers run with permissions bypassed. Read-only roles additionally get `--disallowedTools Edit,Write,NotebookEdit`.
- Codex runs with approvals and sandbox bypassed (`dangerous_full_access = true` in `pool.toml`; set false for the sandbox).
- `.claude/hooks/guardrails.sh` (PreToolUse on Bash/Edit/Write) blocks: paths in `.orchestrator/protected-paths.txt` (keys, credentials,
  Keychain, system dirs, personal data, the hooks themselves), sudo/disk/launchctl/Keychain commands, force-push and direct push to main,
  recursive rm outside `~/code` and temp dirs, sending mail. Hooks fire even in bypass mode. Codex is NOT covered by these hooks:
  its floor is git (task branches only) plus your PR review.
- `main` only moves through a PR you approve. The merge queue targets `goal/<parent>`.

## Phase 1 (manual loop)
Hooks live: guardrails, scope-guard, tests-green, loop-guard, require-acceptance, no-uncommitted, retrospect-written, bus-post. Hand one atomic task to the `codex` tool (orchestrator server wraps `codex exec`), iterate with `codex_reply` deltas, accept via
`.claude/hooks/tests-green.sh wt/<id>`.

## CLI
```
uv run orchestrator status | cost --by role|tier|account|task | hold A --minutes 30 | resume A | daemon | merge T-0001 | post T-0001 --summary ...
```

## Verify on your machine (§13, not assumed)
- Claude model ids in `pool.toml` (`/model` in Claude Code). Astra id `gpt-6-astra` confirmed 2026-09-16.
- `--max-budget-usd` as hard stop under a Max subscription (2.1.273 has no `--max-turns`; turns are a soft limit in prompts).
- Rate-limit error text from `claude -p` → `pool.parse_reset_hint` (best-effort). Codex: `codex mcp-server` is gone in 0.154.0, so the Executor wraps `codex exec --json` / `codex exec resume`; usage-limit text and reset format observed 2026-09-16 ("try again at Sep 19th, 2026 2:00 PM"), thread ids arrive in `thread.started`. `turn.completed` usage shape and thread survival across a limit hit: UNCONFIRMED until the window resets.
- `task_input` payload fields on TaskCreated/TaskCompleted (hooks also accept `task.*` and top-level fields).
- Remote connectors under `~/.claude-b` headless. `CLAUDE_CONFIG_DIR` is undocumented but present in the 2.1.273 binary.
- `window_cap_tokens` in `pool.toml` is a calibration knob: set it from observed 5h-window resets.
