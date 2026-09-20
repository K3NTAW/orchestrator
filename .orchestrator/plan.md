# plan.md — Planner checkpoint (updated 2026-09-20 17:45, session 5; Phase H closed, PR 12 open; no open goal on this bus)

## State in one paragraph
Phase H (T-0353, efficiency handover, complexity 8) closed at 17:36: ten increments merged on goal/T-0353 (head 0d7bf09, 45 commits and 34 files over goal/T-0260 at 2ca1517), tests-green exit 0 externally on wt/goal-T-0353, PR 12 https://github.com/K3NTAW/orchestrator/pull/12 open for the human after PR 11, retrospective in decisions.md 2026-09-20, GOAL result posted. The main checkout stays on goal/T-0240 (the running MCP servers load Phase F code); switch to goal/T-0353 after PRs 11 and 12 merge so routes, reservations, the acceptance-test gate and failure signatures act, then measure tokens per accepted goal on the next goal against 5.96M. Two Claude Code sessions were attached after the 16:36 restart; the launcher session (planner_session.json pid 7181) ran the docs.kentawaibel.com goal T-0007 in the docs repo (done 17:06, PR 2 there) while this session finished Phase H. No open goal remains on this bus.

## Phase H results (measured unless marked)
- Cost: 21.50 USD API-equivalent, 417 calls, 611 turns; review 84.4 percent, spec review 8.0, scouts 7.6; Codex execute 0 percent because Codex rows carry no usd (backlog: scorecard derives it from tokens as the H4 ledger does). 91 tasks: 49 execute (33 fix rounds and rebases: 14 reliability, 10 budget, 4 context, 3 rebases, 1 accounting, 1 routing), 36 reviews (13 approve, 23 request_changes), 4 spec reviews, 2 scouts.
- Baseline Phase G: 22.38 USD, 82 tasks, 5.96M tokens per accepted goal over 9 goals; interactive Fable 35.2M tokens on 2026-09-19, zero headless decision runs.
- After-results for the new policies: estimate only until the server runs goal/T-0353 code. Counted Planner interventions the new code removes: nine codex_reply rounds for tests Codex never wrote (H6 makes that a gate_red), two hand re-spawns of dead reviews (H5b), three slot-counter resets (H5a), every chain marked merged by hand (G7 v4 already merged).
- Delivered: H1 accounting fields and undefined ratio; H2 launch attribution and scorecard --planner; H3a decision routes; H3b routing wired (failures, gitutil, notify modules; per-goal launch guard; evidence-checked idempotent close, PR left to the Planner unless auto_open_pr); H4 reservations owned by the run that starts, ledger behind its flock, notify once per transition; H5a counts from bus, codex tools post results; H5b worktree reuse, review re-spawn, snapshot hash; H6 acceptance-test gate; H7 failure kinds and signatures, bounded validated flaky rerun, resume compatibility; H8 packet header, acceptance never dropped, bus_events filters; H9 docs.
- Deferred with reasons (matrix in git history at 9111d99): Jev changes, read and test result caches, memory expiry, Codex sandbox scope enforcement, learned routing, expansion metrics.

## Lessons (recorded in gotchas.md and decisions.md)
- Codex shipped code without its acceptance-named tests nine times; check `git diff --stat HEAD~1` for tests/ before trusting a result until H6 is live.
- A full-suite failure that passes alone needs an ordered-module run before a fix round (tests.test_cli autostart thread leak).
- Reservation hand-offs between dispatcher and worker breed defects; the run that starts owns its reservation.
- Pool.save must never serialize state that a locked helper owns (running counters, reservation ledger).
- Two Planner sessions on one bus yield to each other symmetrically; plan.md carries an OWNER header, and the session whose spec got reviewed continues.

