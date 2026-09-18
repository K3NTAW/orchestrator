# orchestrator

Tri-model orchestrator: Planner (Claude Code, Fable 5.1) · scouts (Opus/Sonnet/Haiku via `claude -p`, two Max accounts) ·
Executor (GPT-6 Astra via `codex exec`, sandboxed) · cross-model review · serial merge queue · hook-enforced gates.
Spec: the blueprint this repo implements (v2.0, 2026-09). Layout follows its §9.

## Use on a project
This repo *is* the §9 layout. `git worktree add` only ever checks out committed files, so a target repo needs the
scaffold committed before ORCH_ROOT, spawn, the bus and merge (all rooted at ORCH_ROOT) will work on it. From this
repo, run `uv run orchestrator install /path/to/target` — it's idempotent, prints `created`/`kept`/`updated` per
file, and wires `.mcp*.json` to run this repo's code with `ORCH_ROOT` set to the target. Then, in the target repo:
commit the scaffold, and start the Planner there with `ORCH_ROOT=/path/to/target`.
To run the executor itself in a container on a dedicated host instead of locally, see
[docs/executor-host.md](docs/executor-host.md).

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
`orchestrator pick planner|scout|review|execute` tallies Planner usage fresh, then prints `<account_id>\t<config_dir>`
for `pool.pick(role)`, or exits 3 with `hold: no account with headroom`; a launcher not running the daemon calls this
so its account choice still reflects current Planner usage.
`uv run orchestrator handover [--reason TEXT]` writes/replaces the `## Auto-handover` section at the end of
`.orchestrator/plan.md` (open goals, child tasks by status, worktrees, the last 5 bus events); `daemon.tick()`
calls it too, at most once every 15 minutes, so the checkpoint is never older than that even with no Planner running.

## Planner usage is counted from transcripts
The Planner itself is an interactive `claude` session, not a worker spawned by `run_claude`, so it never posts a JSON
result carrying a `usage` block — without `tally_planner()` the pool would only ever see the workers it spawns and
stay blind to the largest consumer of any account's window. `Pool.tally_planner()` reads Claude Code's own transcript
files for this project under each account's `config_dir` (`<config_dir>/projects/<encoded ROOT>/*.jsonl`), sums the
same `input_tokens + output_tokens + cache_read_input_tokens // 10` `run_claude` uses for assistant turns, gating the
day and window counters independently per line (a same-day line outside the current window still counts toward the
day, and vice versa), and folds both totals into `Account.utilization()` and the daily-budget check alongside the
worker totals. It reads incrementally (a per-file byte offset persists in `.orchestrator/planner_usage.json`, written
only by `tally_planner()` under an flock so it isn't raced by `pool_state.json` saves) and skips a missing transcripts
directory with one stderr line per account per process rather than raising on every tick. `daemon.tick()` calls it
once per tick; `orchestrator pick <role>` calls it directly (tally failures there print one stderr line and fall
through to the pool's stored numbers rather than crashing pick) for callers that need a fresh account choice without
a running daemon.

## Pipeline
Scouts are capped (2 per goal, 12 turns, $1.00, 600s) and open with a memory recall step; see `.orchestrator/prompts/scout.md`.
State machine per execute task: `queued` → (depends_on merged, complexity ≥ `spec_review_min` → `spec_review` first,
on `spec_review_tier`) → dispatched to an executor → `done` → gated (`tests-green.sh`) → complexity ≤
`direct_merge_max` merges straight away; complexity between `direct_merge_max` and `two_reviews_from` spawns one
`review` task, tiered to whichever model did not execute the task; complexity ≥ `two_reviews_from` spawns two,
the second on a different model than the executor (a different account when possible). A task with one review
merges on its first `approve`; a task with two merges only once every review of it has approved, and any single
`request_changes` holds it for the Planner to re-spec regardless of what the other review said. These four
thresholds live in `pool.toml`'s `[review]` table (defaults: `spec_review_min = 6`, `direct_merge_max = 3`,
`two_reviews_from = 7`, `spec_review_tier = "sonnet"`); policy and the cost measurement that motivated it are
noted there.
`daemon.tick()` drives every stage: `dispatch()` (spec review or executor), `gate()` (tests-green, then merge or
review), `merge_reviewed()` (merge once every review of a task has approved). Each stage stamps `pipeline.<stage>_at`
on the task json under the bus lock before acting, so a crash-and-retry never re-runs a stage.
Run it: the daemon autostarts inside the orchestrator MCP server per `[daemon] autostart` in `pool.toml` (`ORCH_DAEMON=0`
or `autostart = false` disables it), and `uv run orchestrator daemon` takes the same single-instance lock so two loops
never run at once; `orchestrator daemon --once` runs a single pass without the lock.
`notify()` always logs `[notify] <msg>` to stderr. Set `ORCH_NOTIFY_URL` (e.g. `https://ntfy.kentawaibel.com/orchestrator`,
shape only, not this repo's setup) to also POST the message as a plain-text body to that URL; a failed or unreachable
webhook only logs `[notify] webhook failed: ...` and never breaks a tick. On macOS a desktop notification fires too
unless `ORCH_NOTIFY_DESKTOP=0`; it's a no-op on other platforms regardless.
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

## Headless hosts
The laptop's `secrets_for_role` command form shells out to `f tok get` (a Keychain wrapper) and the `[[claude_accounts]]`
tables assume an interactive `/login`. Neither works on a headless Linux host. Two options, independent of each other:
- **Claude account auth**: run `claude setup-token` once per `CLAUDE_CONFIG_DIR` to mint a long-lived
  `CLAUDE_CODE_OAUTH_TOKEN`, export it under a name of your choice, and set that name as `oauth_token_env` on the
  matching `[[claude_accounts]]` row in `pool.toml`. `spawn.run_claude` puts the value into the child's
  `CLAUDE_CODE_OAUTH_TOKEN` env var when the account has `oauth_token_env` set and that variable is present in the
  environment; it is never logged.
- **Role secrets**: for a `[secrets.<role>]` entry, use `ENV_NAME = "env:OTHER_NAME"` instead of a shell command to
  read `OTHER_NAME` straight from this process's environment (e.g. set by systemd or CI). A missing env var is
  skipped with a stderr note naming the variable, not its value.

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
