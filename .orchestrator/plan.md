# plan.md — Planner checkpoint (updated 2026-09-19 22:40, session 4 handover mid Phase G; written for a fresh session)

## State in one paragraph
Open goal: T-0260 Phase G token economy (complexity 8), branch goal/T-0260 from goal/T-0240 head 4c92cac. Session 4 handed over because account A reached its daily Planner budget (30M tokens) and the pre-F3 MCP server's executor counters leaked (status() says Codex exhausted while no codex exec runs, so new dispatches fall back to Claude sonnet). The main checkout is goal/T-0240: the next server loads Phase F code (F3 posts Codex results itself, F5 codex_reply, F6 red-merge holds) but not Phase G. Merged on goal/T-0260 so far (see git log goal/T-0260): G4 docs+fix, G1a+fix, G6 v4 leases, G1b+fix, G2 chain (packet, two fixes), G0 scout base, G3 bounded outputs, G9 chain (routing, two fixes; verify T-0314 merged and T-0308/T-0275 marked), G2b chain (repomap, two fixes; verify T-0307 merged and T-0302/T-0267 marked). In flight at handover: T-0276 G10 decision packets (work uncommitted by Codex, a sonnet fallback worker was committing it), T-0320 G7 v3 automatic fix rounds (c6, needs its spec review; if the daemon stamps spec_review_at without creating the child, create it by hand in the daemon shape and spawn_spec_review), T-0324 G11 prompts say commit (c2), T-0273 G5 sampled Jev (depends T-0265 and T-0267 marked merged), T-0277 G8 semantic review triggers (depends T-0272 and T-0267; mark T-0272 and T-0296 merged when T-0320 merges). First steps in the new session: bus_read(status_not="done") for T-0260 children; for each done-but-unmerged fix round check merged_into and mark its originals (pattern: bus.update(original, status='done', merged_into='goal/T-0260', merged_via='fix round <id> <sha>')); then let the daemon run. With F3 live the daemon posts Codex results itself; verify on the first Codex task that it reaches done without a Planner post (this also closes T-0240 acceptance 3).

