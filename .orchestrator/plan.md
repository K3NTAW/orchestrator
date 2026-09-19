# plan.md — Planner checkpoint (updated 2026-09-19 15:00, session 3; written for a fresh session)

## State in one paragraph
Open goal: T-0201 Phase E token diet 2 on branch goal/T-0201 (cut from goal/T-0109 head 25404d4, 2026-09-19 14:55). Phase C (PR 7) and Phase D (PR 8) wait for the human. Codex cools until about 16:30; execution runs on the sonnet fallback (opus for E1 and E3). The running MCP server still loads goal/T-0073 code: it dispatches fallback, spec-reviews c5+, and code-reviews c4+ on the other tier; restart the session right after E1 (T-0210) merges so the new review policy is live for the Jev tasks.

## Phase E task graph (parent T-0201; scopes disjoint; merge target goal/T-0201)
- Polish, no deps, c2-3, merge directly at a green gate: T-0202 P1 goals/serve/cli · T-0203 P2 pool tally · T-0204 P3 handover · T-0205 P4 daemon (dispatch continue, decision loop, cfg types, handover only with open goal) · T-0206 P5 planner_runs · T-0207 P6 scorecard · T-0208 P7 spawn which(claude) + spawn tools refuse wrong role · T-0209 P8 image notes.
- T-0210 E1 review policy (c5 opus, deps P4): [review] code_review = never|security_paths|always, security_paths globs, security_review_tier; gate merges directly unless the diff touches a security path; pipeline.review_reason. Old daemon will spec-review it and then run one opus review; accept that.
- T-0211 E5 compact bus_read rows (c3, no deps). T-0213 E1b docs (c2, deps E1, E5). T-0212 E7 worker turn diet: allowedTools, .mcp.worker.json, budget lines, turns in run log (c3, deps P7).
- T-0214 E2 Jev client jev.py (c4, deps E1, E5): fail-open, redact(), daily budget, [jev] table disabled by default, [secrets.jev] TYPESAFE_API_KEY.
- T-0215 E3 jev-gate PreToolUse hook (c5 opus, deps E2): log mode default, block mode on P(redundant) 0.85+ or P(needed) 0.15- with confidence 0.6+; never blocks tests, commit, bus post.
- T-0216 E6 jev_rank for recall.py --goal and handover pruning (c4, deps E2, P3). T-0217 E8 shadow triage of decision points with agreement tracking (c4, deps E2, P5, P1). T-0218 E4 scorecard waste_pct, blocked, turns, jev footer (c3, deps E3, E7, P6).
- Jev API (docs.typesafe.ai 2026-09-19): POST api.typesafe.ai/v1/systemone, Bearer key, questions noul|choice|score, 64k tokens per request, 0.042 USD per M input tokens. Adopters: LiteLLM guardrail (threshold 0.2), jev-compactor (drop at 0.7), pi jev-prune.
- Human steps for Phase E: put TYPESAFE_API_KEY in the Keychain under the f tok name typesafe and flip [jev].enabled = true after E2 merges; add strict MCP config flags to the f orch launcher (protected path) so the Planner session stops loading unrelated MCP servers and plugins every turn; okay the egress (task specs, tool-call metadata, memory titles go to TypeSafe; file contents never).
- Cost guard: 17 tasks at roughly 1 USD each plus 3 spec reviews and up to 4 old-daemon code reviews; stop and report if goal spend passes 35 USD (scorecard --by goal from a goal/T-0109 worktree).
- After all merge: verify acceptance 2 of T-0201 by hand (a c5 non-security task merges with zero reviews under the restarted server), retrospective, close T-0201, PR goal/T-0201 to main (after PR 8).

## Read first
1. `bus_read(status_not="done")` then skill `resume`. Phase E tasks T-0202..T-0218 are live; intervene only on held/failed ones.
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

## Next work after Phase E
1. Verify Phase D acceptance 2 on the first autonomous goal run (planner_runs per goal in the scorecard, Planner tokens per invocation far below the 11.2M session baseline).
2. Flip [jev].gate_mode to block once a week of jev-gate.jsonl shows the waste ratio and false-block rate; let the daemon act on Jev triage once E8 agreement is above 0.9 over 30 decisions.
3. kgpt side of the orchestrator module (C-K1..C-K4) on the kgpt bus (ORCH_ROOT=/Users/k3ntaw/code/kgpt).

