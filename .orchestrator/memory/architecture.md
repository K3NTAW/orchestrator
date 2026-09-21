repo map 8227caf 2026-09-21

## __init__.py — Tri-model orchestrator: bus + account pool + spawner + merge queue, exposed to t…

## acceptance.py — Helpers for checking test ids named by task acceptance criteria.
- def named_tests(acceptance)

## attribution.py — Shared run attribution, independent of scorecard aggregation.
- def task_class(task)

## baseline.py — Frozen Phase I efficiency measurements and None-safe baseline comparisons.
- def save(label, root, since)

## bench.py
- def set_model(model_id, by, **metrics)

## bus.py — Task bus: SQLite hot index + one JSON file per task (git-backed via the orchestr…
- def ready(task_or_id)
- def update(tid, **fields)

## bus_mcp.py — MCP server `bus`: the only way workers report back.
- def bus_read(task_id, status, status_not, role, compact, full)

## cli.py — orchestrator status | cost [--by role|tier|account|task] | hold A [--minutes] |…
- def main()

## critical_path.py — Deterministic critical-path priority for ready execute tasks.
- def rank(ready_ids, tasks, durations)

## daemon.py — Heartbeat and pipeline driver: requeue running tasks whose process died, notify…
- def tick(pool, stop_event)

## decision.py — Pure decision routing; callers gather state and perform every side effect.
- def routes_enabled(cfg)

## executor.py — Executor: GPT-6 Astra via ``codex exec``.
- def start(task_id, prompt, executor_id, packet_meta)

## failures.py — Validated failure evidence and conservative change-risk classification.
- def touch_areas(task, cfg)

## gitutil.py — Read-only git evidence shared by pipeline and Planner routing.
- def moved_paths(base_ref, target_ref, cwd, git)

## goals.py — Goal lifecycle: launch a headless Planner session against any target repo, track…
- def task_pr_url(task)

## handover.py — Auto-handover: refreshes a resumable snapshot at the end of plan.md so a fresh P…
- def write(reason)

## install.py — Scaffold the §9 orchestrator layout into another repo, so `ORCH_ROOT=<target>` a…
- def install(target, orch_repo)

## interference.py — Deterministic, side-effect-free interference checks for orchestrator tasks.
- def select_wave(ready, running, tasks, capacity, order, graph, rules)

## jev.py — Jev (TypeSafe AI): typed questions about a state answered with calibrated probab…
- def score(state, instructions, levels)

## jev_gate.py — PreToolUse gate for worker sessions (.claude/hooks/jev-gate.sh): ask Jev whether…
- def should_skip(tool_name, tool_input)

## jev_rank.py — jev-compactor-style relevance ranking: score each candidate item against a goal…
- def rank(items, goal_text, *, threshold, batch)

## jev_route.py — Fail-open Jev classification for shadow executor-routing evidence.
- def shadow_context(task, pool)

## mcp.py — MCP server `orchestrator`: scheduler for the Planner.
- def status()

## merge.py — Serial merge queue: one at a time, rebase onto target -> tests-green -> fast-for…
- def merge(task_id, target, *, refresh_repomap)

## notify.py — Notifications with persistent transition deduplication.
- def notify_once(task_id, transition, msg)

## planner_runs.py — Autonomous Planner decisions: launch a short-lived headless Planner to act on on…
- def tick(pool)

## pool.py — Account pool: per-account 5h window / daily budget / cooldown, least-loaded-with…
- def is_rate_limited(text)
- def parse_reset_hint(text, default)

## repomap.py — A compact, static map of the orchestrator package for worker context.
- def build(root, budget_chars, rev)

## schedlog.py — Append-only scheduler telemetry, tolerant of interrupted or malformed writes.
- def read_with_malformed(name)

## scorecard.py — Per-executor outcome rollup: runs/*.jsonl + tasks/T-*.json, keyed by executor id…
- def write(card)

## serve.py — orchestrator.serve: the per-user goal endpoint that kgpt's modules/orchestrator…
- def main(host, port)

## spawn.py — Spawner: one `claude -p` subprocess per job, bound to one account via CLAUDE_CON…
- def trust_workspace(config_dir, wt)
