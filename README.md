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

## Program guides

The program entry points are the [context-economy audit](docs/context-economy/00-audit.md),
[skill-intelligence audit](docs/skill-intelligence/00-audit.md), and
[Hermes hardening audit](docs/hermes-hardening/00-audit.md).

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

| Command | Purpose |
| --- | --- |
| `orchestrator scorecard --scheduling` | Show scheduling interference scorecard. |
| `orchestrator scorecard --strategies` | Show workflow-strategy scorecard. |
| `orchestrator explain <task>` | Show recorded decision evidence for a task. |
| `orchestrator promotion` | Show shadow-first feature promotion recommendations. |
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
Run rows in `.orchestrator/runs/<date>.jsonl` carry task/goal, role, tier, account, provider,
outcome and token buckets, plus the following accounting fields (unknown optional values are omitted):

- `attempt` (default 1), `decision_kind`, `payload_key`, `route`, `route_reason`, `client_version`,
  `policy_version` (first 12 SHA-256 hex characters of pool.toml followed by sorted prompt Markdown bytes;
  cached until a policy file's mtime or the file set changes).

Goal token statistics count tasks with run rows, summing retries per task; fewer than five tasks show
`n=<count> range <min>-<max>` instead of a median. The goal JSON includes a `tokens_per_accepted_goal`
summary whose `tokens` ratio is `null` when no goals are accepted; text displays `undefined (0 accepted goals)`.

`uv run orchestrator handover [--reason TEXT]` writes/replaces the `## Auto-handover` section at the end of
`.orchestrator/plan.md` (open goals, child tasks by status, worktrees, the last 5 bus events); `daemon.tick()`
calls it too, at most once every 15 minutes, so the checkpoint is never older than that even with no Planner running.

`orchestrator scorecard --parallelism [--goal ID] [--json]` reports per-goal and total executor and Claude worker concurrency (averaged over busy time), queue, dependency, execution, review and merge waits, wall time, DAG critical paths (including task count and whether missing task durations make the path partial), conflicts, rebase failures, stale events, repair rounds and dispatch-skip reasons. Missing timing evidence displays `undefined`; absent scheduler logs contribute zero events. Baseline saves include a `parallelism` block, and comparisons report its numeric deltas while accepting older snapshots without it.

## Phase H: efficiency

### Accounting

Run rows add `attempt` (default 1), `decision_kind`, `payload_key`, `route`, `route_reason`, `client_version`, and
`policy_version`. `orchestrator scorecard --planner` reports Planner decision kinds, routes, reasons, premium
exceptions, and token totals. Goal accounting sums retries per task: five or more tasks report the median, while
smaller samples report `n=<count> range <min>-<max>`. Tokens per accepted goal are undefined when zero goals were
accepted (`null` in JSON and `undefined (0 accepted goals)` in text), never zero or a division error.

Rollback: set no flag; revert merged accounting commit `6d2824e`.

### Decision routes

`[planner.routes]` has `enabled = true`, `auto_open_pr = false`, `escalate_tier = "fable"`,
`investigate_tier = "sonnet"`, `investigate_max_complexity = 4`, and
`premium_launches_soft_per_goal = 2`. `routine` performs deterministic daemon work without a Planner;
`investigate` launches the cheaper tier for a small, low-risk uncertainty; `escalate` launches the Planner tier
for risk or incomplete evidence. The premium limit is soft: crossing it records a justified exception in the run
ledger instead of blocking work. One launch coalesces all decision points for a goal and records its bus cursor;
the child-state `state_version` prevents a second launch until state changes. Auth, quota, or availability failures
cool the affected account, restore the prior cursor/state guard, and retry after infrastructure backoff. Set
`enabled = false` to restore the per-point, always-escalate path.

Rollback: set `[planner.routes] enabled = false`, or revert merged routing commits `e3b2b36`, `68ff495`, and `f2a1cf1`.

### Budget reservations

`[limits] reservations = true` atomically reserves daily tokens and optional per-goal role USD before a launch.
Estimates use the role's last 20 runs once at least five exist (median tokens and USD); before that they use the
role's `max_budget_usd` ceiling and `default_tokens_per_usd`. A reservation uses `[daemon].stage_lease_s` (default
900 seconds), workers heartbeat it while alive, and completion releases it with actual usage. Refusal is `None`,
so dispatch leaves the task queued and emits the budget notification rather than starting an unbudgeted worker.

