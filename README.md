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
`orchestrator scorecard --by task|goal` rolls runs/*.jsonl up by task or by goal (role split in percent of usd,
with an `other` bucket for any role outside execute/review/spec_review/scout so the percentages always cover
every child task). Each goal row's `planner_runs` column is that goal's own decision-run count from
`runs/planner_runs.json` (plus their usd sum when the records carry one): `-` when the file is missing, `0 runs`
when it exists but has none for that goal, else `<n> runs`. Planner *transcript* tokens are pool-wide and reset
daily, so they aren't a per-goal column; the table ends with one footer line, either
`planner (transcripts, today): <tokens> tokens across <n> accounts` (summed from `.orchestrator/planner_usage.json`)
or `planner: -` when that file is missing.
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
Each execute or fix round starts with a **Worker packet** assembled from repository data. Its ordered sections are
objective, acceptance, base, write scope, read scope, relevant tests, symbols, gotchas, decisions, verify, and
evidence. The packet is capped at 4,800 characters, trimming the bottom sections first and pointing to
`bus_read(task_id)` when the full task is needed.

Scouts are capped (2 per goal, 12 turns, $1.00, 600s) and open with a memory recall step; see `.orchestrator/prompts/scout.md`.

Bounded outputs: review diffs are capped at `[limits].review_diff_chars` (12,000 by default; expand with the displayed
`git -C <worktree> diff -- <paths>` command), spec-review code at `[limits].spec_review_code_chars` (8,000; expand with
`git -C <worktree> show HEAD:<path>`), test failures retain the full log at the printed `.orchestrator/runs/tests/` path,
and recall shows `[limits].recall_hits` (30; expand with `recall.sh index "<terms>" --limit N`). For bus state deltas,
use `bus_events(since)`.

### Routes

| Goal type | Route |
| --- | --- |
| Clear, localized change | Brief spec, one executor, deterministic gates, human PR. |
| Uncertain location or behaviour | One targeted investigation for a named uncertainty in plan.md, then spec. |
| Independent changes | Separate workers with an explicit interface contract in each spec. |
| High-risk or architectural | Detailed planning, spec review, execution, independent review. |

The default is zero scouts: the Planner greps for what it needs first. Warn above five files; split by independently verifiable behaviour and dependency boundaries, never merely to satisfy the count.
State machine per execute task: `queued` → (depends_on merged, complexity ≥ `spec_review_min` → `spec_review` first,
on `spec_review_tier`) → dispatched to an executor → `done` → gated (`tests-green.sh` passing is the merge bar).
By default (`[review].code_review = "security_paths"`) a task merges straight through unless its merged diff
touches a `security_paths` glob, a `semantic_paths` glob, or an added diff line matches a named
`semantic_patterns` regex. Security paths are evaluated first, then semantic paths and semantic patterns; any
match requires exactly one review on `security_review_tier`, never the model that executed the task, with the
security checklist always forced on. `code_review = "never"` drops review entirely; `code_review = "always"` is the pre-2026-09-19
complexity-driven split (`direct_merge_max`/`two_reviews_from` thresholds), still available but not the default.
An orphaned result (executor died, daemon re-gated its commit) gets exactly one review whatever `code_review`
says. `pipeline.review_reason` on the gated task records which branch fired (`none`, `security_paths:<glob>`,
`semantic_path:<glob>`, `semantic_pattern:<name>`, `security_paths_empty`, `diff_unavailable`, `orphaned`, or
`always`) so a later change to `[review]` can't move the goalposts on a task already past this stage. Approval is
bound to the task branch head in `reviewed_sha`; if that head moves, the approval is void and the daemon opens a
fresh review. A task with one review merges on its first `approve`; a task with two
(only possible under `code_review = "always"`) merges once every review has approved, and any single
`request_changes` holds it for the Planner to re-spec regardless of what the other review said. Policy and the
cost measurement that motivated dropping code review by default are noted next to `[review]` in `pool.toml`. The
human reviews every merged PR regardless of pipeline outcome.
`daemon.tick()` drives every stage: `dispatch()` (spec review or executor), `gate()` (tests-green, then merge or
review), `merge_reviewed()` (merge once every review of a task has approved). Each side-effecting stage stamps a
lease (`[daemon].stage_lease_s`, 900 seconds by default) and writes a done marker after issuing its action; old
pre-lease stamps are deliberately not reconciled. On expiry, a queued dispatch is retried (a running dispatch is
left to dead-pid requeue; Codex runs carry no pid), direct merges are retried, and unclaimed child reviews are
re-spawned or missing reviews created. Already-landed merges are marked done. Held tasks are excluded from lease
reconciliation.
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
A filtered `bus_read` (no `task_id`) returns compact rows by default — no spec, events, acceptance or scope — pass
`full=True` or `bus_read(task_id=...)` for the full task.

Automatic fix rounds run after dispatch, gate, and reviewed merges, before the autonomous Planner. Gate failures
are routine only when pytest `FAILED <nodeid>` or unittest `FAIL`/`ERROR` lines can be extracted; unknown runner
output escalates. Review holds are routine only when every rejecting review comment is inside scope. Rounds follow
the fix lineage and stop at `[daemon].auto_fix_rounds` (default 2). Each hold uses `planner_runs._held_at(task)` as
its deduplication key; skipped/escalated holds notify once per key.

### Autonomous decisions
`pool.toml`'s `[planner] autonomous` (default `false`) lets `daemon.tick()` launch a short-lived headless Planner on
its own, without an interactive session, to act on one of three decision points: `scouts_done` (a goal's scouts are
all done/failed and no execute task has split off yet), `held` (an execute task is held), or `closable` (every
execute task of a goal is merged and nothing is left queued or running). `orchestrator/planner_runs.py` tracks one
record per `(goal_id, kind, payload_key)` in `.orchestrator/runs/planner_runs.json`; `reconcile()` (called first
every tick) resolves a running record whose process has exited to `exited_ok` (something already finished the
decision -- for `held`, that means a fix-round task now exists with `depends_on`/`constraints.fix_round_for`
pointing at the held task, created after this decision run started), `exited_early` (retry, up to one more
attempt), or `gave_up` (two early exits; notifies once and blocks that key for good); `decision_points()` then
yields every key with no blocking (`running`/`claimed`/`exited_ok`/`gave_up`) record. `run()` claims a key --
writing a `claimed` row, itself blocking -- inside the same `bus.locked()` block as the check that it isn't
already decided, before it runs either guard or `goals.launch_planner`, so two callers racing for the same key
(`daemon --once` and the background loop, say) can never both launch; a guard skip afterwards flips that same
row to `skipped` (never blocking, one row per key with a running `skip_count`) instead of leaving it stuck
`claimed`. `tick()` launches at most one per pass. Two guards make sure an autonomous launch never runs alongside
a human: `ORCH_DAEMON_HOST=mcp` (set by the orchestrator MCP server's `main()` entrypoint, before it autostarts
the daemon) and `.orchestrator/planner_session.json` (written atomically by that same call, removed at exit, and
named by pid so a stale file is never mistaken for a live session). Meant for the executor container, where no
interactive Planner session ever attaches -- leave it off anywhere one might.
Each run receives a compact decision packet assembled from bus and run data; the interactive handover threshold is
`[planner].handover_context_tokens` and packet size is bounded by `[planner].decision_packet_chars`.

### Jev
Jev (TypeSafe AI) answers typed questions about a piece of state with calibrated probabilities instead of free
text -- `POST https://api.typesafe.ai/v1/systemone` (`orchestrator/jev.py`, stdlib `urllib` only). Off by default
(`pool.toml [jev].enabled = false`); the API key comes from `[secrets.jev].TYPESAFE_API_KEY`, resolved through
`spawn.resolve_secrets` and never logged. `ask(state, questions)` truncates `state` to `max_state_chars` and
redacts token-like substrings (`jev.redact`, tested separately) before anything leaves the machine; a 429/529
gets one 0.5s-backoff retry, and every other failure mode -- disabled, no key, timeout, HTTP error, invalid
JSON, daily budget exhausted -- makes `ask()` return `None` (fail-open) instead of raising. Usage is logged to
`.orchestrator/runs/jev-<date>.jsonl` and tallied against `daily_budget_tokens` in `.orchestrator/jev_state.json`.
`noul()`, `choice()` and `score()` wrap `ask()` for yes/no, multiple-choice and leveled-score questions. Egress
note: task specs, tool-call metadata and memory titles leave the machine; file contents never do.

Jev API confidence is optional: with confidence at least 0.6 the gate blocks at P(redundant) ≥ 0.85 or P(needed) ≤ 0.15; without confidence it uses stricter thresholds of 0.92 and 0.08. The gate defaults to deterministic `sample` mode (10% of calls), detects repeated inputs while tracking target mtimes, and `orchestrator jev diagnose` reports waste, repeats, latency and role/tool splits. Use its JSONL export for hand labelling before enabling any block rule; `block_repeats` can then deny unchanged repeats locally without a network call.

## Executors and routing
`[[executors]]` rows in `pool.toml` are the routable Codex models: `id`, `provider`, `model` (provider's model id),
`roles`, `complexity_min`/`max`, `max_parallel`, `daily_budget_tasks`, `quota_group`, `weight`, `enabled`.
To add a model: add a row, or flip `enabled = true` on one of the disabled 6.x placeholder rows once its id appears
in `~/.codex/models_cache.json`.
`quota_group`: a usage-limit hit on one enabled member cools every other enabled row sharing the group (e.g. all
`chatgpt` rows today). Whether the underlying limit is scoped per account or per model is unconfirmed (2026-09-17),
so the whole group is treated as cooling either way.
`pick_executor(role, complexity, scores, task)` first uses the task's explicit `constraints.task_class`, or infers
`security` for configured security paths, `architectural` at complexity 7+, `debugging` for fix tasks, `mechanical`
at complexity 3 or below, and `unfamiliar` otherwise. Once an executor has at least `[models].min_samples` merged
tasks in that class, routing selects the lowest local expected token cost that clears `[models].success_floor`:
initial execution median plus repair probability times repair median, plus review and spec-review medians. Until then,
it ranks enabled, in-range rows by `weight × score`. `score` comes from
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
