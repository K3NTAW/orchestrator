# plan.md — Planner checkpoint (updated 2026-09-19 14:20, session 3; written for a fresh session)

## State in one paragraph
No open goal. Phase C (goal/T-0073, PR 7) and Phase D (goal/T-0109, PR 8) are code-complete and wait for the human to merge, in the order PR 6 → PR 7 → PR 8. The main checkout sits on goal/T-0073. The running MCP server still loads pre-D3 code; a fresh `f orch` session after PR 8 merges (or a restart on goal/T-0109) gets the decision-point Planner, the D1 review policy and the scorecard by goal. Codex cooled until about 2026-09-19 16:00; both Claude accounts idle.

## Read first
1. `bus_read(status_not="done")` then skill `resume`. Expect only old superseded/failed tasks; nothing queued, held or running.
2. Planner commits from the main checkout: `git status --short` first; stage only .orchestrator/ paths; commit with `git commit -F <scratchpad file>` (the co-author line's `>` trips planner-mode.sh). record.sh facts and any bash text must avoid backticks, `>`, and the words the hook treats as writes (cp, rm, mv, install, tail with a pipe).
3. Tool contracts: `spawn_spec_review(<spec_review task id>)` (given an execute id it RUNS AN EXECUTE); `spawn_scout(<execute id>)` runs the fallback executor; `spawn_review(<review task id>)`; `merge(task_id, target)`. A fix round whose branch carries unreviewed c≥4 work must be pre-stamped `pipeline.gated_at` or the daemon auto-merges it at c≤3. Specs, scope and depends_on are immutable on the bus (recreate the task; mark the superseded one done+merged_into when the replacement merges). merge() stamps merged_into only on the task it was called with: mark the chain by hand with bus.update.
4. Review policy in force (D1): 1–3 hooks only; 4–6 one review on the non-executing model; 7–10 two reviews (cross-model when Codex executed, both on the non-executing Claude tier otherwise) + security checklist; spec review from complexity 6 on sonnet. Review budget 3.0 USD.
5. If the checkout goes stale after a merge into the checked-out branch: `git diff --name-only HEAD -- . ':!.orchestrator'` then `git checkout HEAD -- <those paths>`, never `-- .`.
6. Restart rule: hand over at ~150k context and only when no worker is running.
7. The daemon appends an "Auto-handover" section to this file every 15 min while a goal is open; drop it when rewriting.

## Human items outstanding
- Merge PR 6 (install scaffold): https://github.com/K3NTAW/orchestrator/pull/6, then PR 7 (Phase C): https://github.com/K3NTAW/orchestrator/pull/7, then PR 8 (Phase D): https://github.com/K3NTAW/orchestrator/pull/8. Each builds on the previous branch history.
- GitHub App: add permission "Pull requests: read and write"; private key + App ID via `make secrets-edit` on the Hetzner box (handbook 15-github step 2); no webhook, no callback URL.
- Executor host bring-up on kenta-server per docs/executor-host.md (amd64 image builds only there); logins (`claude setup-token` ×2, `codex login --device-auth`, `gh auth login`) are human steps, tokens only in the gitignored executor.env.
- The claude CLI vanished from this machine on 2026-09-19 (homebrew symlink dangling) and was back by 14:00 (2.1.278). If Claude workers die with no run record again, check `which claude` first (gotcha recorded).

## Next work (pick after the PRs merge, or on user request)
1. Verify Phase D acceptance 2: run one real goal with the daemon on the merged code and autonomous decisions on; `orchestrator scorecard --by goal` must show planner_runs for that goal and per-invocation Planner tokens well below the 11.2M session baseline (2026-09-18 measurement: 411 turns, avg context 257k). Record the number in decisions.md.
2. Polish backlog, c2–3 each, one spec per cluster, target the branch that owns the file:
   - goals.py/serve.py (T-0137, T-0161 notes): TOMLDecodeError guard on the target pool.toml; Popen FileNotFoundError after the scaffold commit; single-goal guard under the goals lock; cli prints the note field; `git clone` argv gets `--` before git_url; missing repos.toml → 503; get_goal/cancel_goal/create_goal duplicate check under the guard; _safe_reason redacts more than URL credentials; no long flock holds in the shared threadpool.
   - pool.py planner usage (T-0175 notes): missing-dir continue skips the rollover save; now= half honoured; null usage values raise; planner_usage.json lacks day/window anchors.
   - handover.py (T-0180 notes): gitignore handover_state.json; throttle under the flock; section-wide truncation branch untested; daemon handover dirties tracked plan.md every 15 min (consider writing only when a goal is open, or committing it).
   - daemon.py (gotcha 17:30, T-0172, T-0200 notes): dispatch loop `break` on zero slots → `continue`; decision-point `break` after every run() starves later points; empty hold_reason path with fewer review records than expected; _load_review_cfg type checks; merge_reviewed walks every done execute task each tick.
   - planner_runs.py (T-0200 notes): _held_at keeps the first stage stamp when a task is held twice; non-dict planner_session.json counts as a failed launch; except BaseException swallows KeyboardInterrupt between claim and record; a decision Planner that legitimately posts no fix round is scored exited_early.
   - scorecard.py (T-0195 notes): guard json.loads on jsonl lines; Codex runs log no usd so the percent split undercounts them; float total tokens.
   - spawn.py: run_claude checks shutil.which('claude') first and holds the task with reason 'claude CLI not found' instead of dying in a daemon thread; spawn tools refuse ids of the wrong role.
   - C-O5 image (T-0184 notes): .venv writable executed code not in protected paths; gh auth setup-git gitconfig not mounted; gh never version-checked; entrypoint runs uv without --frozen.
3. kgpt side of the orchestrator module (C-K1..C-K4) on the kgpt bus: /Users/k3ntaw/code/kgpt/.orchestrator (ORCH_ROOT=/Users/k3ntaw/code/kgpt), plan there in kgpt/.orchestrator/plan.md. Phase B T-0003..T-0011 queued with depends_on; backlog goals T-0012 computer use, T-0013 studio.
4. Consider opus as executor tier for daemon.py/serve.py/planner_runs.py work, or wait for Codex: in sessions 2–3 every sonnet delivery of complexity 5–6 needed 1–5 opus fix rounds and reviews cost as much as execution (T-0073: 62.59 USD, 45 percent review; T-0109: 33.48 USD, 44 percent review).

## Phase C architecture (decided)
Two processes. (1) kgpt `modules/orchestrator`, thin MCP module on the shared image, runs_on home, owns per-user `orchestrator_connections` (endpoint_url, label, encrypted bearer), tools list_goals/goal_status READ and start_goal/cancel_goal WRITE_EXTERNAL (proposals), forwards to the user's endpoint with X-KGPT-User-Id applied by UserContextMiddleware. (2) `orchestrator serve` in its own container on kenta-server (ubuntu:24.04 image from this repo: claude native installer, codex release binary, uv, git, gh), one ORCH_ROOT per repo under /work, `goal start` = install+commit scaffold + GOAL task + headless Planner launch; bearer per endpoint; per-slug locks.

## Phase D architecture (decided)
The daemon owns the loop; the Planner becomes a function of decision points (planner_runs.py): task held, scout fan-out done, goal closable. Each decision is a fresh headless `claude -p` launched by goals.launch_planner with an ids-only prompt, recorded in planner_runs.json, reconciled when dead, terminal gave_up after two failures. Never while an interactive session is registered in planner_session.json. Review cost is bounded by pool.toml [review]; scout cost by pool.toml scout limits; Planner session cost by the 150k handover rule.

## Prior art (ids for recall.sh get)
- Phase C retrospective and D3/Phase D retrospective: decisions.md 2026-09-18/19 entries (goal T-0073, T-0109). Gotchas of 2026-09-18/19 in gotchas.md (stale checkout, spawn_spec_review contract, dispatch break, pre-stamp rule, docker builds outlive executors, amd64 emulation impossible here, claude CLI vanished).
- bus:T-0066..T-0069 kgpt scouts; T-0074..T-0077 Phase C scouts.
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt (compose project), home `ssh k3ntaw@192.168.1.167` (fish shell, pipe scripts via `bash -s`), kgpt home side /opt/kgpt-home.
