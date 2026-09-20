# plan.md — Planner checkpoint (updated 2026-09-20 12:50, session 5; open goal: T-0353 Phase H efficiency handover)

## State in one paragraph
Open goal: T-0353 Phase H, complexity 8: implement /Users/k3ntaw/Documents/orchestrator-efficiency-handover.md on top of goal/T-0260 (Phase G, PR 11 unmerged). Goal branch goal/T-0353 cut from goal/T-0260 head 2ca1517 at 12:48; a read-only checkout of that head sits at wt/goal-T-0260 for scouts (the running server is still Phase F code on goal/T-0240, so scout worktrees are stale and every spec says READ wt/goal-T-0260). Two sonnet scouts on account B are running the gap matrices: T-0354 (handover sections 2-5) and T-0355 (sections 6-11). Next: summarize both once into the matrix below, decide the core changes, write atomic specs with parent T-0353 (the daemon dispatches them into goal/T-0353), then PR 12 goal/T-0353 to main. Phase G closed 2026-09-19 23:56 (PR 11). docs.kentawaibel.com is live (see below).

## Phase H: premium usage evidence (measured, 2026-09-20 12:45)
- Interactive Planner (Fable, account A): planner_day_tokens 35.2M on 2026-09-19 (status() at 23:24), over the 30M daily budget; this session 5 alone ran Phase G's tail, the docs goal and now Phase H. Zero headless decision runs recorded in this root (no .orchestrator/planner_runs.json). One headless goal Planner (docs repo, Fable, account B): 6.17 USD, 1.0M cache-read tokens, 11 min, 652 s API time.
- Workers, 3 days (runs/2026-09-18..20, usd is API-equivalent): execute 128 rows 78.19 USD (in 45.8M, out 2.2M, cache_read 230M; mostly Codex), review 83 rows 67.98 USD (cache_read 52M), spec_review 21 rows 13.61 USD, scout 11 rows 6.12 USD.
- Reading: the premium bottleneck is the interactive Fable session doing bookkeeping the merged Phase G code already automates (marking chains merged, posting Codex results, re-spawning reviews, resetting slot counters, writing fix rounds by hand). The single largest lever is running goal/T-0260 code in the server; Phase H must not rebuild what G7 v4, G6 v4 and G10 already merged.
- Known gaps from session 5 gotchas (all on goal/T-0260): executor running counters persisted and leaking; codex/codex_reply MCP tools do not post results; requeued reviews never re-dispatched; gate does not check acceptance-named tests exist (Codex skipped them 5 times); plan.md rewritten by the daemon every 15 min (competing writer).

## Phase H gap matrix (from scouts T-0354 and T-0355, read once; code = goal/T-0260 at 2ca1517)
Existing and verified: token bucket normalisation incl. cached subset vs separate and no reasoning double count (bus.py:206-229, pool.py:232-313); decision identity per held event and atomic claim under the bus lock (planner_runs.py:189-196, 271-284); stage stamps claim/complete/clear under lock and already_merged idempotency (daemon.py:248-286, 397-428); single-daemon lock so a second account cannot double dispatch (daemon.py:1063-1074); premium headroom via reserve_for_planner (pool.py:209); packet expansion handles and visible truncation markers, empty distinct from truncated (spawn.py); role prompts 94 lines over 9 files, no duplicated contract text; recall shows provenance and age; automatic repair only in scope with lineage and merge-verified reconciliation (daemon.auto_fix_round, report_merge); retry budgets configurable ([daemon].auto_fix_rounds, executor MAX_ROUNDS); review approval bound to reviewed sha and re-assessed after rebase, semantic triggers (G8); Jev sample mode 10 percent (pool.toml:216), 608 gate rows / 190 KB so far.
Partial: run rows lack attempt, decision kind/key, client version, policy version, route reason (bus.py:232-245) -> H1; spend vs API-equivalent vs quota not labelled -> H1 docs; premium invocations only count+usd (scorecard.py:622-632), launch record lacks reason/evidence/cheaper steps (planner_runs.py:271-317) -> H2; bus.events cursor exists but no role filter and no caller persists a cursor -> H8, H3b; infra failures share the 2-strike gave_up with real failures -> H3b; notify fires every tick while a condition holds (daemon.py:1052-1059) -> H4; acceptance summarised to 5 bullets when the packet overflows -> H8; failure kinds only gate_red vs review -> H7; hold dedupe on held_at timestamp not a content signature -> H7; codex_reply resume has no worktree compatibility check -> H7.
Missing: routine/investigate/escalate routing (no decision.py; daemon.py:1044-1049 launches unconditionally) -> H3a, H3b; coalescing of held tasks into one packet -> H3b; pinned config/prompt version per run (goals.py:322-329 re-reads pool.toml fresh by design) -> H2; token-amount budget reservations across parallel dispatch (daemon.free_slots counts slots only) -> H4; tokens per accepted goal returns 0 at zero accepted (scorecard.py:598-603) and no median/tail -> H1; packet hash/version/provenance -> H8; gate does not check acceptance-named tests exist (5 Phase G cases) -> H6; executor running counters leak (persisted increments) and codex tools do not post results -> H5a; requeued reviews never re-dispatched, stray task branch blocks spawn, handover rewrites plan.md on unchanged state (6 times on 2026-09-19) -> H5b.
Unnecessary or deferred with reason: Jev changes (judge cost vs savings unmeasured; sample mode already in force; G5 diagnose exists to build the offline sample first); read-only result cache and test-result cache (no measured duplicate-read or duplicate-test cost; worktree and post-rebase gates cover different states); memory expiry/superseded marks (compact-memory skill exists, volume small); Codex sandbox-boundary scope enforcement (no observed violation; post-run scope diff exists); learned routing (G9 rules exist, sample too small); expansion-read metrics (needs worker tool logs; Jev gate rows could serve later); interactive session exclusion is a pid-liveness check already (planner_runs.py:243-263), functionally a lease.
Regression cases: covered already: idle ticks zero launches (test_tick_launches_nothing_when_autonomous_false), concurrent claim once (test_run_claims_before_launch_so_concurrent_callers_launch_once), usage accounting (test_normalize_usage_*, test_null_usage_counts_zero, test_tally_*), crash reconciliation (test_keyboard_interrupt_releases_claim_and_reraises, reconcile_dead tests), lineage reconciliation and review re-assessment (G7, G8 tests). New in Phase H: reservations (H4), routes and soft budget (H3a/H3b), same-state no relaunch (H3b), unchanged repeated failure stops (H7), truncated results expandable and cursor filters (H8), missing acceptance tests cannot self-certify (H6).

