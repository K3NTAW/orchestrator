# plan.md — Planner checkpoint (updated 2026-09-21 18:30; GOAL T-0755 R22 open: render errors visible + review respawn retry, run under [planner].autonomous = true shadow)

## GOAL (user 18:25 "flip autonomous to true and run one goal"): R22 — spawn.render validates placeholders against the template, dispatch and run_worker hold on render errors, review respawn retries and caps
Complexity 5 (two files in orchestrator/, both `[review]` security paths → one security review each on security_review_tier; below spec_review_min 6, so no spec review). Route: clear localized change → two atomic specs, T2 depends_on T1 (both edit daemon.py and tests/test_daemon.py), deterministic gate, human PR. Zero scouts: all facts below are Planner grep, provenance repo.
[planner].autonomous flipped to true in .orchestrator/pool.toml at 18:30 (comment records the date; revert path: set it back to false). 18:40 user: "planner" added to account B role_affinity so pool.pick("planner") has headroom while A is over budget (`pick planner --model` now answers B, fable); revert path: remove the word from the list.

### Facts (grep 18:10-18:25)
- spawn.render (orchestrator/spawn.py:101-113) substitutes each kw sequentially with str.replace, then raises unfilled_placeholder on ANY remaining double-brace token in the rendered text. Consequences (gotchas 2026-09-21 ×3): a spec or diff that quotes a placeholder kills render; a value containing another key's token gets substituted twice.
- Call sites: daemon.dispatch execute path daemon.py:852-856 renders in the tick thread before spawn_async and before complete(dispatched_at), so the exception escapes dispatch and the task is re-stamped every lease with no event; daemon._dispatch_fresh_fix daemon.py:554-563 (called from 585, inside a worker thread); spawn.run_worker spawn.py:757-779 for review/challenge/spec_review/execute-fallback/scout (thread dies silently after the account was picked but before claim).
- Holding helper exists: daemon.hold_failed(tid, error_key, stage_label, exc) daemon.py:484-492 (status held, hold_reason "<stage> failed: <ExcType>", pipeline[error_key], notify). auto_fix_round (daemon.py:141-) iterates every held execute task and may create fix rounds; a render error is a spec defect the Planner must fix, never a fix round.
- Review respawn (daemon.py:922-962): a queued review/spec_review is respawned once (pipeline.respawned_at); `if respawned_at > requeued_at: continue` blocks any further retry forever, so a respawn whose worker died (render error, crash) leaves the task queued with no worker and no visible reason (gotcha 2026-09-21 T-0686/T-0712). respawn_after_s from [daemon] (default 120).
- Tests: tests/test_spawn.py (79 tests; test_render_flags_unfilled_placeholder:85 must stay green: missing packet key still raises "unfilled_placeholder: packet"), tests/test_daemon.py (204; test_dispatch_respawns_requeued_review_once:982 calls dispatch twice immediately and expects one spawn; must stay green). Harness: tests/_harness.py single ORCH_ROOT, unittest.TestCase.

### Evidence caveat for the autonomous run (why this goal may still produce only skip rows)
- planner_runs._session_attached() (planner_runs.py:212-232) is true while this interactive session lives (ORCH_DAEMON_HOST=mcp in the MCP server hosting the daemon; .orchestrator/planner_session.json pid 53524). run_group skips BEFORE routing and shadow launch with skip reason planner_session_attached (planner_runs.py:1279-1286). Headless launches, router decisions and Opus shadows only fire once this session ends and a standalone `uv run orchestrator daemon` runs the remaining decision points.
- pool.pick("planner") (pool.py:404-422) needs role planner in role_affinity: only account A has it, and A is at 67.1M of 30M daily tokens → skip no_account_headroom until the daily reset, or until the human adds "planner" to account B's role_affinity in pool.toml.
- Plan: file both specs now, let the in-process daemon execute, gate, security-review and merge into goal/T-0755; leave the closable point (retrospective + PR) and any held point to the headless Planner after the human exits this session and runs the standalone daemon. Skip rows in runs/sched/planner_skips.jsonl are themselves the first telemetry.

### Observed under autonomous = true (18:30-18:35)
- planner_skips.jsonl grows six rows per tick: planner_runs.decision_points (planner_runs.py:168-211) yields closable for six long-closed goals (T-0004, T-0065, T-0240, T-0260, T-0353, T-0489) because it never checks the goal task's own status; decision.route eliminates them (routine or unknown_gate) so no launch, but it is noise. Backlog: closable only for goals whose triage task is not done/failed.

