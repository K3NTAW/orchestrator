# plan.md — Planner checkpoint (updated 2026-09-19 23:58, session 5, Phase G closed; next goal: docs.kentawaibel.com)

## State in one paragraph
Phase G (T-0260, token economy, complexity 8) closed at 23:56: all eleven sub-goals merged on goal/T-0260 (head 2ca1517, 37 commits over goal/T-0240), tests-green exit 0 externally on wt/goal-T-0260, PR 11 https://github.com/K3NTAW/orchestrator/pull/11 open for the human after PR 10, retrospective in decisions.md 2026-09-19. No task under T-0260 is open. The main checkout stays on goal/T-0240 (the running MCP server loads Phase F code); switch to goal/T-0260 only after PR 11 is merged or the human says so. The next goal is the user's 23:46 request: create docs.kentawaibel.com with the same visual as kentawaibel.com and one entry for the orchestrator (see "Next goal").

## Next goal: docs.kentawaibel.com (requested 2026-09-19 23:46, not yet started on a bus)
Facts gathered (read-only, session 5):
- kentawaibel.com is the Vite + React 19 + TypeScript + Tailwind v4 site at /Users/k3ntaw/code/k3ntaw-portfolio (no git repo, deployed with the Vercel CLI; .vercel/project.json links project k3ntaw-portfolio, prj_HIXTMCLlDCM7lNDJ6MJgzxKv38Jk, team k3ntaws-projects). Design tokens live in src/index.css @theme: OKLCH near-black surfaces (bg 0.145, surface 0.205, border 0.32), cool white ink, one azure accent (oklch 0.72 0.15 255), Geist Variable and Geist Mono via @fontsource-variable, motion + lenis for animation, lucide-react icons; components Nav, Hero, Featured, Repos, Skills, About, Footer, CommandPalette.
- Domain: kentawaibel.com is registered at Vercel with Cloudflare nameservers (lisa/fonzie.ns.cloudflare.com, proxied); docs.kentawaibel.com already resolves to Cloudflare and Vercel answers DEPLOYMENT_NOT_FOUND, so no DNS change is needed: add docs.kentawaibel.com as a domain on a new Vercel project. Vercel CLI 54 is logged in (the human completed the device login at 23:47). No Cloudflare API tooling on this machine (cloudflared cert only); Keychain and ~/.config/f are guardrail-protected.
- Stack decision (house rules): new repo /Users/k3ntaw/code/docs-kentawaibel, Vite + React + TypeScript + Tailwind v4, the portfolio's @theme tokens copied verbatim, content as markdown files under content/ rendered at build (import.meta.glob + react-markdown + remark-gfm), sidebar of entries, first entry "Orchestrator" written from this repo's README, CLAUDE.md, skills/orchestrate and decisions.md (what it is, roles, lifecycle, bus, daemon, review policy, memory, cost numbers). Deploy: new Vercel project docs-kentawaibel, domain docs.kentawaibel.com. Approval gate for the human: creating the Vercel project and adding the domain are outward-facing; the Planner asks before running them.
- How to run it: this bus belongs to the orchestrator repo. Use `uv run orchestrator goal start <repo> "<text>"` (scaffolds .orchestrator in the target, creates the GOAL task, launches a headless Planner there), or install into the new repo and run a Planner session in it. Codex worktrees are per ORCH_ROOT, so the docs repo needs its own.