## Phase H task map (all parent T-0353, goal branch goal/T-0353 from goal/T-0260)
- No deps: H1 T-0356 accounting fields (c3); H3a T-0357 decision.py routes (c4); H5a T-0358 running counts from bus + codex tools post (c4); H6 T-0359 gate checks named tests (c3); H8 T-0360 packet header, acceptance whole, bus_events filters (c3).
- Chained: H2 T-0361 launch attribution + scorecard --planner (c4, after T-0356); H5b T-0363 worktree reuse, review re-spawn, handover hash (c4, after T-0359, T-0360); H4 T-0362 reservations + notify per transition (c5, after T-0358, T-0359, T-0360); H7 T-0364 failure signatures and kinds, resume compatibility (c5, after T-0363); H3b T-0365 routes wired, coalescing, cursor, infra backoff (c6, spec review fires; after T-0357, T-0361, T-0364); H9 T-0366 docs (c2, after all).
- Measurement plan: baseline = Phase G (22.38 USD, 82 tasks, 5.96M tokens per accepted goal over 9 goals, interactive Fable 35.2M tokens on 2026-09-19). Phase H runs on the Phase F server, so after-results for the new code come from tests and policy replay only (label: estimate); the measured after-result is Phase H's own scorecard row. Report both in PR 12.

## Phase G (T-0260) closed 2026-09-19 23:56
All eleven sub-goals merged on goal/T-0260 (head 2ca1517, 37 commits over goal/T-0240), tests-green exit 0 externally, PR 11 https://github.com/K3NTAW/orchestrator/pull/11 open for the human after PR 10, retrospective in decisions.md. Cost 22.38 USD, 82 tasks, review 62.8 percent.

## docs.kentawaibel.com (LIVE 2026-09-20 00:30)
Repo /Users/k3ntaw/code/docs-kentawaibel (private GitHub K3NTAW/docs-kentawaibel, PR 1 goal/T-0001 to main open). Headless Planner on account B: 11 min, 6.17 USD, five Codex tasks, zero fix rounds. Vercel project docs-kentawaibel (linked to the GitHub repo, production branch main), domain docs.kentawaibel.com, production deploy from goal/T-0001 at ebf4a11. Merging PR 1 triggers the first automatic deploy.

## Read first
1. `bus_read(status_not="done")` then skill `resume`. Live: T-0353 and its children; the rest are superseded/failed leftovers.
2. Planner commits from the main checkout (goal/T-0240): stage only .orchestrator/ paths; commit with `git commit -F <scratchpad file>`. planner-mode.sh scans the whole command text: keep backticks, `>`, `<`, and the words it treats as writes (cp, rm, mv, install, tail with a pipe, git rebase/checkout/reset/revert, .env) out of Bash text. Use absolute paths. macOS has no `timeout`; use run_in_background.
3. Tool contracts: `spawn_scout(<scout id>)`; `spawn_review(<review task id>)`; `spawn_spec_review(<spec_review task id>)`; `merge(task_id, target)` skips the review policy. `codex(task_id, prompt)` and `codex_reply(task_id, delta)` return the result dict and do NOT post it: bus.post_result by hand. Specs are immutable: a fix round is a new task with constraints.fix_round_for; mark the chain merged by hand (bus.update(original, status='done', merged_into=..., merged_via='fix round <id> <sha>', hold_reason=None)) until G7 v4 runs in the server.
4. Before trusting a Codex result whose acceptance names test ids: `git diff --stat HEAD~1` in the worktree must touch tests/; if not, one codex_reply listing the missing ids fixes it.
5. Slot counters: pool_state.json running fields leak when a restart kills a Codex process; reset only when pgrep shows no codex exec and no claude -p worker.
6. A review that shows status queued with a "process died; requeued" event never runs again by itself: spawn_review(<id>) by hand.
7. Restart rule: hand over at ~150k context or when an account's daily budget is reached, only when no worker is alive. Keep Planner turns short; this goal's own objective is fewer Fable tokens.
8. The daemon appends an "Auto-handover" section to this file every 15 min while a goal is open; drop it when rewriting.
9. Goal branch for T-0353 is goal/T-0353 (from goal/T-0260); execute tasks with parent T-0353 base on it. When Phase H closes: PR 12 goal/T-0353 to main, listing PR 11 as its base.

