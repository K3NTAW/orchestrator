# plan.md — Planner checkpoint (updated 2026-09-21 19:40; no open goal. Awaiting the human: PR 18 (orchestrator), kgpt-ios PR 17, kgpt PR 38; account B out of Claude credits)

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

### Progress
- T1 T-0756 merged into goal/T-0755 at 77fb206 (18:12 UTC+2 stamp chain: gate green first attempt, gate_reds 0; security review T-0758 approve, review_reason security_paths:orchestrator/*.py). Diff 4 files +147/-38. Codex's own final full-gate run was red on one new test it then fixed; the external gate is the bar and passed.
- T2 T-0757 became ready on T1's merge and was dispatched to Codex (terra) at 18:13:21. The Planner session restarted at 18:14 (daemon.lock re-created), which killed the MCP server and its codex exec child: the task sat running with pid null, no process, clean worktree at 77fb206. reconcile_dead never fires for pid-null tasks (gotcha 2026-09-19, polish candidate still open). 18:17 Planner ran daemon.reconcile_dead(T-0757) by hand → requeued; in-process daemon redispatches. One background wait armed on merged/held/failed.
- Backlog from this incident (c3, next goal): reconcile_dead treats running tasks with pid null and claimed_at older than the daemon start as dead; codex()/codex_reply() record the codex exec pid on the task.

### Progress (closed)
- T-0757 merged into goal/T-0755 at 160f28c (18:35; gate green, 0 reds; security review T-0759 approve). Retrospective recorded in decisions.md 2026-09-21 (378 lines, compact-memory due). Branch pushed, PR opened 18:45.

### Next step
Human: review and merge the PR goal/T-0755 → main, then git pull the checkout and restart with f orch. No open goal. Backlog (next goal candidates, in order): (c3) reconcile_dead treats pid-null running tasks with claimed_at older than the daemon start as dead and the codex tools record the pid; (c2) planner_runs.decision_points yields closable for long-closed goals; (c2) tests/test_scorecard.py:858 live-bus leak; compact-memory.

## CROSS-REPO GOAL (user 18:50, CLOSED 19:39): highlight the chat that has the active proposal; make accepting proposals work
Diagnosis (Planner grep + Hetzner DB 18:40-18:55, provenance repo/db): the badge counts GET /proposals; the one pending proposal (ops health_watch "Health data has stopped arriving", created 2026-09-19 21:18, expires 2026-09-22 21:18) has turn_id null, so it belongs to no conversation and the iOS app has no UI at all to decide it (no Inbox; ApproveInline only renders inline for tool calls with proposal_id). list_pending() does not return conversation_id, so no client can highlight the chat. ApproveInline swallows errors, never refreshes the badge, cannot answer the 409 requires-reconfirm path.
Route: two repos, each via `orchestrator goal start --account B` with a full brief (scratchpad brief-kgpt.txt, brief-kgpt-ios.txt); zero scouts.
- kgpt T-0014 (c2, pid 52094, log kgpt/.orchestrator/runs/planner-T-0014.log): list_pending LEFT JOIN kernel.turns → conversation_id; tests in kernel/tests/test_agent_loop.py. Scaffold commit 3a5d6b3 on kgpt main (local; revert path git revert 3a5d6b3). kgpt cowork tasks T-0003..T-0011 parked as failed reason "parked" (18:55, reversible: status queued; old plan.md on branch goal/T-0001). kgpt main had no tracked .orchestrator on main, fresh install.
- kgpt-ios T-0006 (c5, pid 52256, log kgpt-ios/.orchestrator/runs/planner-T-0006.log): T1 model+APIClient.approve(reconfirmed)+AppState list/refreshProposals; T2 sidebar highlight + "Needs your decision" section + ApproveInline error/409/refresh. Scaffold commit 0321e34 on kgpt-ios main (local).
- 18:43-18:49 outcome: both headless Planners died with "You're out of usage credits" on account B (kgpt after 17 turns with no child task; kgpt-ios after filing T-0007/T-0008, T-0007 merged). Interactive Planner took over: kgpt-ios daemon --once passes gated and merged T-0008 (0fbc93a), retrospective in kgpt-ios decisions.md, GOAL T-0006 done, PR 17 https://github.com/K3NTAW/kgpt-ios/pull/17. kgpt: Planner filed T-0015 (c2) by bus.create_task; daemon --once cannot dispatch (the worker thread dies with the process; dispatched_at stamp had to be cleared by clear_stage), so a long-lived `orchestrator daemon` runs from this session (background Bash, 10 min cap, re-arm if it expires mid-task). T-0015 running on Codex astra since 18:53.
- Phone: Debug build of goal/T-0006 (0fbc93a) built for generic iOS at scratchpad kgpt-dd2; the iPhone is unavailable over the local network since ~18:50; a background wait installs and launches it as soon as devicectl lists it available.
- kgpt round 1 (T-0015, Codex astra, commits 3084303 d54a7a7): change correct, one assertion compared UUID to str; the worktree gate could not run database tests (.env.test untracked, 419 skips) or the web typecheck (no node_modules). Planner: tests.sh fixed and committed on kgpt main e200247, goal/T-0014 cut from it, T-0015 and the auto fix round T-0016 marked failed/superseded, T-0017 (round 2, worktree preset to wt/T-0015) created 19:10, dispatched to Codex luna 19:13 under a manual `orchestrator daemon` (background Bash, 10 min cap; re-arm if it expires mid-run). One wait armed on T-0017 terminal state.
- kgpt round 2 (T-0017, Codex luna, 203e954 after rebase onto goal/T-0014): assertion fixed; daemon held it as gate_red with "acceptance tests missing" because acceptance.py _TEST_ID matches `tests/...py::name` inside `kernel/tests/...`, resolving to wt/tests/ (BACKLOG c2 for this repo: honour a directory prefix before tests/). External gate red for a real reason: ruff lints the committed .claude scaffold (138 errors; main lacks the exclusion that goal/T-0001 33c1afb has). Round 3 T-0019 (19:30, same worktree) adds the ruff extend-exclude; acceptance phrased without path::name ids. Auto fix rounds T-0016/T-0018 retired as failed/superseded.
- kgpt round 3 (T-0019, Codex sol, e268a30): ruff extend-exclude for the scaffold; gate green, 1014 passed with database tests running; merged into goal/T-0014 19:37. Retrospective in kgpt decisions.md, GOAL T-0014 done, PR 38 https://github.com/K3NTAW/kgpt/pull/38 opened 19:39. CROSS-REPO GOAL CLOSED.
- Phone: user is away from home; the phone cannot be reached tonight. Install later from scratchpad kgpt-dd2 (or rebuild from the merged main) with devicectl when the phone is on the network.
- Escalation for the human: account B has no Claude usage credits left (billing); account A is over its daily planner budget. Headless Planners cannot run until credits return; Codex executors are unaffected.
- After both PRs merge: human deploys kgpt (ghcr image on Hetzner) for the highlight field; Planner builds kgpt-ios onto the phone again (xcodebuild Debug + devicectl, as at 18:33). One background wait armed on both planners exiting.

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

## Auto-handover 2026-09-21T18:34:05+02:00 — daemon tick

[planner].handover_context_tokens is the configured handover threshold.
Open goals: none

Worktrees: wt/T-0002, wt/T-0006, wt/T-0007, wt/T-0008, wt/T-0009, wt/T-0010, wt/T-0012, wt/T-0013, wt/T-0014, wt/T-0016, wt/T-0018, wt/T-0021, wt/T-0022, wt/T-0023, wt/T-0026, … and 353 more

Last events:
- 2026-09-21T18:32:27+02:00 T-0757 update {"pipeline": {"first_ready_at": 1790007184.3929868, "dispatched_at": 1790007469.
- 2026-09-21T18:32:28+02:00 T-0755 update {"pipeline": {"last_merge": {"status": "merged", "target": "goal/T-0755", "sha":
- 2026-09-21T18:32:28+02:00 T-0755 update {"status": "done", "result": {"goal_closed": true, "pr_url": null, "summary": "G
- 2026-09-21T18:32:28+02:00 T-0755 update {"pipeline": {"last_merge": {"status": "merged", "target": "goal/T-0755", "sha":
- 2026-09-21T18:32:28+02:00 T-0755 update {"pipeline": {"last_merge": {"status": "merged", "target": "goal/T-0755", "sha":

Resume: skill resume; re-spawn held spec reviews; dispatch ready execute tasks by hand while Codex cools.
