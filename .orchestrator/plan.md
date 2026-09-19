# plan.md — Planner checkpoint (updated 2026-09-19 16:50 machine time, session 3; written for a fresh session)

## State in one paragraph
No open goal. Phase E (goal/T-0201, PR 9) is code-complete: 34 commits over goal/T-0109, 384 tests green, retrospective in decisions.md. PRs wait for the human in order 6, 7, 8, 9. The main checkout is ON goal/T-0201 and in sync. RESTART THE SESSION before any new goal: the running MCP server still executes pre-Phase-E daemon code (it reviewed every c4+ task on opus today and never posts Codex results). A fresh f orch loads: security_paths review policy, Jev client (enabled, key in the Keychain), jev-gate hook in log mode, compact bus_read, worker allowedTools and bus-only MCP config, P9 hygiene. Codex is available again (astra executed the last three tasks).

## Phase E closed 2026-09-19 (goal T-0201, PR 9)
- Merged: P1-P10 (T-0202..T-0209, T-0235 P9, T-0236 P10), E1 T-0220+T-0223+T-0225, E1b T-0213, E2 T-0214+T-0227, E3 T-0229+T-0237, E5 T-0211, E6 T-0216+T-0233+T-0238, E7 T-0212, E8 T-0217+T-0234, E4 T-0230. Superseded: T-0210, T-0215, T-0218, T-0222. Cost 38.93 USD (review 14.6 percent).
- Verify on the first restarted-server workers: (a) runs/jev/gate.jsonl gains rows per worker tool call (the hook works from ROOT: p_needed 0.46 in 704 ms); (b) a non-security task merges with review_reason none and zero review tasks; (c) `orchestrator scorecard --by goal` shows waste_pct and turns.
- Backlog from this phase (spec when asked, c2-3 each): P11 daemon._dispatch_worker posts Codex results (gotcha 16:20); jev-gate does two uv round trips per tool call (T-0239 note: fold the --enabled check into the scorer or a cached flag file); Jev noul answers carry confidence null so block mode never fires: read the docs on confidence, or gate on probability extremes alone; narrow security_paths for this repo if the user wants fewer sonnet reviews; worker sessions start orchestrator.mcp from their worktree (pre-E7) and may run a stale daemon under the shared lock.
- Human items: merge PRs 6, 7, 8, 9 in order; add --strict-mcp-config --mcp-config .mcp.json to the f orch launcher; decide on security_paths width for this repo.

## Read first
1. `bus_read(status_not="done")` then skill `resume`. Expect only superseded/failed leftovers; nothing queued, held or running.
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

## Auto-handover 2026-09-19T16:39:51+02:00 — daemon tick

