# Phase F review: Jev in production (goal T-0240, branch goal/T-0240)

Written 2026-09-19 by the Planner (Fable 5.1) for the human reviewer; final revision 20:25. Status: F1 to F7 merged on goal/T-0240, head fe7026a, 10 commits over Phase E head df08523. PR 10 (goal/T-0240 to main) is open for you after PRs 6 to 9. This document is for thinking about architecture changes; the operational checkpoint stays in plan.md.

## 1. What shipped

| Task | Change | Commit | Executor | Review |
|---|---|---|---|---|
| T-0241 F3 | daemon._dispatch_worker posts done/failed bus results from executor.start(); a raising start marks the task failed | df9074f | Codex astra | T-0244 sonnet approve (security_paths) |
| T-0245 F3 fix | test harness runs spawn_async synchronously; the non-blocking test joins its threads (flaky KeyError in merge run) | fd2e078 | Codex sol | none (tests-only diff, review_reason none) |
| T-0242 F1 | jev-gate.sh makes one interpreter call; jev_gate exits before importing urllib when disabled; per-process [jev] cache; startup_ms logged; skip list for empty inputs and bare Glob | b07a5ff | Codex sol (astra thread aborted) | T-0246 sonnet approve, low note |
| T-0243 F2 | jev.py keeps confidence None, adds [jev].votes averaging; block rule with four pool.toml thresholds (0.85/0.15 with confidence, 0.92/0.08 without); rule conf/noconf/none logged; README sentence | ea492c5 | Codex astra | T-0247 sonnet approve |
| T-0250 F6 | merge_reviewed holds a task whose merge returns non-merged (hold_reason "merge tests_red"), clears merged_at for tests_red, keeps it for conflict; planner_runs test proves held tasks are decision points | 1d5217e | Codex sol | T-0253 sonnet approve |
| T-0248 F4 + T-0252 fix | scorecard --by task shows calls/waste_pct/blocked/turns, --by goal shows calls/waste_pct/turns; old snapshot test updated | cbd5a6a, f408edb | Codex astra, sol | T-0255 sonnet approve, low note |
| T-0249 F5 + T-0254 + T-0258 | executor.argv_for(kind): codex exec resume without -C (cwd instead), -s/--sandbox translated to --config sandbox_mode; usage errors do not consume a round and are logged; T-0258 only rebased the branch onto F7 so its gate stopped hitting the sqlite flake | d3f452f, fe7026a | Codex sol, astra | T-0251 request_changes (sandbox flag), then T-0259 approve |
| T-0256 F7 | bus.db() now returns one cached sqlite connection per state path with explicit close (0 of 7 call sites changed); test_handover counts only handover lines; unclosed-database warnings 122 to 0 | 35c0462 | Codex astra | T-0257 sonnet approve, low note (atexit close without the lock) |

Diff df08523..fe7026a: 10 commits, 16 files, about +700/-45. Goal cost 3.44 USD, all of it nine sonnet security reviews; eleven Codex runs (astra 5, sol 6) at zero Claude cost.

## 2. How the run actually went

The session restart at 18:16 killed the MCP server. That server hosts the daemon as an in-process thread (daemon.autostart) and the dispatch threads that run codex exec, so both Codex turns (T-0241, T-0242) were aborted mid-turn: no thread id, no run row, uncommitted edits in one worktree. T-0241 had committed seconds earlier. T-0242 was resumed with a fresh codex() thread on the same worktree and finished from its diff.

codex_reply (fix rounds on an existing thread) has been broken since Codex came back: the installed CLI rejects -C on codex exec resume, and later also -s. F5 fixes the argv; every fix round in this phase therefore ran as a fresh thread with the diff described in the prompt.

The daemon stamps merged_at before merging. T-0241's merge went red on a flaky test after a green worktree gate and an approved review; the task ended failed with merged_at set and nothing retried it. F6 turns that into a hold for the Planner.

Every Codex result in this phase was posted by hand: tests-green on the worktree, then bus_post_result with executed_by codex:<executor>. F3 fixes this, but the running server loads the goal/T-0201 checkout, so the fix is not live until a restart on goal/T-0240 code. Goal acceptance 3 (a Codex task reaches done without Planner help) is therefore unverified.

Two flaky tests cost about an hour: leaked daemon threads writing into the next test's bus (fixed by T-0245), and unclosed sqlite connections whose ResourceWarnings land in a test that counts stderr lines (F7). Both only show in a full-suite run; single tests pass. The sqlite one failed the same branch three times in the daemon gate and once in the merge queue before F7 landed, and planner-mode.sh blocks git rebase, so the F5 branch was rebased by a Codex task (T-0258). Two scope misses needed fix rounds: T-0248 could not touch tests/test_scorecard.py, T-0249 missed the sandbox flag.

Reviews earned their cost once in nine: T-0251 caught that resume also rejects -s, which only worked because pool.toml sets dangerous_full_access = true. The other eight approved with at most a low note.

## 3. Measurements

Gate latency (runs/jev/gate.jsonl, tasks of this goal): 26 rows, median 667 ms, max 830 ms. Post-F1 rows show startup_ms 63 to 80, so about 590 ms is the Jev network round trip. Goal acceptance 2 (median under 400 ms) fails on the network, not the hook.

Waste per worker (p_needed under 0.3 or p_redundant over 0.7): review T-0244 9 calls 77.8 percent, review T-0246 8 calls 62.5 percent, execute T-0242 4 calls 0 percent. Reviews re-read files the diff already shows. Block mode has not fired anywhere (gate_mode = log; confidence still null on every noul answer).

Phase E verification: (a) per-worker gate rows: confirmed, every Claude worker under the restarted server wrote rows; (b) a non-security diff merges with zero reviews: confirmed on T-0245 (review_reason none); (c) scorecard shows waste_pct and turns: false until F4, the columns existed only in --json.

