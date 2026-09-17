# plan.md — Planner checkpoint
(empty: no goal in progress. The Planner overwrites this after every fan-out and merge.)

## Last goal (closed 2026-09-18 04:00): T-0043 parallel machine + daemon autostart
goal/T-0043 → PR 5. Per-module tests, depends_on, daemon stages with stamps and lock, async dispatch, error holds, safe notify, spec_review role, shape-agnostic fit_result, budgets 30M/40M, daemon autostart inside the MCP server, closed-goal and ancestor-merge guards, docs. 86 tests green.

## Handover for the next session (read first)
1. Start fresh with `f orch`: the orchestrator MCP servers reload (base_for, depends_on, spawn_spec_review) and the daemon autostarts with them ([daemon] autostart in pool.toml; ORCH_DAEMON=0 to disable). Two stale server pairs from the old session die with it.
2. Merge PR 5 when reviewed. From then on: write specs with depends_on, tests in tests/test_<module>.py; the daemon dispatches, gates, reviews, merges; the Planner intervenes only on held tasks (hold_reason on the bus).
3. Codex returns 2026-09-19 14:00: first goal = one complexity-3 execute task to observe luna/terra/sol routing and the real usage-limit scope; then adjust quota_group semantics.
4. Stale worktrees wt/T-00xx (≈40) remain; removal is a deletion → ask the human once, then remove merged ones.
5. Small debts: challenge-web prompt template; pool_state.json last-writer-wins (lock like bus.lock); executor.md prompt references scripts/tests_green.sh not at the worktree root; record.sh treats backticks in --fact as shell (quote or avoid).
