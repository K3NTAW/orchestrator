repo map 160f28c 2026-09-21

## __init__.py — Tri-model orchestrator: bus + account pool + spawner + merge queue, exposed to t…

## acceptance.py — Helpers for checking test ids named by task acceptance criteria.

## allocation.py — Expected-value executor allocation with hard eligibility boundaries.

## attribution.py — Shared run attribution, independent of scorecard aggregation.

## baseline.py — Frozen Phase I efficiency measurements and None-safe baseline comparisons.

## bench.py

## bus.py — Task bus: SQLite hot index + one JSON file per task (git-backed via the orchestr…

## bus_mcp.py — MCP server `bus`: the only way workers report back.

## capacity.py — Read-only executor capacity snapshots and deterministic task admission.

## cli.py — orchestrator status | cost [--by role|tier|account|task] | hold A [--minutes] |…

## concurrency.py — Pure adaptive-concurrency selection for scheduler-ready tasks.

## critical_path.py — Deterministic critical-path priority for ready execute tasks.

## daemon.py — Heartbeat and pipeline driver: requeue running tasks whose process died, notify…

## decision.py — Pure decision routing; callers gather state and perform every side effect.

## decision_log.py — Structured, bounded evidence for orchestrator decisions.

## duration.py — Empirical execution-duration estimates for scheduler task ranking.

## executor.py — Executor: GPT-6 Astra via ``codex exec``.

## failures.py — Validated failure evidence and conservative change-risk classification.

## gitutil.py — Read-only git evidence shared by pipeline and Planner routing.

## goals.py — Goal lifecycle: launch a headless Planner session against any target repo, track…

## handover.py — Auto-handover: refreshes a resumable snapshot at the end of plan.md so a fresh P…

## install.py — Scaffold the §9 orchestrator layout into another repo, so `ORCH_ROOT=<target>` a…

## interference.py — Deterministic, side-effect-free interference checks for orchestrator tasks.

## jev.py — Jev (TypeSafe AI): typed questions about a state answered with calibrated probab…

## jev_gate.py — PreToolUse gate for worker sessions (.claude/hooks/jev-gate.sh): ask Jev whether…

## jev_planner.py — Jev shadow signal for Planner tier routing decisions.

## jev_points.py — Experimental, fail-open Jev decision points.

## jev_rank.py — jev-compactor-style relevance ranking: score each candidate item against a goal…

## jev_route.py — Fail-open Jev classification and bounded executor-routing adjustment.

## jev_sched.py — Bounded Jev shadow signals for ambiguous scheduler interference pairs.

## mcp.py — MCP server `orchestrator`: scheduler for the Planner.

## merge.py — Serial merge queue: one at a time, rebase onto target -> tests-green -> fast-for…

## merge_pressure.py — Measure merge-queue pressure and selectively defer interfering work.

## notify.py — Notifications with persistent transition deduplication.

## planner_packet.py — Pure, compact packets for Planner decisions and Opus-to-Fable escalation.

## planner_router.py — Pure Planner tier policy; callers resolve aliases and gather all state.

## planner_runs.py — Autonomous Planner decisions: launch a short-lived headless Planner to act on on…

## planner_scorecard.py — Read-side Planner routing evidence; no routing decisions or ledger writes.

## planner_shadow.py — Isolated shadow Planner launches and bounded, prompt-free evaluation rows.

## planner_taxonomy.py — Purpose-first taxonomy for Planner decisions.

## planner_telemetry.py — Per-invocation Planner telemetry and goal-level usage attribution.

## pool.py — Account pool: per-account 5h window / daily budget / cooldown, least-loaded-with…

## promotion.py — Evidence-based, shadow-first feature promotion recommendations.

## repomap.py — A compact, static map of the orchestrator package for worker context.

## roadmap.py — Machine-readable implementation status for the orchestrator roadmaps.

## sched_scorecard.py — Join scheduler predictions to