## Phase C architecture (decided)
Two processes. (1) kgpt `modules/orchestrator`, thin MCP module on the shared image, runs_on home, owns per-user `orchestrator_connections` (endpoint_url, label, encrypted bearer), tools list_goals/goal_status READ and start_goal/cancel_goal WRITE_EXTERNAL (proposals), forwards to the user's endpoint with X-KGPT-User-Id applied by UserContextMiddleware. (2) `orchestrator serve` in its own container on kenta-server (ubuntu:24.04 image from this repo: claude native installer, codex release binary, uv, git, gh), one ORCH_ROOT per repo under /work, `goal start` = install+commit scaffold + GOAL task + headless Planner launch; bearer per endpoint; per-slug locks.

## Phase D architecture (decided)
The daemon owns the loop; the Planner becomes a function of decision points (planner_runs.py): task held, scout fan-out done, goal closable. Each decision is a fresh headless `claude -p` launched by goals.launch_planner with an ids-only prompt, recorded in planner_runs.json, reconciled when dead, terminal gave_up after two failures. Never while an interactive session is registered in planner_session.json. Review cost is bounded by pool.toml [review]; scout cost by pool.toml scout limits; Planner session cost by the 150k handover rule.

## Prior art (ids for recall.sh get)
- Phase C retrospective and D3/Phase D retrospective: decisions.md 2026-09-18/19 entries (goal T-0073, T-0109). Gotchas of 2026-09-18/19 in gotchas.md (stale checkout, spawn_spec_review contract, dispatch break, pre-stamp rule, docker builds outlive executors, amd64 emulation impossible here, claude CLI vanished).
- bus:T-0066..T-0069 kgpt scouts; T-0074..T-0077 Phase C scouts.
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt (compose project), home `ssh k3ntaw@192.168.1.167` (fish shell, pipe scripts via `bash -s`), kgpt home side /opt/kgpt-home.

## Auto-handover 2026-09-19T11:39:29+02:00 — daemon tick

### T-0201 GOAL Phase E: token diet 2 — no default code review, Jev-gated tool calls, Jev-ranked Planner inputs, worker turn diet, polish backlog
- queued: T-0206 P5: planner_runs.py polish from approving review T-0200 (held_at freshness, session file shape, interrupt handling, clean-exit scoring) (depends_on=[]), T-0207 P6: scorecard.py polish from approving review T-0195 (malformed jsonl, runs without usd, integer tokens) (depends_on=[]), T-0208 P7: spawn.py and mcp.py polish — hold when the claude CLI is missing, spawn tools refuse ids of the wrong role (depends_on=[]), T-0209 P8: executor image polish from approving security review T-0184 (venv protection, gh pin and gitconfig mount, frozen uv) (depends_on=[]), T-0210 E1: review policy — no code review by default, one cheap review only for security-touching diffs, spec review stays (depends_on=['T-0205']), T-0211 E5: bus_read returns compact rows by default; full task only on request (depends_on=[]), T-0212 E7: worker turn diet — allowedTools per role, bus-only MCP config, tool-call budget lines in prompts (depends_on=['T-0208']), T-0213 E1b: docs for the new review policy — CLAUDE.md, planner.md, README, orchestrate skill (depends_on=['T-0210', 'T-0211'])
- running: T-0202 P1: goals.py, serve.py and cli polish from approving reviews T-0137 and T-0161 (executor=claude:sonnet, started=2026-09-19T11:37:29+02:00), T-0203 P2: pool.py planner-usage tally polish from approving review T-0175 (executor=claude:sonnet, started=2026-09-19T11:37:29+02:00), T-0204 P3: handover.py polish from approving review T-0180 (gitignore state, throttle under the flock, truncation test) (executor=claude:sonnet, started=2026-09-19T11:37:29+02:00), T-0205 P4: daemon.py polish — dispatch continue instead of break, decision-point loop, review cfg type checks, handover only with an open goal (executor=claude:sonnet, started=2026-09-19T11:37:59+02:00)

Worktrees: wt/T-0002, wt/T-0006, wt/T-0007, wt/T-0008, wt/T-0009, wt/T-0010, wt/T-0012, wt/T-0013, wt/T-0014, wt/T-0016, wt/T-0018, wt/T-0021, wt/T-0022, wt/T-0023, wt/T-0026, … and 87 more

Last events:
- 2026-09-19T11:38:07+02:00 T-0209 created {}
- 2026-09-19T11:38:48+02:00 T-0210 created {}
- 2026-09-19T11:38:56+02:00 T-0211 created {}
- 2026-09-19T11:39:08+02:00 T-0212 created {}
- 2026-09-19T11:39:23+02:00 T-0213 created {}

Resume: skill resume; re-spawn held spec reviews; dispatch ready execute tasks by hand while Codex cools.
