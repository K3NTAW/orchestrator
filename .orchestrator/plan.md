# plan.md — Planner checkpoint (2026-09-18 01:00)

## Goal C — T-0043 parallel machine (complexity 7)
User words: "do those" = split tests per module + shared harness; task dependencies + daemon-driven gate→review→merge→dispatch; pre-execution spec review at complexity ≥5. "In the end we have a parallel working machine."
Branch goal/T-0043 off main 046a8f8 (PR 4 merged). First commit: daily budgets 30M/40M.
Also asked: restart the orchestrator MCP server. It is a child of the Planner session (two stale process pairs seen: 39220/39221, 88704/88706). A Planner cannot restart its own MCP servers safely; the restart happens when the human starts a fresh `f orch` session. That new session resumes this goal from here (skill resume).

## Scouts (account B/A, sonnet, output cap 4,500 chars)
- C1 tests: map the 16 classes in tests/test_orchestrator.py (lines), which module each covers, shared fixtures (TMP, REPO, hook(), FakeProc, env setup at import), import-order hazards (ORCH_ROOT must be set before importing orchestrator), and propose tests/_harness.py + per-module files; how tests-green.sh / unittest discover pick them up; which classes must stay together.
- C2 daemon/bus: daemon.tick today (requeue dead pids, notifications), bus task schema (no depends_on), executor.start/spawn.run_worker entry points, merge.merge, spawn_review; propose the state machine for the daemon: queued(depends_on all merged? & spec_review approved?) → dispatch (codex()) → done → gate (tests-green wt) → review task (≥4) → approve → merge → dependents; failure/request_changes → held + notify. Where each hook lives; what must be idempotent; lock needs (merge.lock exists).
- C3 spec review: prompts/review.md + spawn.run_worker review path; propose spec-review role (bus ROLES has no spec_review: add role or reuse review with inputs[0]={spec,...}); prompt template; where the hold lives (constraints or status held with hold_reason spec_review); test approach.
Challenge <0.7 findings acted on. Synthesize → specs with DISJOINT scopes so C tasks run in parallel:
- C-A tests split (tests/**) — must land FIRST (everything else adds tests to per-module files).
- C-B depends_on in bus.create_task + bus_mcp (bus.py, bus_mcp.py, tests/test_bus.py).
- C-C daemon advancement (daemon.py, tests/test_daemon.py) — depends on C-B.
- C-D spec review role + prompt + spawn path + mcp tool (spawn.py, mcp.py, prompts/spec-review.md, tests/test_spawn.py).
- C-E docs (README, CLAUDE.md orchestrate skill step 6/7 update).
Review: sonnet other account for ≥4; daemon (C-C) gets adversarial + security checklist (it dispatches and merges autonomously).

## Rollback
Each task's commit names its revert; goal branch PR to main is the human gate.

## Specs on the bus (01:20)
T-0047 C-A tests split (4) → T-0048 C-B depends_on (3) ∥ T-0049 C-D spec_review (4) → T-0050 C-C daemon (7; opus fallback; review adversarial + security) → T-0051 C-E docs (2). Reviews: sonnet other account for ≥4; pre-create review worktrees off the task branch (server still old).
Dispatched: T-0047 (wt/T-0047 off goal/T-0043).

## Next step
spawn scouts C1–C3 → read → synthesize → write specs → dispatch C-A first (fallback sonnet), then C-B and C-D in parallel, C-C after C-B, C-E last → PR goal/T-0043.