### Decomposition
- T1 c5 execute: spawn.render validates against the template token set, single-pass substitution; dispatch, _dispatch_fresh_fix and run_worker hold with hold_reason render_error and notify; auto_fix_round skips render_error holds. Scope orchestrator/spawn.py, orchestrator/daemon.py, tests/test_spawn.py, tests/test_daemon.py.
- T2 c3 execute (depends_on T1): review respawn retries every respawn_after_s while queued and unassigned, counts pipeline.respawn_count, holds with hold_reason respawn_exhausted at [daemon].respawn_max (default 3). Scope orchestrator/daemon.py, tests/test_daemon.py, .orchestrator/pool.toml.
- Then: gate on goal/T-0755, retrospective (record.sh), PR goal/T-0755 → main. Read `scorecard --planner-routing`, `promotion`, and runs/sched/planner_skips.jsonl.

### Next step
Filed 18:28: GOAL T-0755, T1 T-0756 (c5, running on Codex since 18:28), T2 T-0757 (c3, depends_on T-0756). Note: pool.toml [daemon] has no respawn_after_s line today; T2 may add it alongside respawn_max, fine. Daemon (in-process, autostart) dispatches. Wait with one Monitor on T1 merged; then T2. Human PR at the end.

## History (closed)
- GOAL T-0674 Adaptive Planner Routing: closed 17:55, PR 17 merged into main 545fd12 at 18:00, local main = origin/main 363942a. Details: decisions.md 2026-09-21. Backlog still open after this goal: tests/test_scorecard.py:858 live-bus leak (c2); compact-memory (decisions.md and gotchas.md over 300 lines); rebase_changed_diff review respawn.
- GOAL T-0561 roadmap completion: PR 16 merged 15:35. Earlier goals: decisions.md 2026-09-18 to 2026-09-21.

## Session rules (carry forward)
- Spec-writing: acceptance names tests as tests/test_<module>.py::test_name, unittest.TestCase methods; create a dependent task only after its parent's spec review approves (depends_on immutable); read every finished review.
- Planner commits from main checkout: stage only .orchestrator/ paths; `git commit -F <scratchpad file>`; guardrails block pushing main and ~/.config/f; planner-mode scans Bash text (no backticks, `<`, `>`, cp/rm/mv/install/touch, tail pipes, git rebase/checkout/reset/revert, .env).
- Tool contracts: codex(task_id, prompt) dispatches directly; codex_reply(task_id, delta) resumes; merge(task_id) after gate + approve only; recall via `uv run python .claude/skills/memory/scripts/recall.py` (recall.sh picks a python without tomllib).
- Prompt-template edits: Planner edits .orchestrator/prompts directly and commits on main before the goal branch is cut; never through a reviewed Codex diff.
- Session restart kills the in-process daemon; check pgrep for codex exec and claude -p first. Worktrees base on goal/<parent> when that branch exists, else origin/main.

## Prior art (ids for recall)
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt, home `ssh k3ntaw@192.168.1.167` (fish), kgpt home side /opt/kgpt-home. docs.kentawaibel.com: repo /Users/k3ntaw/code/docs-kentawaibel, Vercel project docs-kentawaibel.

## Auto-handover 2026-09-21T18:03:36+02:00 — daemon tick

[planner].handover_context_tokens is the configured handover threshold.
### T-0755 GOAL: R22 — render errors become visible holds, spawn.render validates placeholders against the template, review respawns retry and cap
- queued: T-0757 T2: daemon re-respawns a queued unassigned review after respawn_after_s, counts respawns and holds with respawn_exhausted at [daemon].respawn_max (depends_on=['T-0756'])
- running: T-0756 T1: spawn.render validates placeholders against the template with single-pass substitution; dispatch, fresh-fix dispatch and run_worker hold on render errors with hold_reason render_error (executor=sol, started=2026-09-21T18:01:40+02:00)

Worktrees: wt/T-0002, wt/T-0006, wt/T-0007, wt/T-0008, wt/T-0009, wt/T-0010, wt/T-0012, wt/T-0013, wt/T-0014, wt/T-0016, wt/T-0018, wt/T-0021, wt/T-0022, wt/T-0023, wt/T-0026, … and 352 more

Last events:
- 2026-09-21T18:01:26+02:00 T-0756 update {"pipeline": {"first_ready_at": 1790006483.880789, "dispatched_at": 1790006483.8
- 2026-09-21T18:01:40+02:00 T-0756 update {"packet_meta": {"chars": 3613, "est_tokens": 903, "hash": "702842a4ba6e", "vers
- 2026-09-21T18:01:40+02:00 T-0756 update {"status": "running", "assigned_to": "codex", "worktree": "/Users/k3ntaw/code/or
- 2026-09-21T18:01:40+02:00 T-0756 update {"rounds": 0, "executor": "sol", "tier": "sol"}
- 2026-09-21T18:01:48+02:00 T-0757 created {}

Resume: skill resume; re-spawn held spec reviews; dispatch ready execute tasks by hand while Codex cools.
