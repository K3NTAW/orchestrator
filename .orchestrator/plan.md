# plan.md — Planner checkpoint (updated 2026-09-20 12:50, session 5; open goal: T-0353 Phase H efficiency handover)

## State in one paragraph
Open goal: T-0353 Phase H, complexity 8: implement /Users/k3ntaw/Documents/orchestrator-efficiency-handover.md on top of goal/T-0260 (Phase G, PR 11 unmerged). Goal branch goal/T-0353 cut from goal/T-0260 head 2ca1517 at 12:48; a read-only checkout of that head sits at wt/goal-T-0260 for scouts (the running server is still Phase F code on goal/T-0240, so scout worktrees are stale and every spec says READ wt/goal-T-0260). Two sonnet scouts on account B are running the gap matrices: T-0354 (handover sections 2-5) and T-0355 (sections 6-11). Next: summarize both once into the matrix below, decide the core changes, write atomic specs with parent T-0353 (the daemon dispatches them into goal/T-0353), then PR 12 goal/T-0353 to main. Phase G closed 2026-09-19 23:56 (PR 11). docs.kentawaibel.com is live (see below).

## Phase H: premium usage evidence (measured, 2026-09-20 12:45)
- Interactive Planner (Fable, account A): planner_day_tokens 35.2M on 2026-09-19 (status() at 23:24), over the 30M daily budget; this session 5 alone ran Phase G's tail, the docs goal and now Phase H. Zero headless decision runs recorded in this root (no .orchestrator/planner_runs.json). One headless goal Planner (docs repo, Fable, account B): 6.17 USD, 1.0M cache-read tokens, 11 min, 652 s API time.
- Workers, 3 days (runs/2026-09-18..20, usd is API-equivalent): execute 128 rows 78.19 USD (in 45.8M, out 2.2M, cache_read 230M; mostly Codex), review 83 rows 67.98 USD (cache_read 52M), spec_review 21 rows 13.61 USD, scout 11 rows 6.12 USD.
- Reading: the premium bottleneck is the interactive Fable session doing bookkeeping the merged Phase G code already automates (marking chains merged, posting Codex results, re-spawning reviews, resetting slot counters, writing fix rounds by hand). The single largest lever is running goal/T-0260 code in the server; Phase H must not rebuild what G7 v4, G6 v4 and G10 already merged.
- Known gaps from session 5 gotchas (all on goal/T-0260): executor running counters persisted and leaking; codex/codex_reply MCP tools do not post results; requeued reviews never re-dispatched; gate does not check acceptance-named tests exist (Codex skipped them 5 times); plan.md rewritten by the daemon every 15 min (competing writer).

## Phase H gap matrix (to fill from T-0354 and T-0355 once)
(pending)

## Phase H task map
- Scouts: T-0354 (sections 2-5), T-0355 (sections 6-11) running on account B.
- Execute specs: after the matrix.

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
