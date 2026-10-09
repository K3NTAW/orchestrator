repo map 60c8f81 2026-10-09

## __init__.py — Tri-model orchestrator: bus + account pool + spawner + merge queue, exposed to t…

## acceptance.py — Helpers for checking test ids named by task acceptance criteria.

## allocation.py — Expected-value executor allocation with hard eligibility boundaries.

## attribution.py — Shared run attribution, independent of scorecard aggregation.

## baseline.py — Frozen Phase I efficiency measurements and None-safe baseline comparisons.

## bench.py

## bus.py — Task bus: SQLite hot index + one JSON file per task (git-backed via the orchestr…

## bus_mcp.py — MCP server `bus`: the only way workers report back.

## cache_telemetry.py — Prompt-cache economics derived from canonical run rows.

## capacity.py — Read-only executor capacity snapshots and deterministic task admission.

## claude_cli.py — One resolver for the `claude` and `uv` binaries.

## cli.py — orchestrator status | cost [--by role|tier|account|task] | hold A [--minutes] |…

## concurrency.py — Pure adaptive-concurrency selection for scheduler-ready tasks.

## context_eval.py — Deterministic fixture evaluation for context-economy shadow features.

## context_router.py — Pure, deterministic routing of canonical evidence into context levels.

## context_scanner.py — Deterministic, heuristic inspection of untrusted context; no I/O or model calls.

## context_scorecard.py — Context-economy reporting from run-log packet telemetry.

## contracts.py — Worker contracts from .orchestrator/prompts/{scout,review,spec-review,challenge}…

## critical_path.py — Deterministic critical-path priority for ready execute tasks.

## daemon.py — Heartbeat and pipeline driver: requeue running tasks whose process died, notify…

## decision.py — Pure decision routing; callers gather state and perform every side effect.

## decision_log.py — Structured, bounded evidence for orchestrator decisions.

## duration.py — Empirical execution-duration estimates for scheduler task ranking.

## env_policy.py — Allowlisted worker environments with names-only, best-effort telemetry.

## evidence.py — Canonical, content-addressed evidence for deterministic context selection.

## executor.py — Executor: GPT-6 Astra via ``codex exec``.

## failures.py — Validated failure evidence and conservative change-risk classification.

## gate.py — Bounded execution for repository gates.

## gitutil.py — Read-only git evidence shared by pipeline and Planner routing.

## goals.py — Goal lifecycle: launch a headless Planner session against any target repo, track…

## handoff_scorecard.py — Read-only economics for executor and Planner handoffs.

## handover.py — Auto-handover: refreshes a resumable snapshot at the end of plan.md so a fresh P…

## harness_depth.py — Deterministic harness depth and dispatch-local shadow evidence.

## hermes_eval.py — Deterministic Hermes integration checks, isolated from production state.

## install.py — Scaffold the §9 orchestrator layout into another repo, so `ORCH_ROOT=<target>` a…

## instructions.py — Deterministic conditional instruction selection and composition.

## interference.py — Deterministic, side-effect-free interference checks for orchestrator tasks.

## jev.py — Jev (TypeSafe AI): typed questions about a state answered with calibrated probab…

## jev_gate.py — PreToolUse gate for worker sessions (.claude/hooks/jev-gate.sh): ask Jev whether…

## jev_planner.py — Jev shadow signal for Planner tier routing decisions.

## jev_points.py — Experimental, fail-open Jev decision points.

## jev_rank.py — jev-compactor-style relevance ranking: score each candidate item against a goal…

## jev_route.py — Fail-open Jev classification and bounded executor-routing adjustment.

## jev_sched.py — Bounded Jev shadow signals for ambiguous scheduler interference pairs.

## jev_skills.py — Bounded Jev classification for only the ambiguous skill-routing bucket.

## machine.py — Machine-wide leases and account usage shared by all orchestrator repos.

##