Cost trend by goal: T-0073 Phase C 62.59 USD (45 percent review), T-0109 Phase D 33.48 (44 percent), T-0201 Phase E 38.93 (14.6 percent), T-0240 Phase F 2.78 (100 percent review, Codex executed everything).

## 4. Architecture as it stands

- Planner: an interactive Claude session (Fable) with planner-mode.sh blocking writes outside .orchestrator/. It plans, specs, posts results, repairs bus state and commits .orchestrator. The Phase D decision-point Planner (headless claude -p per held task) exists but never runs while an interactive session is registered.
- MCP servers: orchestrator.mcp (codex, codex_reply, merge, spawn_*, status) and bus_mcp, both started by the Claude session from the main checkout. orchestrator.mcp autostarts the daemon thread and holds daemon.lock.
- Daemon: one tick loop: dispatch ready execute tasks (spec review from complexity 6), gate done tasks with tests-green.sh, review policy (code_review = security_paths, one sonnet review when the diff touches orchestrator/*.py or the other globs), serial merge into goal/<parent>, auto-handover to plan.md every 15 minutes, shadow Jev triage of decision points.
- Bus: JSON file per task under .orchestrator/tasks plus a sqlite mirror for tasks and events, flock-serialised. Specs, scope and depends_on are immutable; state changes go through bus.update.
- Executors: Codex CLI threads (astra, sol, luna, terra enabled; scorecard picks) in per-task git worktrees under wt/; Claude sonnet/opus fallback when Codex cools. Codex runs log tokens but no usd.
- Jev (TypeSafe System One): PreToolUse hook scoring every worker tool call (needed/redundant/destructive) in log mode; jev_rank for recall and handover pruning; shadow triage. Egress is task specs, tool-call metadata and memory titles, never file contents.
- Memory: decisions.md, gotchas.md, model-notes.md, architecture.md under .orchestrator/memory with record.sh; plan.md as the restart checkpoint.

## 5. Weak points seen this phase, with options

1. Daemon lifetime is tied to the Planner session. A restart, a handover or an MCP reconnect kills dispatch threads and their codex exec children. Options: run the daemon as its own process (launchd agent or `orchestrator daemon` under a supervisor) and make orchestrator.mcp a thin client; or keep autostart but hand codex exec to a detached subprocess group with the thread id written at thread.started so a reconciler can adopt orphans from ~/.codex/sessions rollouts.
2. Result flow depends on the daemon thread. F3 posts from the thread; a crash between run end and post still loses the result. Option: the executor writes a result file in the worktree (.orchestrator-result.json) that the gate reads, so the bus can be reconstructed from the worktree alone.
3. security_paths = orchestrator/*.py reviews every task. Seven reviews for eight tasks; one was worth it. Options: narrow to the mutation surface (executor.py, merge.py, daemon.py merge path, mcp.py, hooks, prompts) and let tests-green carry the rest; or a diff-size trigger (review when a diff touches more than N lines of orchestrator/).
4. Jev latency is the network. 590 ms per gated call is fixed cost; startup is 80 ms. Options: score asynchronously in log mode (hook returns immediately, scoring appended later) and only block synchronously for destructive tool kinds; cache verdicts per (tool, normalised input) within a run; widen the skip list from the waste data once a week of rows exists; ask TypeSafe about regional endpoints.
5. Test suite isolation. Both flaky failures were cross-test state in one process. Options: run the gate with per-module processes (unittest per file, or pytest with -p xdist --dist loadfile), enable -W error::ResourceWarning in the gate so leaks fail loudly and locally, and keep spawn_async synchronous in tests by default.
6. Codex CLI drift. The resume argv broke silently on an upgrade. Options: probe `codex exec resume --help` at server start and record the accepted flags in status(); pin the CLI version in the executor image; treat rc 2 usage errors as a cooldown-free hold with a notification.
7. Hand repairs are still frequent. This session the Planner cleared holds, re-stamped merged_into on superseded tasks, re-gated a task and posted eight results by hand. Options: a `orchestrator repair` command family (post-from-worktree, mark-merged-via, regate) so the repairs are one call and logged; a fix-round helper that creates the task, sets fix_round_for and copies scope plus the extra files.
8. Worktree hygiene. wt/ holds over 100 directories; the daemon lists 92 of them in every auto-handover. Option: prune worktrees whose branch is an ancestor of its goal branch and older than a day, on a daily tick.
9. Scope immutability plus 5-file cap. Both scope misses this phase came from spec time guesses about which test files pin old behaviour. Option: the executor may edit tests/* that fail solely because of its own change if it lists them in the result, with the gate diffing against scope after the fact; or a pre-dispatch scout that greps tests for the touched CLI/API strings.

## 6. Decisions the human should make

- Latency target: accept about 700 ms per gated call in log mode, or fund option 4 (async scoring). The 400 ms criterion in T-0240 is not reachable as written.
- security_paths width for this repo (option 3). Current cost per review is 0.4 to 0.6 USD.
- Daemon as a separate process (option 1), before the next long goal.
- Restart the Planner session on the goal/T-0240 checkout so F3, F5 and F6 are live, then run one tiny Codex task to verify acceptance 3.
- Merge order for PRs 6, 7, 8, 9, then 10.

## 7. Backlog left from this phase (spec when asked, c2 each)

README note that block mode should follow a week of log-mode rows (F2 left it out); jev_gate.main() wraps P.config in cache() on every call with no reset (T-0246 note); cli._scorecard_measurement_totals recomputes by_task per goal render (T-0255 note); daemon --once and session restarts kill dispatch threads (document or fix per option 1); tests/test_handover count assertion hardened by F7; worktree pruning.
