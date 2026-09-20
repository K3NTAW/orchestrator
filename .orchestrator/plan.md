# plan.md — Planner checkpoint (updated 2026-09-19 20:30, session 4, Phase F closed; written for a fresh session)

## State in one paragraph
No open goal. Phase F (T-0240) is code-complete: goal/T-0240 = fe7026a, 10 commits over Phase E head df08523 (F1-F7, see decisions.md 2026-09-19 Phase F retrospective and reviews/2026-09-19-phase-f-review.md). PR 10 goal/T-0240 to main is open for the human after PRs 6, 7, 8, 9. The main checkout was switched to goal/T-0240 at the end of session 4 so the next MCP server loads F3 (daemon posts Codex results), F5 (codex_reply argv) and F6 (red merges hold). First thing in the next session: confirm the checkout branch is goal/T-0240 and status() shows Codex available; then run one tiny c2 Codex task to verify goal acceptance 3 (a Codex-executed task reaches done on the bus without a Planner bus_post_result) and that codex_reply returns a message on its thread. Do not restart the session while a Codex task is running: the daemon lives inside the MCP server and its dispatch threads die with it (gotcha 2026-09-19).

## Decisions waiting on the human (from the Phase F review)
- Gate latency target: median 667 ms, 590 of it the Jev network call; accept about 700 ms in log mode or spec async scoring (hook returns at once, verdict appended later; block synchronously only for destructive tool kinds).
- security_paths width: orchestrator/*.py reviewed every Phase F task (nine reviews, 3.44 USD, one changed code); narrow to executor.py, merge.py, the daemon merge path, mcp.py, hooks, prompts, or keep.
- Daemon as its own process (launchd or supervisor) instead of a thread in orchestrator.mcp, before the next long goal.
- Merge order for PRs 6, 7, 8, 9, 10.

## Backlog (c2 each, spec when asked)
README note that block mode follows a week of log rows; jev_gate.main() cache() without reset (T-0246); cli._scorecard_measurement_totals recomputes by_task per goal render (T-0255); bus atexit close without the connection lock (T-0257); worktree pruning (wt/ holds over 100 directories); orchestrator repair command family (post-from-worktree, mark-merged-via, regate); probe codex exec resume --help at server start; per-module test processes in the gate.

## Read first
1. `bus_read(status_not="done")` then skill `resume`. Everything under T-0240 is done; the rest are superseded/failed leftovers from earlier phases.
2. Planner commits from the main checkout: `git status --short` first; stage only .orchestrator/ paths; commit with `git commit -F <scratchpad file>` (the co-author line's `>` trips planner-mode.sh). planner-mode.sh scans the whole command text: record.sh facts, python heredocs and any bash text must avoid backticks, `>`, and the words the hook treats as writes (cp, rm, mv, install, tail with a pipe, git rebase/checkout/reset/revert even inside quoted prose). Put long text in a scratchpad file with the Write tool and read it back with cat.
3. Tool contracts: `spawn_spec_review(<spec_review task id>)` (given an execute id it RUNS AN EXECUTE); `spawn_scout(<execute id>)` runs the fallback executor; `spawn_review(<review task id>)`; `merge(task_id, target)` rebases, tests and fast-forwards but skips the review policy, so use it only after the review exists. `codex(task_id, prompt)` works on a running task and returns the result dict without posting it (post by hand if the daemon predates F3). Specs, scope and depends_on are immutable on the bus (recreate the task; mark the superseded one done+merged_into when the replacement merges). merge() and the daemon stamp merged_into only on the task they merged: mark the chain by hand with bus.update. A branch rebase is a Codex task with constraints.fix_round_for set (T-0258 pattern).
4. Review policy in force: code review only when the merged diff touches pool.toml [review] security_paths (one sonnet review, security checklist on); spec review from complexity 6 on sonnet; tests-green is the merge bar otherwise. Review budget 3.0 USD per goal.
5. If the checkout goes stale after a merge into the checked-out branch: `git diff --name-only HEAD -- . ':!.orchestrator'` then restore those paths from HEAD, never the whole tree.
6. Restart rule: hand over at ~150k context and only when no worker or Codex task is running.
7. The daemon appends an "Auto-handover" section to this file every 15 min while a goal is open; drop it when rewriting.

## Human items outstanding
- Merge PR 6 (install scaffold): https://github.com/K3NTAW/orchestrator/pull/6, then PR 7 (Phase C): https://github.com/K3NTAW/orchestrator/pull/7, then PR 8 (Phase D): https://github.com/K3NTAW/orchestrator/pull/8, PR 9 (Phase E), PR 10 (Phase F). Each builds on the previous branch history.
- GitHub App: add permission "Pull requests: read and write"; private key + App ID via `make secrets-edit` on the Hetzner box (handbook 15-github step 2); no webhook, no callback URL.
- Executor host bring-up on kenta-server per docs/executor-host.md (amd64 image builds only there); logins (`claude setup-token` ×2, `codex login --device-auth`, `gh auth login`) are human steps, tokens only in the gitignored executor.env.
- Add --strict-mcp-config --mcp-config .mcp.json to the f orch launcher; decide security_paths width.

## Next work
1. Verify goal acceptance 3 and codex_reply on the restarted server (one tiny Codex task).
2. Phase D acceptance 2 on the first autonomous goal run (planner_runs per goal in the scorecard, Planner tokens per invocation far below the 11.2M session baseline).
3. Flip [jev].gate_mode to block once a week of gate.jsonl shows the waste ratio and false-block rate (F2 made the noconf rule possible); let the daemon act on Jev triage once E8 agreement is above 0.9 over 30 decisions.
4. kgpt side of the orchestrator module (C-K1..C-K4) on the kgpt bus (ORCH_ROOT=/Users/k3ntaw/code/kgpt).

## Phase C architecture (decided)
Two processes. (1) kgpt `modules/orchestrator`, thin MCP module on the shared image, runs_on home, owns per-user `orchestrator_connections` (endpoint_url, label, encrypted bearer), tools list_goals/goal_status READ and start_goal/cancel_goal WRITE_EXTERNAL (proposals), forwards to the user's endpoint with X-KGPT-User-Id applied by UserContextMiddleware. (2) `orchestrator serve` in its own container on kenta-server (ubuntu:24.04 image from this repo: claude native installer, codex release binary, uv, git, gh), one ORCH_ROOT per repo under /work, `goal start` = install+commit scaffold + GOAL task + headless Planner launch; bearer per endpoint; per-slug locks.

## Phase D architecture (decided)
The daemon owns the loop; the Planner becomes a function of decision points (planner_runs.py): task held, scout fan-out done, goal closable. Each decision is a fresh headless `claude -p` launched by goals.launch_planner with an ids-only prompt, recorded in planner_runs.json, reconciled when dead, terminal gave_up after two failures. Never while an interactive session is registered in planner_session.json. Review cost is bounded by pool.toml [review]; scout cost by pool.toml scout limits; Planner session cost by the 150k handover rule.

## Prior art (ids for recall.sh get)
- Phase C retrospective and D3/Phase D retrospective: decisions.md 2026-09-18/19 entries (goal T-0073, T-0109). Gotchas of 2026-09-18/19 in gotchas.md (stale checkout, spawn_spec_review contract, dispatch break, pre-stamp rule, docker builds outlive executors, amd64 emulation impossible here, claude CLI vanished).
- bus:T-0066..T-0069 kgpt scouts; T-0074..T-0077 Phase C scouts.
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt (compose project), home `ssh k3ntaw@192.168.1.167` (fish shell, pipe scripts via `bash -s`), kgpt home side /opt/kgpt-home.