Rollback: set `[limits] reservations = false`, or revert merged reservation commit `668a9c1`.

### Failure kinds and signatures

`[daemon] flaky_rerun_max = 1` and `flaky_rerun_timeout_s = 600` bound evidence-only reruns. Kinds are
`code_defect`, `invalid_spec`, `flaky`, `environment`, `conflict`, `quota`, `permissions`, and `unknown`.
Signatures hash the kind, validated failing test ids, and normalized rejecting review comments. The automatic
fix loop stops and escalates when a signature repeats, the lineage reaches `auto_fix_rounds`, evidence is unknown,
or the failure is infrastructure/spec/risk work that requires intervention.

Rollback: revert merged failure-classification commit `b9c2972`.

### Gate checks acceptance-named tests

The gate extracts `tests/path.py::test_name` references (and same-criterion `::test_name` shorthand) from each
acceptance criterion and verifies that every named test function exists before accepting the result. A missing
file or function makes the gate red, so a result cannot pass merely by omitting its promised regression test.

Rollback: revert merged acceptance-gate commit `06b7ce4`.

### Packet header and acceptance never dropped

Every worker packet starts with `packet v<hash> base <sha> sources pool.toml@<policy> gotchas@<hash>` so its exact
body, base, and policy inputs are auditable. Size trimming removes lower-priority evidence first; the complete
acceptance section, base, and verification command are never dropped, even when that makes the packet exceed its
nominal cap.

Rollback: revert merged packet-contract commit `2868122`.

### bus_events filters, ensure_worktree reuse and review re-spawn

`bus_events(since, limit, role, task_ids)` filters by role and task id while `next_since` advances over every
examined event, preventing filtered readers from looping over irrelevant rows. `ensure_worktree` reattaches an
existing `task/<id>` branch after a stale worktree is pruned. Reconciliation re-spawns a missing review worker
after its lease expires, but reuses the review task/branch and does not duplicate a live or completed review.

Rollback: revert merged recovery commits `2868122` and `ec54196`.

### Handover snapshot hash

Handover hashes the sorted task id/status/hold/merge snapshot and stores it in `handover_state.json`. An unchanged
snapshot skips rendering and leaves `plan.md` untouched; a changed snapshot is still written atomically under the
bus compare-and-swap lock.

Rollback: revert merged handover-recovery commit `ec54196`.

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

The `[scheduler]` table defaults to `mode = "shadow"`, which keeps first-come dispatch and logs proposed waves to `waves.jsonl` in the scheduler log directory. `active` dispatches only the selected wave, deferring predicted interference and backfilling from later eligible tasks; `off` disables wave selection and logging. `soft_conflict_policy = "defer"` postpones soft conflicts behind clean candidates, and `max_wave = 0` uses available capacity (a positive value caps the wave). Wave rows include eligible and running ids, baseline order, selected wave, deferrals, predicted conflicts and whether the wave was applied.
Every green-gated task records a stale-work check against its goal branch before review or merge. Set `[scheduler].stale_rebase = true` to rebase high-risk work first; conflicts hold the task with `stale_rebase_conflict` and escalate to the Planner.
Ready tasks are ranked by their unresolved downstream critical path, using deterministic cold-start estimates of 600, 1200 and 2400 seconds for complexity bands 1-3, 4-6 and 7-10 respectively.
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

Executor routing can collect Jev evidence with `[jev.routing] mode = "shadow"`. It batches seven task-shape questions, compares a deliberately simple hypothetical choice with the unchanged pool choice, and logs both under the Jev attribution bucket. Only redacted task metadata and memory titles are sent—never repository file contents or diffs—and hard eligibility constraints remain authoritative. The code default is `off`; `active` is accepted as a shadow-mode preview until active ranking lands in P5.