## Read first
1. `bus_read(status_not="done")` then skill `resume`. Nothing live under T-0260; leftovers are superseded/failed.
2. Planner commits from the main checkout (goal/T-0240): `git status --short` first; stage only .orchestrator/ paths; commit with `git commit -F <scratchpad file>`. planner-mode.sh scans the whole command text: keep backticks, `>`, `<`, and the words it treats as writes (cp, rm, mv, install, tail with a pipe, git rebase/checkout/reset/revert even inside quoted prose, .env) out of Bash text. Use absolute paths: a `cd wt/...` inside a command moves the session cwd. macOS has no `timeout`; use run_in_background instead.
3. Tool contracts: `spawn_spec_review(<spec_review task id>)`; `spawn_scout(<execute id>)` runs the fallback executor; `spawn_review(<review task id>)`; `merge(task_id, target)` skips the review policy. `codex(task_id, prompt)` and `codex_reply(task_id, delta)` return the result dict and do NOT post it: bus.post_result(tid, {summary, commit, executed_by, provenance:['repo'], usage}, 'done') by hand. Specs are immutable: a fix round is a new task with constraints.fix_round_for; mark the chain merged by hand (bus.update(original, status='done', merged_into=..., merged_via='fix round <id> <sha>', hold_reason=None)) until G7 v4 runs in the server.
4. Before trusting a Codex result whose acceptance names test ids: `git diff --stat HEAD~1` in the worktree must touch tests/; if not, one codex_reply listing the missing ids fixes it (five cases in Phase G).
5. Slot counters: pool_state.json running fields leak when a restart kills a Codex process; reset them by hand only when pgrep shows no codex exec and no claude -p worker, then confirm the next tick dispatches.
6. A review that shows status queued with a "process died; requeued" event never runs again by itself: spawn_review(<id>) by hand.
7. Restart rule: hand over at ~150k context or when an account's daily budget is reached, only when no worker is alive. Account A planner_day_tokens 35.2M over its 30M budget at 23:24; `orchestrator pick planner` says hold. Keep Planner turns short.
8. The daemon appends an "Auto-handover" section to this file every 15 min while a goal is open; drop it when rewriting.

## Human items outstanding
- Merge PRs 6, 7, 8, 9, 10, 11 in order: https://github.com/K3NTAW/orchestrator/pull/11 is Phase G.
- Decisions: latency target, security_paths width, daemon as a process (Phase F review); review budget (Phase G: 22.38 USD, review 63 percent, every review found a defect).
- Approve before the Planner runs them: `vercel project add docs-kentawaibel` and `vercel domains add docs.kentawaibel.com` for the docs goal.

## Backlog (c2-c3, orchestrator repo, after PR 11)
- Pool load derives executor running counters from bus tasks (status running, executor == id); drop the legacy codex mirror.
- codex and codex_reply MCP tools post the result to the bus when the task is running and assigned to codex.
- dispatch re-spawns queued review and spec_review tasks whose pipeline is empty.
- Gate checks that every tests/...::name in the acceptance resolves to a defined test.
- Consistent total_tokens for pre-G1a scorecard rows; recall ranking relevance.
- Switch the checkout to goal/T-0260 after PR 11 so the daemon runs leases, auto fix rounds and packets on itself; measure tokens per accepted goal against 5.96M.
- kgpt side of the orchestrator module (C-K1..C-K4) on the kgpt bus.

## Phase C architecture (decided)
Two processes. (1) kgpt `modules/orchestrator`, thin MCP module on the shared image, runs_on home, owns per-user `orchestrator_connections` (endpoint_url, label, encrypted bearer), tools list_goals/goal_status READ and start_goal/cancel_goal WRITE_EXTERNAL (proposals), forwards to the user's endpoint with X-KGPT-User-Id applied by UserContextMiddleware. (2) `orchestrator serve` in its own container on kenta-server (ubuntu:24.04 image from this repo: claude native installer, codex release binary, uv, git, gh), one ORCH_ROOT per repo under /work, `goal start` = install+commit scaffold + GOAL task + headless Planner launch; bearer per endpoint; per-slug locks.

## Phase D architecture (decided)
The daemon owns the loop; the Planner becomes a function of decision points (planner_runs.py): task held, scout fan-out done, goal closable. Each decision is a fresh headless `claude -p` launched by goals.launch_planner with an ids-only prompt, recorded in planner_runs.json, reconciled when dead, terminal gave_up after two failures. Never while an interactive session is registered in planner_session.json. Review cost is bounded by pool.toml [review]; scout cost by pool.toml scout limits; Planner session cost by the 150k handover rule.

## Prior art (ids for recall.sh get)
- Phase C, D, F, G retrospectives: decisions.md 2026-09-18/19 entries (goals T-0073, T-0109, T-0240, T-0260). Gotchas of 2026-09-19 in gotchas.md.
- bus:T-0066..T-0069 kgpt scouts; T-0074..T-0077 Phase C scouts; T-0261..T-0263 Phase G scouts.
- Hosts: Hetzner `ssh kgpt@46.62.167.12` dir /home/kgpt/kgpt (compose project), home `ssh k3ntaw@192.168.1.167` (fish shell, pipe scripts via `bash -s`), kgpt home side /opt/kgpt-home.
