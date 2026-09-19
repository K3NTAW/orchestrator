# plan.md — Planner checkpoint (updated 2026-09-19 23:02, session 5, Phase G closing out)

## State in one paragraph
Open goal: T-0260 Phase G token economy (complexity 8), branch goal/T-0260 from goal/T-0240 head 4c92cac. Session 5 started 22:50 on a fresh MCP server (Phase F code live: the daemon posts Codex results it dispatched itself, holds red merges, and reconciles dead workers that carry a pid). The restart orphaned two Codex runs dispatched by the old server: T-0326 G7 v4 (resumed with codex_reply, committed 8262cd7, result posted by hand because codex_reply returns the result but does not post it) and T-0331 G10 fix round 1 (fresh codex thread started 22:53, running). Merged on goal/T-0260 so far: G0, G1a+fix, G1b+fix, G2+2 fixes, G2b chain (T-0267, T-0302, T-0307, T-0329 at 644285b), G4+fix (T-0274, T-0280), G6 v4 (T-0290), G11+fix (T-0324, T-0328 at b12190e). Not merged, contrary to the session 4 plan: G3 T-0271 is held on review T-0309 (duplicated hunk headers in bounded_diff, render re-bounds, tests missing); fix round T-0336 queued 23:00.

## Phase G task map (23:02)
- Merged: G0 T-0270; G1a T-0264+T-0283; G1b T-0265+T-0291; G2 T-0266+T-0284+T-0298; G2b T-0267+T-0302+T-0307+T-0329; G4 T-0274+T-0280; G6 v4 T-0290 (T-0268, T-0281, T-0286 superseded); G11 T-0324+T-0328.
- In review: G7 v4 T-0326 (done 8262cd7, review T-0335 running, security_paths); G9 fix round 3 T-0327 (done d0ba3a7, review T-0332 requeued after the restart killed its worker, waits for a review slot). When T-0326 merges: mark T-0272, T-0296, T-0320 done+merged_into goal/T-0260 (merged_via 'fix round T-0326 <sha>'), which unblocks G8 T-0277. When T-0327 merges: mark T-0314, T-0308, T-0275 the same way.
- Running: G5 T-0273 (dispatched 22:59 after T-0267 was marked merged); G10 fix round 1 T-0331 (Codex luna, fresh thread; original T-0276 held on review T-0330). When T-0331 merges: mark T-0276.
- Queued: G3 fix round 1 T-0336 (fix_round_for T-0271; the spec text says "commit with T-0335", cosmetic); G8 T-0277 (depends T-0272, T-0267).
- Closed leftovers: T-0301 feat-none smoke task marked failed 22:53; T-0299, T-0311 failed spec reviews stay as they are.
- Backlog after Phase G (c2 each): consistent total_tokens for legacy scorecard rows; recall ranking relevance; review-budget decision (T-0260 at about 12 USD at 22:00, every review found a defect); codex_reply and codex MCP tools should post the result to the bus like the daemon's executor thread does (see gotcha 2026-09-19 23:00).

## Phase G measurements and cost (22:30)
- scorecard --by goal T-0260: 9.60 USD at 21:55 (44.9 percent review, 39.1 percent spec review, 16 percent scouts), above the 3.0 USD review budget; every one of the ten code reviews and five spec reviews found a real defect. Decision for the human: keep spec review from 6 and security_paths as is (it paid), or narrow.
- tokens per accepted goal (G1b, on this repo): 5,963,247 over 9 goals (usd 20.27); legacy rows mix with normalised ones.
- G2 packet for T-0256: 3,335 chars, 21 symbols, 15 relevant tests. repomap --stdout: 3,966 chars.