## Human items outstanding
- Merge PRs 6, 7, 8, 9, 10, 11 in order; PR 1 in the docs repo. PR 12 (Phase H) follows.
- Decisions: latency target, security_paths width, daemon as a process (Phase F review); review budget (Phase G: 22.38 USD, review 63 percent, every review found a defect).

## Backlog (c2-c3, after PR 11) — candidates for Phase H specs
- Pool load derives executor running counters from bus tasks; drop the legacy codex mirror.
- codex and codex_reply MCP tools post the result to the bus.
- dispatch re-spawns queued review and spec_review tasks whose pipeline is empty.
- Gate checks that every tests/...::name in the acceptance resolves to a defined test.
- Consistent total_tokens for pre-G1a scorecard rows; recall ranking relevance.
- kgpt side of the orchestrator module (C-K1..C-K4) on the kgpt bus.

## Phase C architecture (decided)
Two processes. (1) kgpt `modules/orchestrator`, thin MCP module on the shared image, runs_on home, owns per-user `orchestrator_connections`, tools list_goals/goal_status READ and start_goal/cancel_goal WRITE_EXTERNAL, forwards to the user's endpoint with X-KGPT-User-Id. (2) `orchestrator serve` in its own container on kenta-server, one ORCH_ROOT per repo under /work, `goal start` = install+commit scaffold + GOAL task + headless Planner launch; bearer per endpoint; per-slug locks.

## Phase D architecture (decided)
The daemon owns the loop; the Planner becomes a function of decision points (planner_runs.py): task held, scout fan-out done, goal closable. Each decision is a fresh headless `claude -p` launched by goals.launch_planner with an ids-only prompt (G10: compact decision packet), recorded in planner_runs.json, reconciled when dead, terminal gave_up after two failures. Never while an interactive session is registered in planner_session.json.

## Prior art (ids for recall.sh get)
- Phase C, D, F, G retrospectives: decisions.md 2026-09-18/19 entries (goals T-0073, T-0109, T-0240, T-0260). Gotchas of 2026-09-19 in gotchas.md.
- bus:T-0261..T-0263 Phase G scouts; T-0354, T-0355 Phase H scouts.
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt, home `ssh k3ntaw@192.168.1.167` (fish shell), kgpt home side /opt/kgpt-home.

## Auto-handover 2026-09-20T12:38:55+02:00 — daemon tick

### T-0353 GOAL Phase H: efficiency handover — gap assessment against goal/T-0260, normalized accounting completeness, premium (Fable) launch attribution and escalation routing, no-work/no-duplicate decision guarantees, budget reservations where parallel spend exists, packet and result hygiene, bounded repair stopping rules, Jev and memory conditionals, reviewable PR
- queued: T-0354 Scout H-S1 (gap matrix, handover sections 2-5): accounting completeness, premium launch attribution and escalation routes, decision identity/claims/coalescing/backoff, budget reservations and quota handling (depends_on=[])
- done (not merged): T-0355 Scout H-S2 (gap matrix, handover sections 6-11): packets, bounded results and bus deltas, repair stopping rules and failure signatures, resume compatibility, review binding, Jev cost, memory and snapshot hygiene, scope enforcement at the sandbox boundary

Worktrees: wt/T-0002, wt/T-0006, wt/T-0007, wt/T-0008, wt/T-0009, wt/T-0010, wt/T-0012, wt/T-0013, wt/T-0014, wt/T-0016, wt/T-0018, wt/T-0021, wt/T-0022, wt/T-0023, wt/T-0026, … and 144 more

Last events:
- 2026-09-20T12:35:06+02:00 T-0355 created {}
- 2026-09-20T12:35:22+02:00 T-0355 update {"status": "running", "assigned_to": "claude:A", "worktree": "/Users/k3ntaw/code
- 2026-09-20T12:35:22+02:00 T-0355 update {"account": "A"}
- 2026-09-20T12:35:22+02:00 T-0355 update {"pid": 51957, "account": "A"}
- 2026-09-20T12:38:44+02:00 T-0355 update {"status": "done", "result": {"summary": "", "findings": [{"claim": "Packet has 

Resume: skill resume; re-spawn held spec reviews; dispatch ready execute tasks by hand while Codex cools.
