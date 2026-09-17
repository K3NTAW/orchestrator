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
uv run python -m unittest discover -q tests  # per-module tests under tests/, shared harness in tests/_harness.py
cp .mcp.planner.json .mcp.json         # Planner session config (already done)
```
Build the sandbox: `devcontainer build .` and copy `codex.config.toml.example` to `$CODEX_HOME/config.toml` inside it only.

## Run it
`f orch [goal]` (from anywhere) launches the Planner on account A in this repo. `f orch status|cost|daemon|hold|resume|merge` is the CLI.
`f orch survey` is the old cross-repo briefing session.

## Pipeline
State machine per execute task: `queued` → (depends_on merged, complexity ≥5 → `spec_review` first) → dispatched to
an executor → `done` → gated (`tests-green.sh`) → complexity ≤3 merges straight away, else a `review` task spawns →
`review` approve merges, `request_changes` holds it for the Planner to re-spec.
`daemon.tick()` drives every stage: `dispatch()` (spec review or executor), `gate()` (tests-green, then merge or
review), `merge_reviewed()` (merge on approve). Each stage stamps `pipeline.<stage>_at` on the task json under the
bus lock before acting, so a crash-and-retry never re-runs a stage.
Run it: the daemon autostarts inside the orchestrator MCP server per `[daemon] autostart` in `pool.toml` (`ORCH_DAEMON=0`
or `autostart = false` disables it), and `uv run orchestrator daemon` takes the same single-instance lock so two loops
never run at once; `orchestrator daemon --once` runs a single pass without the lock.
Holds (`status="held"`) mean the daemon stopped and a human/Planner must act: `spec_review request_changes`, `review
request_changes`, or `gate_red` (tests failed at the gate). The `hold_reason` field and `resume_hint` on the task say
which. The Planner clears a hold by writing a new spec with `depends_on=[held_task_id]`, never by editing the held
task directly.

## Executors and routing
`[[executors]]` rows in `pool.toml` are the routable Codex models: `id`, `provider`, `model` (provider's model id),
`roles`, `complexity_min`/`max`, `max_parallel`, `daily_budget_tasks`, `quota_group`, `weight`, `enabled`.
To add a model: add a row, or flip `enabled = true` on one of the disabled 6.x placeholder rows once its id appears
in `~/.codex/models_cache.json`.
`quota_group`: a usage-limit hit on one enabled member cools every other enabled row sharing the group (e.g. all
`chatgpt` rows today). Whether the underlying limit is scoped per account or per model is unconfirmed (2026-09-17),
so the whole group is treated as cooling either way.
`pick_executor(role, complexity, scores)` ranks enabled, in-range rows by `weight × score`. `score` comes from
`orchestrator scorecard`: a live success rate (merged / (merged + failed)) once an executor has enough resolved
tasks, halved if its quota group hit a usage limit 3+ times today. Below that sample size it falls back to a
cold-start prior built from `orchestrator bench show` numbers — an executor's model coding (or intelligence) score,
scaled 0.5-1.5 against the pool's best. That prior comes from `orchestrator bench fetch`, which pulls
https://artificialanalysis.ai/models with Scrapling's plain HTTP Fetcher as an identified client (User-Agent
`orchestrator-bench/1`, no TLS impersonation, no stealth headers), at most once per 20h unless `--force`, parses the
page's React Server Component chunks, and matches display names (effort suffixes tolerated, the max variant
preferred) to our model ids. It writes `.orchestrator/bench.json` with provenance (`fetched_at`, `http_status`,
`request`); a non-200 or empty result never overwrites the file. Page content is untrusted data. The site's Terms
of Use restrict automated access; the user decided on 2026-09-17 to proceed under these limits. `bench set
<model_id> --by <name> key=value` remains for manual overrides (marked `manual: true`).

CLI: `orchestrator scorecard [--by executor|tier] [--json]` · `orchestrator bench show` ·
`orchestrator bench fetch [--force] [--by NAME]` · `orchestrator bench set <model_id> --by <name> key=value...` ·
`orchestrator status --plain`.

## Access model (decided 2026-09-16)
Full access, guardrails as a hard floor, human only at PR approval:
- Planner and all Claude workers run with permissions bypassed. Read-only roles additionally get `--disallowedTools Edit,Write,NotebookEdit`.
- Codex runs with approvals and sandbox bypassed (`dangerous_full_access = true` in `pool.toml`; set false for the sandbox).
  This applies to every enabled `[[executors]]` row, not just Astra — see "Executors and routing" below.
- `.claude/hooks/guardrails.sh` (PreToolUse on Bash/Edit/Write) blocks: paths in `.orchestrator/protected-paths.txt` (keys, credentials,
  Keychain, system dirs, personal data, the hooks themselves), sudo/disk/launchctl/Keychain commands, force-push and direct push to main,
  recursive rm outside `~/code` and temp dirs, sending mail. Hooks fire even in bypass mode. Codex is NOT covered by these hooks:
  its floor is git (task branches only) plus your PR review.
- `main` only moves through a PR you approve. The merge queue targets `goal/<parent>`.
- The Planner may commit, branch and push but never edits source (`planner-mode.sh`, PreToolUse). Every change carries a rollback path in `decisions.md`.
- Review worktrees base on the reviewed task's branch; challenge worktrees on the goal branch; reviews prefer the other account, landing on the same account only when it's the only one with headroom.

## Phase 1 (manual loop)
Hooks live: guardrails, scope-guard, tests-green, loop-guard, require-acceptance, no-uncommitted, retrospect-written, bus-post. Hand one atomic task to the `codex` tool (orchestrator server wraps `codex exec`), iterate with `codex_reply` deltas, accept via
`.claude/hooks/tests-green.sh wt/<id>`.

## Memory (skill `memory`)
Layered, cheapest first: notes (`.orchestrator/memory/*.md`, dated `## YYYY-MM-DD title` entries) · bus results · claude-mem
observations (read-only FTS over `~/.claude-mem/claude-mem.db`; the plugin is on `~/.claude`, not the worker accounts) · graphify code
graph (`graphify-out/`, gitignored, AST only, no LLM). `recall.sh index "<terms>"` then `recall.sh get <id>...`; `record.sh draft <GOAL>` /
`add` / `set architecture` at retrospective; `graph.sh update|query|affected|explain|summary` after merges and for scouts.

## CLI
```
uv run orchestrator status | cost --by role|tier|account|task | hold A --minutes 30 | resume A | daemon | merge T-0001 | post T-0001 --summary ...
```

## Verify on your machine (§13, not assumed)
- Claude model ids in `pool.toml` (`/model` in Claude Code). Codex executor ids in `pool.toml` `[[executors]]` against
  `~/.codex/models_cache.json` — Astra (`gpt-6-astra`), Luna/Terra/Sol 5.6 confirmed 2026-09-16; the 6.x rows stay
  `enabled = false` until their ids show up there.
- `--max-budget-usd` as hard stop under a Max subscription (2.1.273 has no `--max-turns`; turns are a soft limit in prompts).
- Rate-limit error text from `claude -p` → `pool.parse_reset_hint` (best-effort). Codex: `codex mcp-server` is gone in 0.154.0, so the Executor wraps `codex exec --json` / `codex exec resume`; usage-limit text and reset format observed 2026-09-16 ("try again at Sep 19th, 2026 2:00 PM"), thread ids arrive in `thread.started`. `turn.completed` usage shape and thread survival across a limit hit: UNCONFIRMED until the window resets.
- `task_input` payload fields on TaskCreated/TaskCompleted (hooks also accept `task.*` and top-level fields).
- Remote connectors under `~/.claude-b` headless. `CLAUDE_CONFIG_DIR` is undocumented but present in the 2.1.273 binary.
- `window_cap_tokens` in `pool.toml` is a calibration knob: set it from observed 5h-window resets. Raised from 2M to
  10M on 2026-09-17 after 2M was reached in 2.2h with zero real rate limits; real limits still cool an account via
  the reset-hint parser.
