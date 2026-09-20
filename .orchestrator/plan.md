# plan.md — Planner checkpoint (updated 2026-09-21; OPEN GOAL T-0503 Adaptive Parallelism S1 on goal/T-0503 cut from main)

## OPEN 2026-09-21 — GOAL T-0503 Adaptive Parallelism S1 (brief priorities 0-4), complexity 9, route: high-risk/architectural
Brief: .orchestrator/adaptive-parallelism-program.md (user text 2026-09-21, committed on main so the goal branch carries it). Goal branch goal/T-0503 cut from main after the brief commit; PR 15 will target main. Baseline sched-pre saved from live data before any change (.orchestrator/baselines/sched-pre.json). Scouts: none; every fact below is Planner grep, verified in source.

Verified dispatch facts (main, 2026-09-21):
- daemon.dispatch (daemon.py:591-689) walks bus.read(status=queued, role=execute) in id order plus budget-held retries; bus.ready (bus.py:140-150) = every depends_on merged (or done for non-execute); dispatches first-come until free_slots (daemon.py:449-464: idle Codex slots = sum(max_parallel - running) over enabled non-cooling execute executors, 4 executors x 2 = 8; in fallback mode limits.max_parallel_claude_workers 4 minus running and in-flight Claude workers). No interference, priority or capacity reasoning; a task below SPEC_REVIEW_MIN with no slot breaks the loop; reviews and spec reviews respawn from a second loop with review_slots. tick order (daemon.py:1203-1250): tally_planner, sweep_leases, reconcile_dead, dispatch, gate, merge_reviewed, auto_fix_round, planner_runs, notifications, maybe_handover.
- Pipeline stamps: created_at (task field), pipeline.dispatched_at/_done, claimed_at, gated_at, first_green_at, gate_attempts, gate_reds, lineage_fix_rounds, review_held_at, reviews_expected, reviewed_sha, merged_at, accepted_at, last_merge, failure_kind, resume. Run rows (runs/<date>.jsonl via bus.log_run bus.py:278) carry ts, duration_s, role, task, tier, account, executor, complexity, outcome, tokens; concurrency is derivable post hoc from claimed_at..gated_at intervals and run rows.
- merge.merge (merge.py:25-60): serial, rebase onto target, tests-green, fast-forward; a conflict marks the task failed reason=rebase_conflict with resume_hint {conflicts, hunks}. Phase H paid three rebase tasks (T-0388, T-0419, T-0431) for parallel chains on daemon.py: real interference cost.
- Reviews: _open_reviews (daemon.py:832-876) already spawns all missing reviews concurrently; complementary roles exist behind [review].complementary=false (P6b); review overlap is measured by scorecard --reviews (P6a). Brief P9/P10 are therefore mostly live; only measurement remains.
- Graph: graphify-out/graph.json (node-link JSON: nodes with source_file, source_location, label; edges under "links"; built_at_commit) built 2026-09-17, so stale against main; graph.sh update refreshes it. Graph use must fail back when absent or stale.
- Security: [review].security_paths covers orchestrator/*.py, pool.toml, tests are not listed; every task here gets one sonnet security review (never the executing model); complexity >= 6 gets a spec review; >= 7 two reviews.
- Jev: jev_route.py shadow classification pattern (mode off|shadow|active, runs/jev rows, routing_eval) is the template for a later P15 scheduling signal; not in this goal.

Decomposition (7 atomic tasks; daemon.py touches serialized as one dependency chain, the pure modules run in parallel — the same interference rule the goal implements):
- S0a dispatch-skip telemetry and readiness stamps (daemon.py, bus.py) c4
- S0b scorecard --parallelism, baseline block (scorecard.py, baseline.py, cli.py) c5, parallel with S0a under an explicit row contract
- S1 orchestrator/interference.py deterministic none/soft/hard + select_wave (pure module) c5, parallel
- S2 execution waves in dispatch behind [scheduler].mode = shadow|active|off (daemon.py, pool.toml) c7, after S0a and S1
- S3 orchestrator/critical_path.py ranking wired as the wave order (critical_path.py, daemon.py order hook) c6, after S2
- S4a graph-aware interference signal with freshness fallback (interference.py) c5, after S1
- S4b stale-work detection before review, optional rebase behind [scheduler].stale_rebase (daemon.py, gitutil.py) c6, after S3
Waves: 1 {S0a, S0b, S1} -> 2 {S2, S4a} -> 3 {S3} -> 4 {S4b}. Default mode shadow: nothing changes dispatch until the next goal compares shadow rows against baseline and flips active.

Interface contracts (binding for every task):
- pipeline.first_ready_at: epoch float, stamped once by dispatch when bus.ready first holds for a queued execute task.
- runs/sched/dispatch.jsonl (S0a writes, S0b reads): one row per daemon tick that considered at least one queued execute task: {"ts", "free_slots", "fallback", "running_execute", "running_claude", "considered": [{"task", "goal_id", "ready", "action": "dispatched"|"spec_review"|"skipped", "reason": "dependency"|"executor_capacity"|"budget"|"cooldown"|"account_capacity"|"spec_review_pending"|"spec_review_changes"|"fallback_no_tier"|"stale"|"predicted_interference"|"merge_pressure"|"other", "executor"}]}.
- runs/sched/waves.jsonl (S2 writes, S0b tolerates absence): {"ts", "mode", "ready", "running", "baseline_order", "wave", "deferred": [{"task", "reason"}], "predicted": [{"a", "b", "level", "reasons"}], "priority": {id: explain} (S3), "applied": bool}.
- runs/sched/stale.jsonl (S4b writes): {"ts", "task", "goal_id", "base", "goal_head", "moved_count", "stale_paths", "risk": "none"|"low"|"high", "action": "recorded"|"rebased"|"rebase_conflict"}; pipeline.stale_check mirrors the row.
- interference.classify(t1, t2, tasks=None, graph=None, rules=None) -> {"level", "reasons", "score"}; interference.select_wave(ready, running, tasks, capacity, order=None, graph=None, rules=None) -> {"wave", "deferred"}; interference.graph_signal(t1, t2, graph) -> list of reasons (S4a fills it; S1 ships it returning [] with graph None).
- critical_path.rank(ready_ids, tasks, durations=None) -> ordered ids; critical_path.explain(task_id, tasks, durations=None) -> {"downstream_depth", "blocked_descendants", "est_duration_s", "priority"}.
- [scheduler] pool.toml table (S2 adds, S3/S4a/S4b extend): mode = "shadow", soft_conflict_policy = "defer", max_wave = 0, graph_max_commits_behind = 50, stale_rebase = false.

Next step: create the seven execute tasks (skill write-spec), let the daemon dispatch wave 1, intervene only on holds. After S4b merges: external gate on goal/T-0503, baseline save sched-s1-code, compare with sched-pre, retrospective, PR 15.

## Read first (next session)
1. `bus_read(status_not="done")` then skill `resume`; the open goal is T-0503 (this file's header). Older leftovers are superseded or failed tasks.
2. Planner commits from the main checkout (branch main): stage only .orchestrator/ paths; commit with `git commit -F <scratchpad file>`; guardrails block pushing main, so the goal branch (cut from main) carries Planner commits into its PR. planner-mode.sh scans the whole command text: keep backticks, `>`, `<`, and the words it treats as writes (cp, rm, mv, install, touch, tail with a pipe, git rebase/checkout/reset/revert, .env) out of Bash text. Use absolute paths. macOS has no `timeout`.
3. Tool contracts: `spawn_scout(<scout id>)`, `spawn_review(<review id>)`, `spawn_spec_review(<spec_review id>)`; `merge(task_id, target)` skips the review policy; `codex` and `codex_reply` return the result dict and now post it (H5a). Specs are immutable: a fix round is a new task with constraints.fix_round_for.
4. Session restart kills the in-process daemon and its posting threads; check pgrep for codex exec and claude -p before restarting.
5. recall.sh needs `uv run python .claude/skills/memory/scripts/recall.py` (system python lacks tomllib).

## Human items outstanding
- Docs repo: merge PR 1 then PR 2 (docs-kentawaibel).
- Decisions: latency target, security_paths width, daemon as a process; review budget; [limits].reservations after the first production goal; [planner.routes].auto_open_pr stays false; when to flip [scheduler].mode to active (after this goal's shadow rows are compared).

## Backlog (c2-c3)
- cache_max_entries = 0 guard in jev_route; --economics/--efficiency flag conflict message in cli; consistent total_tokens for pre-G1a rows; recall ranking relevance; recall.sh python shim.
- Brief priorities 5-14 as later goals: duration/capacity-aware scheduling, scout objective reuse, review-overlap follow-up, merge pressure, adaptive concurrency, scheduling scorecard, Jev shadow signal, critical-path model allocation, workflow learning, speculative execution.
- kgpt side of the orchestrator module (C-K1..C-K4) on the kgpt bus.

## Architecture (decided)
Phase C: two processes, kgpt `modules/orchestrator` thin MCP module forwarding to `orchestrator serve` per repo (bearer per endpoint, per-slug locks). Phase D: the daemon owns the loop; the Planner is a function of decision points (planner_runs.py), each launch a fresh headless `claude -p` with a compact decision packet. Phase H/I: routes, reservations, acceptance-test gate, failure kinds, packets, Jev shadow routing, review telemetry — all live on main since 2026-09-21.

## Prior art (ids for recall)
- Retrospectives: decisions.md 2026-09-18 to 2026-09-21 (goals T-0073, T-0109, T-0240, T-0260, T-0353, T-0445, T-0489, release T-0499). Gotchas 2026-09-18 to 2026-09-20 in gotchas.md (dispatch loop break, free_slots, rebased fix rounds, dead reviews).
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt, home `ssh k3ntaw@192.168.1.167` (fish shell), kgpt home side /opt/kgpt-home.
- docs.kentawaibel.com: repo /Users/k3ntaw/code/docs-kentawaibel, Vercel project docs-kentawaibel, PRs 1 and 2 open.
