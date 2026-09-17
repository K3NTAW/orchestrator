# plan.md — Planner checkpoint
(empty: no goal in progress. The Planner overwrites this after every fan-out and merge.)

## Last goal (closed 2026-09-18 03:00): T-0043 parallel machine
goal/T-0043 = per-module tests + harness, depends_on, daemon stages (dispatch/gate/review/merge, stamps, lock, async, error holds, safe notify), spec_review role, shape-agnostic fit_result, budgets 30M/40M, docs. PR 5 → main.

## Handover for the next session (read first)
1. Start fresh with `f orch` so the orchestrator MCP servers load the new spawn/bus code (base_for, depends_on, spawn_spec_review). Two stale server pairs from this session die with it.
2. After PR 5 merges: run `uv run orchestrator daemon` in a second terminal (or `daemon --once` from the Planner) — it dispatches ready tasks, gates, spawns reviews, merges on approve. The Planner then writes specs with depends_on and intervenes only on held tasks.
3. Codex returns 2026-09-19 14:00: first goal = one complexity-3 execute task to observe luna/terra/sol routing and the real usage-limit scope (per account vs per model); update pool.toml quota_group semantics from what is observed.
4. Stale worktrees wt/T-00xx (≈35) remain; removal is a deletion → ask the human once, then `git worktree remove` each merged one.
5. Known small debts: challenge-web prompt template; pool_state.json last-writer-wins (lock like bus.lock); executor.md prompt references scripts/tests_green.sh which is not at the worktree root.