## docs.kentawaibel.com
- Site repo /Users/k3ntaw/code/docs-kentawaibel (private GitHub K3NTAW/docs-kentawaibel; Vercel project docs-kentawaibel linked to it, production branch main). Live since 2026-09-20 00:30 from goal/T-0001 (PR 1 open). Goal T-0007 (orchestrator system map page public/orchestrator/index.html, six SVG diagrams, twelve systems, glossary; entry content/orchestrator.md rewritten) done on goal/T-0007, PR 2 https://github.com/K3NTAW/docs-kentawaibel/pull/2 (base PR 1). This session verifies gate and build on wt/deploy7, deploys production from that branch and publishes the page as a shareable artifact. Human: merge PR 1 then PR 2 so main matches production.

## Read first (next session)
1. `bus_read(status_not="done")` then skill `resume`; no goal is open on this bus. Leftovers are superseded or failed tasks.
2. Planner commits from the main checkout (goal/T-0240): stage only .orchestrator/ paths; commit with `git commit -F <scratchpad file>`. planner-mode.sh scans the whole command text: keep backticks, `>`, `<`, and the words it treats as writes (cp, rm, mv, install, touch, tail with a pipe, git rebase/checkout/reset/revert, .env) out of Bash text. Use absolute paths. macOS has no `timeout`.
3. Tool contracts: `spawn_scout(<scout id>)` may answer spawned while ensure_worktree failed on a stray task branch (check the task claimed); `spawn_review(<review id>)`; `spawn_spec_review(<spec_review id>)`; `merge(task_id, target)` skips the review policy. `codex` and `codex_reply` return the result dict and do NOT post it on the Phase F server. Specs are immutable: a fix round is a new task with constraints.fix_round_for; mark chains merged by hand until goal/T-0353 code runs.
4. Slot counters in pool_state.json leak on restart-killed processes on the Phase F server; reset only when pgrep shows no codex exec and no claude -p worker.
5. A review or spec review requeued after a dead worker never runs again on the Phase F server: spawn it by hand.
6. Session restart kills the in-process daemon and its posting threads; workers that survive lose their result. Check pgrep before restarting.
7. Two sessions attached: read the OWNER header before creating tasks.

## Human items outstanding
- Merge PRs 6 to 12 in order in the orchestrator repo; PR 1 then PR 2 in the docs repo.
- Decisions: latency target, security_paths width, daemon as a process (Phase F review); review budget (Phases G and H: every review found a real defect); [limits].reservations on or off after the first production goal; [planner.routes].auto_open_pr stays false.

## Backlog (c2-c3, after PR 12)
- Scorecard usd for Codex rows from tokens (reuse pool.usd_of); [daemon].respawn_after_s documented in pool.toml; consistent total_tokens for pre-G1a rows; recall ranking relevance.
- kgpt side of the orchestrator module (C-K1..C-K4) on the kgpt bus.

## Phase C architecture (decided)
Two processes. (1) kgpt `modules/orchestrator`, thin MCP module on the shared image, runs_on home, owns per-user `orchestrator_connections`, tools list_goals/goal_status READ and start_goal/cancel_goal WRITE_EXTERNAL, forwards to the user's endpoint with X-KGPT-User-Id. (2) `orchestrator serve` in its own container on kenta-server, one ORCH_ROOT per repo under /work, `goal start` = install+commit scaffold + GOAL task + headless Planner launch; bearer per endpoint; per-slug locks.

## Phase D architecture (decided)
The daemon owns the loop; the Planner is a function of decision points (planner_runs.py): task held, scout fan-out done, goal closable, now routed routine/investigate/escalate (H3). Each launch is a fresh headless `claude -p` with a compact decision packet, recorded with route, reason, evidence and cheaper steps, reconciled when dead, gave_up after two real failures (infra failures cool the account instead).

## Prior art (ids for recall.sh get)
- Phase C, D, F, G, H retrospectives: decisions.md 2026-09-18 to 2026-09-20 (goals T-0073, T-0109, T-0240, T-0260, T-0353). Gotchas of 2026-09-19 and 2026-09-20 in gotchas.md.
- bus:T-0354, T-0355 Phase H scouts; docs bus T-0008, T-0009 system-map scouts.
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt, home `ssh k3ntaw@192.168.1.167` (fish shell), kgpt home side /opt/kgpt-home.