## Read first
1. `bus_read(status_not="done")` then skill `resume`. Live: T-0260 and the children above; the rest are superseded/failed leftovers.
2. Planner commits from the main checkout (goal/T-0240): `git status --short` first; stage only .orchestrator/ paths; commit with `git commit -F <scratchpad file>`. planner-mode.sh scans the whole command text: keep backticks, `>`, and the words it treats as writes (cp, rm, mv, install, tail with a pipe, git rebase/checkout/reset/revert even inside quoted prose) out of Bash text; put long text in a scratchpad file with the Write tool. Use absolute paths: a `cd wt/...` inside a command moves the session cwd.
3. Tool contracts: `spawn_spec_review(<spec_review task id>)` (given an execute id it RUNS AN EXECUTE); `spawn_scout(<execute id>)` runs the fallback executor; `spawn_review(<review task id>)`; `merge(task_id, target)` rebases, tests and fast-forwards but skips the review policy. `codex(task_id, prompt)` and `codex_reply(task_id, delta)` return the result dict and do NOT post it: post with bus.post_result(tid, {summary, commit, executed_by, provenance:['repo'], usage}, 'done') by hand. Specs, scope and depends_on are immutable on the bus: a fix round is a new task with constraints.fix_round_for (its worktree is cut from the held task's branch); mark the chain merged by hand (bus.update(original, status='done', merged_into='goal/T-0260', merged_via='fix round <id> <sha>', hold_reason=None)) until G7 v4 runs in the server. A rebase is a Codex task (T-0258 pattern).
4. Review policy in force: code review only when the merged diff touches pool.toml [review] security_paths (orchestrator/*.py and .orchestrator/prompts/** match almost everything); spec review from complexity 6 on sonnet, waived by the Planner after three rounds; tests-green is the merge bar otherwise.
5. Restart rule: hand over at ~150k context or when an account's daily budget is reached, and only when no codex exec or claude -p worker is alive (pgrep). Tasks dispatched by the old server carry no pid, so the new daemon never reconciles them: resume each by hand (codex_reply on the thread if one exists, else a fresh codex thread).
6. The daemon appends an "Auto-handover" section to this file every 15 min while a goal is open; drop it when rewriting.
7. Account state at 22:51: A planner_day_tokens 33.4M over its 30M budget, B at 4.7M; `orchestrator pick planner` said "hold: no account with headroom" (rate_limit reason on both). Session 5 runs anyway; keep Planner turns short.

## Human items outstanding
- Merge PRs 6, 7, 8, 9, 10 in order: https://github.com/K3NTAW/orchestrator/pull/10 is Phase F. PR 11 (Phase G) comes after Phase G closes.
- Decisions from the Phase F review (latency target, security_paths width, daemon as a process) and the Phase G review-budget question above.

## Next work after Phase G
1. Retrospective for T-0260 (skill memory record), open PR 11 goal/T-0260 → main.
2. Switch the checkout to goal/T-0260 after PR 11 opens so the daemon runs the lease sweep, auto fix rounds and packets on itself; measure tokens per accepted goal on the next goal against 5.96M.
3. kgpt side of the orchestrator module (C-K1..C-K4) on the kgpt bus.

## Phase C architecture (decided)
Two processes. (1) kgpt `modules/orchestrator`, thin MCP module on the shared image, runs_on home, owns per-user `orchestrator_connections` (endpoint_url, label, encrypted bearer), tools list_goals/goal_status READ and start_goal/cancel_goal WRITE_EXTERNAL (proposals), forwards to the user's endpoint with X-KGPT-User-Id applied by UserContextMiddleware. (2) `orchestrator serve` in its own container on kenta-server (ubuntu:24.04 image from this repo: claude native installer, codex release binary, uv, git, gh), one ORCH_ROOT per repo under /work, `goal start` = install+commit scaffold + GOAL task + headless Planner launch; bearer per endpoint; per-slug locks.

## Phase D architecture (decided)
The daemon owns the loop; the Planner becomes a function of decision points (planner_runs.py): task held, scout fan-out done, goal closable. Each decision is a fresh headless `claude -p` launched by goals.launch_planner with an ids-only prompt, recorded in planner_runs.json, reconciled when dead, terminal gave_up after two failures. Never while an interactive session is registered in planner_session.json. Review cost is bounded by pool.toml [review]; scout cost by pool.toml scout limits; Planner session cost by the 150k handover rule.

## Prior art (ids for recall.sh get)
- Phase C retrospective and D3/Phase D retrospective: decisions.md 2026-09-18/19 entries (goal T-0073, T-0109). Gotchas of 2026-09-18/19 in gotchas.md.
- bus:T-0066..T-0069 kgpt scouts; T-0074..T-0077 Phase C scouts; T-0261..T-0263 Phase G scouts.
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt (compose project), home `ssh k3ntaw@192.168.1.167` (fish shell, pipe scripts via `bash -s`), kgpt home side /opt/kgpt-home.