## Phase G measurements and cost (22:30)
- scorecard --by goal T-0260: 9.60 USD at 21:55 (44.9 percent review, 39.1 percent spec review, 16 percent scouts), above the 3.0 USD review budget; every one of the ten code reviews and five spec reviews found a real defect (unguarded lookup in the run logger, packet ranking and truncation, repomap trim and a gate skip, routing dead code and floor fallback, lease reconciliation gaps, fix-round coordination and lineage cap). Decision for the human: keep spec review from 6 and security_paths as is (it paid), or narrow.
- tokens per accepted goal (G1b, on this repo): 5,963,247 over 9 goals (usd 20.27); T-0240 row shows cache_read above total_tokens because legacy rows mix with normalised ones (polish: make total_tokens consistent for pre-G1a rows).
- G2 packet for T-0256: 3,335 chars, 21 symbols, 15 relevant tests; a 100-bullet acceptance list stays under the cap without losing discovery sections.
- repomap --stdout: 3,966 chars, 4 functions each for daemon.py, bus.py, spawn.py; architecture.md refreshes from the merged sha after each merge that touches orchestrator/*.py.

## Phase G task map
- G0 T-0270 merged. G1a T-0264 (+T-0283) merged. G1b T-0265 (+T-0291) merged. G2 T-0266 (+T-0284, +T-0298) merged. G2b T-0267 (+T-0302, +T-0307) pending merge of T-0307. G3 T-0271 merged. G4 T-0274 (+T-0280) merged. G6 T-0290 merged (T-0268, T-0281, T-0286 superseded). G9 T-0275 (+T-0308, +T-0314) pending merge of T-0314. G10 T-0276 committing. G7 T-0320 queued (T-0272, T-0296 superseded). G5 T-0273 queued. G8 T-0277 queued. G11 T-0324 queued.
- Backlog after Phase G (c2 each): consistent total_tokens for legacy rows; recall ranking puts unrelated claude-mem hits first (relevance, not just provenance); worker sessions and the repomap refresh once the server runs G code; the review budget decision above.

## Read first
1. `bus_read(status_not="done")` then skill `resume`. Live: T-0260 and its children listed above; the rest are superseded/failed leftovers.
2. Planner commits from the main checkout (goal/T-0240): `git status --short` first; stage only .orchestrator/ paths; commit with `git commit -F <scratchpad file>`. planner-mode.sh scans the whole command text: keep backticks, `>`, and the words it treats as writes (cp, rm, mv, install, tail with a pipe, git rebase/checkout/reset/revert even inside quoted prose) out of Bash text; put long text in a scratchpad file with the Write tool and read it back with cat. Use absolute paths: a `cd wt/...` inside a command moves the session cwd.
3. Tool contracts: `spawn_spec_review(<spec_review task id>)` (given an execute id it RUNS AN EXECUTE); `spawn_scout(<execute id>)` runs the fallback executor; `spawn_review(<review task id>)`; `merge(task_id, target)` rebases, tests and fast-forwards but skips the review policy. `codex(task_id, prompt)` works on a running task; when Codex is exhausted it silently falls back to a sonnet worker. Specs, scope and depends_on are immutable on the bus: a fix round is a new task with constraints.fix_round_for (its worktree is cut from the held task's branch); mark the chain merged by hand until G7 lands. A rebase is a Codex task (T-0258 pattern).
4. Review policy in force: code review only when the merged diff touches pool.toml [review] security_paths (orchestrator/*.py matches almost everything); spec review from complexity 6 on sonnet, waived by the Planner after three rounds (set complexity 5 on the replacement); tests-green is the merge bar otherwise.
5. Restart rule: hand over at ~150k context or when an account's daily budget is reached, and only when no codex exec or claude -p worker is alive (pgrep).
6. The daemon appends an "Auto-handover" section to this file every 15 min while a goal is open; drop it when rewriting.

## Human items outstanding
- Restart the Planner session (checkout goal/T-0240) so F3, F5 and F6 are live; account A is at its daily budget, `orchestrator pick planner` names the account.
- Merge PRs 6, 7, 8, 9, 10 in order: https://github.com/K3NTAW/orchestrator/pull/10 is Phase F. PR 11 (Phase G) comes after Phase G closes.
- Decisions from the Phase F review (latency target, security_paths width, daemon as a process) and the Phase G review-budget question above.

## Next work after Phase G
1. Verify goal T-0240 acceptance 3 and codex_reply on the restarted server.
2. Switch the checkout to goal/T-0260 after PR 11 opens so the daemon runs the lease sweep, auto fix rounds and packets on itself; measure tokens per accepted goal on the next goal against 5.96M.
3. kgpt side of the orchestrator module (C-K1..C-K4) on the kgpt bus.

## Phase C architecture (decided)
Two processes. (1) kgpt `modules/orchestrator`, thin MCP module on the shared image, runs_on home, owns per-user `orchestrator_connections` (endpoint_url, label, encrypted bearer), tools list_goals/goal_status READ and start_goal/cancel_goal WRITE_EXTERNAL (proposals), forwards to the user's endpoint with X-KGPT-User-Id applied by UserContextMiddleware. (2) `orchestrator serve` in its own container on kenta-server (ubuntu:24.04 image from this repo: claude native installer, codex release binary, uv, git, gh), one ORCH_ROOT per repo under /work, `goal start` = install+commit scaffold + GOAL task + headless Planner launch; bearer per endpoint; per-slug locks.

## Phase D architecture (decided)
The daemon owns the loop; the Planner becomes a function of decision points (planner_runs.py): task held, scout fan-out done, goal closable. Each decision is a fresh headless `claude -p` launched by goals.launch_planner with an ids-only prompt, recorded in planner_runs.json, reconciled when dead, terminal gave_up after two failures. Never while an interactive session is registered in planner_session.json. Review cost is bounded by pool.toml [review]; scout cost by pool.toml scout limits; Planner session cost by the 150k handover rule.

## Prior art (ids for recall.sh get)
- Phase C retrospective and D3/Phase D retrospective: decisions.md 2026-09-18/19 entries (goal T-0073, T-0109). Gotchas of 2026-09-18/19 in gotchas.md (stale checkout, spawn_spec_review contract, dispatch break, pre-stamp rule, docker builds outlive executors, amd64 emulation impossible here, claude CLI vanished).
- bus:T-0066..T-0069 kgpt scouts; T-0074..T-0077 Phase C scouts.
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt (compose project), home `ssh k3ntaw@192.168.1.167` (fish shell, pipe scripts via `bash -s`), kgpt home side /opt/kgpt-home.

