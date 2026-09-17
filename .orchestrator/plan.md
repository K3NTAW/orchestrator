# plan.md — Planner checkpoint
(empty: no goal in progress. The Planner overwrites this after every fan-out and merge.)

## Last goal (closed 2026-09-18 00:40): T-0005 multi-model executor pool
goal/T-0005 = e794cf4, PR 4 (ready for review). 22 execute tasks, 14+ merges, all Claude fallback (Codex cooling until 2026-09-19 14:00). decisions.md 2026-09-17 entries carry shas and rollback.
Open for the human: daily_budget_tokens (A 6M, B 8M) sit below the 10M window; suggest 30M/40M. Stale worktrees wt/T-00xx remain (removal = deletion, needs OK). MCP server still runs pre-B6 spawn.py until restarted (base_for inactive; Planner pre-creates worktrees meanwhile).
First thing when Codex returns: one small execute task at complexity 3 to see luna/terra/sol routing and the real usage-limit scope.