### T-0201 GOAL Phase E: token diet 2 — no default code review, Jev-gated tool calls, Jev-ranked Planner inputs, worker turn diet, polish backlog
- running: T-0239 review: E3 fix round 1: rebase the committed gate hook onto goal/T-0201 with the test fix, re-gate (T-0229 held gate_red) (executor=?, started=2026-09-19T16:39:22+02:00)
- held: T-0217 E8: Jev shadow triage of decision points — log what Jev would decide before any Planner launch, no behaviour change (hold_reason=review request_changes, resume_hint_keys=[]), T-0229 E3 v2: Jev tool-call gate — PreToolUse hook scores every worker tool call for necessity, logs to runs/jev/gate.jsonl, blocks only in block mode (supersedes T-0215 after spec review T-0228) (hold_reason=gate_red, resume_hint_keys=['failures'])
- failed: T-0215 E3: Jev tool-call gate — PreToolUse hook scores every worker tool call for necessity, logs always, blocks only in block mode (reason=superseded by E3 v2 (next id after T-0228): spec review risks folded in (runs/jev/gate.jsonl path, transcript JSONL schema, dead bus-tool branch). Mark done+merged_into goal/T-0201 when v2 merges., resume_hint_keys=[]), T-0218 E4: scorecard shows tool-call waste per task and goal from jev-gate.jsonl, plus turns per run (reason=superseded by E4 v2: its spec named the old jev-gate.jsonl path; re-pointed at E3 v2 and runs/jev/gate.jsonl, resume_hint_keys=[]), T-0222 E1 fix round 1: security_paths cover the MCP surface, prompts and skills; NUL-safe diff; real git tests; empty list fails closed (from review T-0221) (reason=Planner: the daemon cut wt/T-0222 from goal/T-0201 before the Planner could base it on task/T-0220, so the worker started without the E1 v2 commit; superseded by the next fix-round task whose first step resets onto task/T-0220, resume_hint_keys=[])
- done (not merged): T-0219 spec review: E1: review policy — no code review by default, one cheap review only for security-touching diffs, spec review stays, T-0221 review: E1 v2: review policy — no code review by default, one cheap review only for security-touching diffs, fail-closed diff, spec review stays (supersedes T-0210 after spec review T-0219), T-0224 review: E1 fix round 1 (v2): security_paths cover the MCP surface, prompts and skills; NUL-safe diff; real git tests; empty list fails closed (from review T-0221), T-0226 review: E2: Jev client — orchestrator/jev.py typed questions against TypeSafe AI System One, fail-open, budgeted, T-0228 spec review: E3: Jev tool-call gate — PreToolUse hook scores every worker tool call for necessity, logs always, blocks only in block mode, T-0231 review: E6: Jev relevance ranking for memory recall and the handover snapshot — prune what the Planner reads, keep the rest verbatim, T-0232 review: E8: Jev shadow triage of decision points — log what Jev would decide before any Planner launch, no behaviour change, T-0237 E3 fix round 1: rebase the committed gate hook onto goal/T-0201 with the test fix, re-gate (T-0229 held gate_red)
- merged: T-0202 P1: goals.py, serve.py and cli polish from approving reviews T-0137 and T-0161 (sha=f5a899e3), T-0203 P2: pool.py planner-usage tally polish from approving review T-0175 (sha=0c9b1a08), T-0204 P3: handover.py polish from approving review T-0180 (gitignore state, throttle under the flock, truncation test) (sha=02463d13), T-0205 P4: daemon.py polish — dispatch continue instead of break, decision-point loop, review cfg type checks, handover only with an open goal (sha=?), T-0206 P5: planner_runs.py polish from approving review T-0200 (held_at freshness, session file shape, interrupt handling, clean-exit scoring) (sha=90da599b), T-0207 P6: scorecard.py polish from approving review T-0195 (malformed jsonl, runs without usd, integer tokens) (sha=c31e6851), T-0208 P7: spawn.py and mcp.py polish — hold when the claude CLI is missing, spawn tools refuse ids of the wrong role (sha=ae051556), T-0209 P8: executor image polish from approving security review T-0184 (venv protection, gh pin and gitconfig mount, frozen uv) (sha=8517e922), … and 16 more

Worktrees: wt/T-0002, wt/T-0006, wt/T-0007, wt/T-0008, wt/T-0009, wt/T-0010, wt/T-0012, wt/T-0013, wt/T-0014, wt/T-0016, wt/T-0018, wt/T-0021, wt/T-0022, wt/T-0023, wt/T-0026, … and 95 more

Last events:
- 2026-09-19T16:39:21+02:00 T-0237 update {"pipeline": {"dispatched_at": 1789826494.7570748, "gated_at": 1789828761.532011
- 2026-09-19T16:39:21+02:00 T-0239 created {}
- 2026-09-19T16:39:22+02:00 T-0239 update {"status": "running", "assigned_to": "claude:B", "worktree": "/Users/k3ntaw/code
- 2026-09-19T16:39:22+02:00 T-0239 update {"account": "B"}
- 2026-09-19T16:39:22+02:00 T-0239 update {"pid": 35386, "account": "B"}

Resume: skill resume; re-spawn held spec reviews; dispatch ready execute tasks by hand while Codex cools.