## Executors and routing
`[[executors]]` rows in `pool.toml` are the routable Codex models: `id`, `provider`, `model` (provider's model id),
`roles`, `complexity_min`/`max`, `max_parallel`, `daily_budget_tasks`, `quota_group`, `weight`, `enabled`.
To add a model: add a row, or flip `enabled = true` on one of the disabled 6.x placeholder rows once its id appears
in `~/.codex/models_cache.json`.

### Adding or removing models
To add a Codex model, copy its exact id from `~/.codex/models_cache.json` into a new `[[executors]]` row and set
the routing fields explicitly. For example:

```toml
[[executors]]
id = "my-codex-model"
provider = "codex"
model = "gpt-5.6-luna" # use the model id from ~/.codex/models_cache.json
roles = ["execute"]
complexity_min = 1
complexity_max = 10
max_parallel = 1
daily_budget_tasks = 40
quota_group = "chatgpt"
weight = 1.0
enabled = true
```

To remove a model from routing, set its row's `enabled = false`. If the host should run without Codex, set
`enabled = false` on every `provider = "codex"` row; `[codex].on_exhausted` still controls the legacy fallback.

Claude models use an alias in `[models]`, followed by an executor row whose id is `claude:<alias>`:

```toml
[models]
opus = "claude-opus-5"

[[executors]]
id = "claude:opus"
provider = "claude"
roles = ["execute"]
complexity_min = 1
complexity_max = 8
max_parallel = 1
daily_budget_tasks = 40
quota_group = "claude"
weight = 1.0
enabled = true
```

The Claude row's `model` is optional; when present it must equal the alias value (here, `claude-opus-5`). An invalid
Claude row loads disabled and records the reason. It is routed only while at least one `[[claude_accounts]]` account
has headroom, so keep `max_parallel` small. `[limits].max_parallel_claude_workers` is not yet enforced for routed
Claude executes at scheduler level (follow-up in `.orchestrator/plan.md`). Routed Claude tasks use the same path as
the Claude fallback: an account with headroom executes them, and review uses another account and a different model.
Reviewers never run the model that executed the task, compared by model id.

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
graph (`graphify-out/`, gitignored, AST only, no LLM). Progressive recall stops once its hit or character budget is met and never consults a richer layer merely because it exists; unavailable layers are recorded but skipped. `recall.sh index --progressive "<terms>"` reports consulted layers and records attributable memory usage; worker packets deliberately use notes and bus only. `record.sh draft <GOAL>` / `add` / `set architecture` at retrospective; `graph.sh update|query|affected|explain|summary` after merges and for scouts.

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

## Efficiency telemetry (Phase I P0)

Run rows record `bucket` (planner, scout, execute, fix_round, spec_review, review,
challenge, jev, memory, or other), `lineage_root` (initial execute task), `round_index`,
`band` (1-3, 4-6, 7-10), `task_class`, `executor`, and `model`.
Normalized usage fields are `input_uncached_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `output_tokens`, `reasoning_tokens`, and `total_tokens`.
Raw total = uncached + cache read + cache write + output; reasoning is informational,
already included in output. `usd_source` distinguishes `reported` from `token_estimate`.
Efficiency uses effective tokens E = uncached + output + floor(cache read / 10) + cache write.
Legacy total-only rows retain their recorded total rather than inventing token buckets.

Lifecycle stamps: task `created_at`; pipeline `first_green_at`, `gate_attempts`,
`gate_reds`; `lineage_fix_rounds`; and task `accepted_at`. Missing stamps remain
undefined, never zero. Acceptance means a merged initial execute lineage; repairs,
reviews, and spec reviews contribute usage to that lineage without extra acceptances.

`orchestrator scorecard --efficiency [--by goal|executor|band|class|role] [--json]`
reports the following (a zero denominator produces an undefined ratio):

| Metric | Formula |
| --- | --- |
| tokens / usd | Sum E / sum recorded or estimated USD in the selected rows |
| calls / turns | Usage row count / sum recorded turns (missing turns contribute zero) |
| accepted_tasks | Count accepted initial execute lineages |
| tokens / usd / calls / turns per accepted task | Mean corresponding lineage total over accepted tasks |
| tokens / usd per accepted goal | Mean accepted-goal total: accepted lineage usage plus goal planner/scout usage |
| tokens_to_first_green | Sum lineage E with timestamp ≤ first_green_at; baseline reports mean over accepted tasks |
| time_to_first_green_s / time_to_accepted_s | Corresponding stamp minus created_at |
| fix_rounds / fix_round_tokens | Recorded lineage count (else linked descendants) / sum fix_round E |
| first_pass_rate | Accepted tasks with first green, zero fixes and zero gate reds / tasks with defined first-pass evidence |
| fix_round_rate / avg_fix_rounds | Tasks with ≥1 fix / defined tasks; mean fix count over defined tasks |
| first_pass_defined_count / fix_round_defined_count | Number of accepted tasks with the respective evidence |
| median_tokens_per_accepted_task / max_tokens_per_accepted_task | Median / maximum accepted lineage E |
| gate_success_share | Sum(gate_attempts − gate_reds) / sum(gate_attempts), for tasks recording both |
| review_request_changes_rate | Review tasks with request_changes / review tasks with approve or request_changes |
| model_distribution | Row count and sum E per model and bucket |
| breakdown | Sum E by Planner, Scout, Execution, Fix rounds, Spec review, Code review, Challenge, Jev, Other; Total is their sum |
| pipeline_amplification / Amplification | Total E / initial Execution E |

Amplification is an observation metric, not a target: reducing necessary review can
lower it while worsening quality. Read it alongside first-pass and repair outcomes.
The `unknown` group retains unattributed usage; unaccepted usage remains in window
breakdowns. Unknown models are explicit, and unrecognized buckets contribute to Other.
Legacy rows receive read-time attribution from task metadata and fix/review links,
with explicit recorded attribution preserved; history is never rewritten.
Review quality is reported with `orchestrator scorecard --reviews`.
Use `--by role|packet_version|tier|band|reviewed_executor` to select the cohort.
JSON output is available with `--json` for analysis and baseline tooling.
The `pre-packet` packet-version bucket keeps reviews predating packet metadata comparable.
Verdicts and severity totals show what reviewers found, not merely what they cost.
Finding rates use review counts; missing denominators remain undefined rather than zero.
Token and USD medians expose the review-context cost for each cohort.
Findings per million tokens helps compare differently sized review packets.
Two completed reviews also report deduplicated defects, overlap, and pass-two additions.
Compare packet-version cohorts alongside those quality measures; reviews are never removed to save tokens.
Missing lifecycle evidence stays undefined. Role groups measure usage rather than outcomes.

Before any P1+ change, run `orchestrator baseline save phase-h-code`.
Snapshots live in `.orchestrator/baselines/<label>.json` and freeze all six groupings,
window row counts, and the pool, Jev, review, executor, planner-route, packet, client,
policy, git and package fingerprint. Mixed recorded client/policy versions remain lists.
Use `baseline save phase-i-shadow --since 2026-09-20T00:00:00Z` for a usage window;
undated/out-of-window rows and out-of-window acceptances are excluded. Lineage totals
then cover only that window; lifecycle stamps remain lifetime observations. Without
`--since`, all available history is measured. Naive ISO timestamps are interpreted as UTC.
Use `baseline show phase-h-code [--json]`, `baseline list`, then
`baseline compare phase-h-code phase-i-shadow [--json]` after the change.
Deltas are after − before; percent is 100 × delta / |before|, undefined at zero.
Lower primary metrics and repair/rejection rates are flagged better; higher first-pass
and gate success rates are better. Worse non-inferiority metrics have `*` in text.
The footer is `non-inferior: no` for any regression, `undefined` for missing evidence,
or `yes` when all non-inferiority metrics are defined and unchanged or improved.
Fingerprint diffs name changed leaf keys, so configuration changes remain visible.
