# plan.md — Planner checkpoint (handover 2026-09-18, written for a fresh session)

## Read first
1. `bus_read(status_not="done")` then skill `resume`. Codex cools until 2026-09-19 ~14:00; until then execute tasks are dispatched by hand with `spawn_scout(<execute id>)` (runs the claude:sonnet fallback in its worktree). The running daemon in the MCP server still has the old free_slots code; the fixed code is merged in goal/T-0073 but only live after this restart, so check whether the daemon now dispatches on its own before dispatching by hand (watch `pipeline.dispatched_at`).
2. Main checkout is on goal/T-0073. goal/T-0065 (PR 6) is pushed and awaits the human. goal/T-0109 (Phase D) is cut from goal/T-0073 and has no commits yet.
3. Review policy until D1 merges: one review per fallback-executed task, on the model that did not execute (the old daemon still spawns a sonnet review; if the executor was sonnet, mark that review failed and create an opus review with inputs=[task]). Spec review from complexity 5 (old daemon) — for tasks the Planner already re-specced, set `spec_review_verdict="approve"` via bus.update to waive.
4. Fix rounds: cut the worktree from the held task's branch (`spawn.ensure_worktree(new_id, base="task/<held>")`) before spawning; after the fix round merges, mark the original done+merged by hand (rebased commits lose branch ancestry; gotcha recorded).
5. Restart rule (own decision): hand over at 150k context. This session ended at about 450k and 411 turns.

## Open goals and tasks (all execute tasks are sonnet fallback unless Codex is back)
Phase C — goal T-0073 (orchestrator repo, branch goal/T-0073). Merged: T-0079 C-O2a auth/secrets · T-0078+T-0087+T-0090 C-O1 notify webhook · T-0083+T-0102 C-O6 daemon fallback slots and review tier · config commit 523f438 (spec_review in role_affinity).
- T-0115 C-O3 v4 goal runner (c6): was running at handover (pid 75992, started 1 min before). After restart the daemon requeues it (dead pid) → `spawn_scout("T-0115")` again. Spec review waived (verdict set); needs one opus code review when done (executor sonnet).
- T-0116 C-O4 serve (c6, deps T-0115) → spec review by the daemon on sonnet is fine; then dispatch.
- T-0117 C-O5 executor image + runbook (c5, deps T-0079, T-0116). Acceptance includes `docker build` locally.
- T-0118 C-O7a planner usage tally + `pick` (c5, deps T-0083, T-0116).
- T-0119 C-O7b `handover` command (c4, deps T-0118).
- Then: retrospective for T-0073 (record.sh draft T-0073), close the GOAL task (`orchestrator post T-0073 --summary ...`), PR goal/T-0073 → main.
Phase D — goal T-0109 (branch goal/T-0109 from goal/T-0073): D1 T-0120 review policy (deps T-0102, T-0115) · D2 T-0121 scout budget (deps T-0115) · D3 T-0122 planner_runs decision points (deps T-0115, T-0119, T-0102) · D4 T-0123 session rules incl. 150k restart (deps T-0115, T-0119) · D5 T-0124 scorecard by task/goal (deps T-0116, T-0118). Merge target goal/T-0109; when a D task and a C task both touch daemon.py, the C one goes first.
kgpt — its own bus at /Users/k3ntaw/code/kgpt/.orchestrator (ORCH_ROOT=/Users/k3ntaw/code/kgpt). GOAL T-0001; T-0002 ruff exclude merged; Phase B T-0003..T-0011 (B0b-api, B0b-web, B0, B1, B2a, B2b, B3, B4, B5) queued with depends_on; backlog goals T-0012 computer use, T-0013 Higgsfield-style studio. Branch goal/T-0001 pushed to origin. Plan there: kgpt/.orchestrator/plan.md (Phase B facts and specs, B0b, Phase C kgpt side C-K1..C-K4, backlog). A kgpt Planner session starts from the kgpt root with ORCH_ROOT set; its daemon autostarts with the MCP server.

## Human items outstanding
- Merge PR 6 (orchestrator install) when reviewed: https://github.com/K3NTAW/orchestrator/pull/6
- GitHub App: add permission "Pull requests: read and write"; private key + App ID via `make secrets-edit` on the Hetzner box (handbook 15-github step 2); no webhook, no callback URL; client secret unused.
- Executor host decided: own container on kenta-server; C-O5 writes the runbook; logins (`claude setup-token` ×2, `codex login --device-auth`, `gh auth login`) are human steps, tokens only in the gitignored executor.env on the server.
- Launcher (protected config dir): start the Planner on the account with headroom; `orchestrator pick planner` arrives with C-O7a. Account A's config dir now holds the second subscription after the /login on 2026-09-18.

## Phase C architecture (decided)
Two processes. (1) kgpt `modules/orchestrator`, thin MCP module on the shared image, runs_on home, owns per-user `orchestrator_connections` (endpoint_url, label, encrypted bearer), tools list_goals/goal_status READ and start_goal/cancel_goal WRITE_EXTERNAL (proposals), forwards to the user's endpoint with X-KGPT-User-Id applied by UserContextMiddleware. (2) `orchestrator serve` in its own container on kenta-server (ubuntu:24.04 image from this repo: claude native installer, codex release binary, uv, git, gh), one ORCH_ROOT per repo under /work, `goal start` = install+commit scaffold + GOAL task + headless Planner (`claude -p --mcp-config .mcp.planner.json --strict-mcp-config --append-system-prompt <planner.md>`), only ever opens PRs. Kernel reaches modules by URL only (kernel/kgpt_kernel/mcp/client.py:88-144); home modules gated by KGPT_HOME_AVAILABLE; ingress mcp-<name>.kentawaibel.com with an Access service-token policy.

## Measurements that drive Phase D (2026-09-18)
Planner session: 411 turns, 104M cache-read, 777k output, avg context 257k, max 446k, 112 monitor notifications, ≈11.2M by the pool metric (in + out + cache_read/10). Workers: 25 runs, 16.78 USD: scouts 4.59 (8 × sonnet, ~130 s), execute 4.56 (6 × sonnet, ~213 s), reviews opus 4.61 (4) + sonnet 1.43 (4), spec reviews 1.59. Every fallback task got two reviews. Three spec-review rounds on one task (C-O3) cost ≈3 USD and an hour; write specs against the CLI's real flags (`claude --help`: --append-system-prompt exists, -file does not) and real module boundaries.

## Gotchas recorded today (gotchas.md)
daemon never fired the Claude fallback (free_slots) → fixed C-O6/C-O6b · gate spawned same-model reviews → fixed C-O6 review_tier · spec_review missing from role_affinity → fixed in both pool.toml · rebased fix round hides the original from already_merged → manual bus.update, fix candidate open · planner-mode.sh flags any command text containing `>` or words like cp/rm/install/mv, and guardrails.sh any mention of the ssh or system config dirs, even inside ssh remote strings or record.sh facts.

## Prior art
- bus:T-0066..T-0069 kgpt scouts (projects model, github module, web client, tests/gate); T-0074..T-0077 Phase C scouts (home-module wiring, portability, connection pattern, headless auth).
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt (compose project), home `ssh k3ntaw@192.168.1.167` (fish shell, pipe scripts via `bash -s`), kgpt home side /opt/kgpt-home. Both healthy 2026-09-18 11:00; gateway saw zero chat requests in the prior 7 days.
