# decisions

## 2026-09-17 Planner mode is pinned by f orch, not by CLAUDE.md alone
type: decision · goal: manual-2026-09-17 · provenance: repo
- CLAUDE.md:3 — a goal is any change to a repo including the orchestrator itself; first tool call is Skill(orchestrate)
- .claude/hooks/planner-mode.sh — PreToolUse floor: Planner writes only under .orchestrator/ and temp dirs; git commit/add/checkout -b/merge/rebase blocked; push and gh pr allowed because merge() does not push
- .claude/hooks/planner-prompt.sh — UserPromptSubmit reminder on every message; silent for /skill prompts and workers
- dotfiles/config/f/f.sh _f_planner_mode — appended system prompt; goal arg passed as /orchestrate <goal>
outcome: Chosen over trusting CLAUDE.md: on 2026-09-17 the Planner built the memory skill by hand despite the rule. Alternative rejected: --disallowedTools Edit,Write for the Planner, because plan.md and memory need writes and bash heredocs bypass it anyway. Escape hatch ORCH_PLANNER_MODE=0.

## 2026-09-17 Planner may commit, branch and push; still never edits source
type: decision · goal: T-0001 · tasks: T-0002,T-0003 · provenance: repo
- .claude/hooks/planner-mode.sh — git block list is now merge|rebase|cherry-pick|apply|am|reset --hard|filter-branch only (commit b4f03ea by claude:sonnet on account A via executor_fallback, Codex cooling)
- CLAUDE.md:11 Always item 11 — every Planner commit states what/why and gets a dated decisions.md entry naming the revert path
- tests/test_orchestrator.py Guardrails.test_edit_protected_path — path derived from protected-paths.txt (commit 6f6d8cb by claude:sonnet on account B); was checkout-relative and failed the merge gate in every worktree
- merge(T-0003, target=planner-mode) fast-forwarded 2064225..6f6d8cb; PR 2 carries all of it
outcome: Rollback: git revert 6f6d8cb b4f03ea on planner-mode, or close PR 2 unmerged. Alternative rejected: keep commits blocked and have the human commit; the user wants the Planner to do it and be accountable via this record. First real execute run: fallback path, worktree, scope-guard, merge queue all worked; two pre-existing defects found (see gotchas 2026-09-17).

## 2026-09-17 eval 001 passed: status --plain via the full lifecycle
type: decision · goal: T-0004 · tasks: T-0006,T-0011 · provenance: repo
- orchestrator/cli.py:22,30-38 — --plain prints one tab line per account and one for codex (commit 152bcb9 by claude:sonnet via executor_fallback)
- scout T-0006 (account B, 46 s, USD 0.26) located the insertion points; execute T-0011 first try green; merge into goal/T-0004 clean
outcome: Pipeline verified: spawn_scout → spec → fallback executor → external gate → serial merge → goal branch. Rollback: git revert 152bcb9 on goal/T-0004 or close the PR

## 2026-09-17 Benchmark input: Scrapling fetcher for artificialanalysis.ai, by user decision over the ToS concern
type: decision · goal: T-0005 · tasks: T-0007,T-0019,T-0022 · provenance: repo
- scout T-0007: no public API; Terms of Use forbid automated scraping or mining (0.85, provenance web)
- Planner proposed a hand-taken snapshot (T-0019); user reaffirmed on 2026-09-17 18:20: use Scrapling (github.com/D4Vinci/Scrapling)
outcome: T-0019 superseded; scout T-0022 checks the Scrapling API and how the page delivers its table; replacement B4 spec adds a low-frequency, identified fetcher (manual or at most daily), keeps hand-entry as fallback, marks data provenance web. Rollback: disable the fetch subcommand; bench.json stays hand-editable

## 2026-09-17 Benchmark fetch is identified and plain: no TLS impersonation, no stealth headers
type: decision · goal: T-0005 · tasks: T-0033,T-0038 · provenance: repo
- orchestrator/bench.py fetch_html — Scrapling Fetcher.get defaults to impersonate=chrome and stealthy_headers=True (fake referer); security review T-0033 flagged this as evasion contradicting the 2026-09-17 decision
- B4d sets stealthy_headers=False, no impersonation, User-Agent orchestrator-bench/1 (+github.com/K3NTAW/orchestrator), stores the real HTTP status and fails closed
outcome: Scraping was the user's call; disguising the client was never part of it. If the site refuses the identified client, we stop fetching and fall back to bench set by hand; we do not add impersonation back. Rollback: git revert the B4d commit

## 2026-09-17 Multi-model executor pool shipped on goal/T-0005: executors table, routing by complexity and live score, Scrapling benchmark input
type: decision · goal: T-0005 · tasks: T-0015,T-0016,T-0025,T-0017,T-0029,T-0032,T-0018,T-0034,T-0023,T-0036,T-0037,T-0038,T-0035,T-0024 · provenance: repo
- .orchestrator/pool.toml [[executors]] — astra (1-10), luna/terra/sol 5.6 (1-6) enabled; luna6/terra6/sol6 disabled placeholders; quota_group chatgpt; pace_reserve removed (361b7ce, 5cc7d0b)
- orchestrator/pool.py pick_executor(role, complexity, scores), cooldown_executor cools the quota group, codex_available(complexity); executor.py routes -m per executor and logs executor+complexity (a01dc3c)
- orchestrator/scorecard.py build/scores/write from runs+tasks, execute tasks only; merge() writes scorecard.json; bench.json prior for cold executors (1b5a17f, 815af4d, 763986d)
- orchestrator/bench.py Scrapling plain Fetcher, identified UA, no impersonation, RSC chunk parser, 20 h cap, fail closed; live: 200, 6/8 matched with intelligence/speed/price (67adc81, 019cb09, e29a97e, 913a5a9)
- spawn.py base_for: review worktrees off the reviewed branch, execute off goal/<parent>; review affinity on A; fit_result caps worker results; tests-green runs under uv in the target project (cd5048a, 3808258, 7b0e2ef, 09ef8d1)
outcome: Rollback: close PR goal/T-0005 unmerged, or git revert the listed shas in reverse order; dependency rollback uv remove scrapling curl_cffi playwright patchright browserforge. Rejected alternatives: hand-taken benchmark snapshot (user chose Scrapling); [fetchers] extra (browser stack); impersonated fetch (evasion)

## 2026-09-17 window_cap_tokens raised from 2M to 10M
type: decision · goal: T-0005 · provenance: repo
- .orchestrator/pool.toml:3 — cap hit in 2.2 h on both accounts (A 1.67M, B 2.03M counted; cache reads at one tenth dominate) with zero real rate limits in runs/2026-09-17.jsonl
- pool.py utilization() rolls the window after 5 h; pick() ceiling for workers on A is 65 percent because of the planner reserve
outcome: User set 10M on 2026-09-17 23:50. Alternative rejected: wait for the 01:40 roll (idle time). Real limits remain handled by parse_reset_hint cooldowns. Rollback: revert the commit or edit the value

## 2026-09-17 Daily token budgets raised to 30M (A) and 40M (B)
type: decision · goal: T-0043 · provenance: repo
- .orchestrator/pool.toml:10,17 — were 6M/8M, below the 10M window_cap_tokens; today's counted usage ran about 1.7M and 2.0M per 2.2 h
outcome: User set 30M/40M on 2026-09-18 00:55. Suite run before commit (67 OK). Rollback: revert the commit

## 2026-09-17 Parallel machine design: per-module tests, depends_on, daemon stages with pipeline stamps, spec review at complexity 5+
type: decision · goal: T-0043 · tasks: T-0044,T-0045,T-0046,T-0047,T-0048,T-0049,T-0050,T-0051 · provenance: repo
- scout T-0044: tests/test_orchestrator.py had two order couplings (Bus next_id T-0002 assertion; SpawnBase reusing MergeQueue's git repo) — the split removes them; no tests/__init__.py, bare from _harness import
- scout T-0045: daemon.tick only requeues dead pids; bus.update and Pool.save are unlocked read-modify-write — daemon gets flock on bus.lock and stage stamps (pipeline dict) for idempotency
- scout T-0046: spec_review as a new role mirroring review; verdict written on the execute task; hold via status held + hold_reason; spawn_spec_review tool
outcome: Order: C-A tests split first (T-0047), then C-B (T-0048) and C-D (T-0049) in parallel (disjoint scopes), C-C daemon (T-0050, opus + adversarial + security review) after both, C-E docs last. Rejected: reusing role review with a flag (branchy render), tests/__init__.py (changes discover import semantics). Rollback per task commit; goal PR is the gate

## 2026-09-17 Parallel machine shipped on goal/T-0043: per-module tests, depends_on, daemon stages, spec review
type: decision · goal: T-0043 · tasks: T-0047,T-0048,T-0049,T-0055,T-0050,T-0056,T-0051 · provenance: repo
- tests/_harness.py + nine test files (104fce1); order couplings removed; discover from tests/ without __init__.py
- orchestrator/bus.py depends_on/ready/dependents + bus.locked() flock (b85b742, daemon commit); bus_mcp passes depends_on and constraints
- orchestrator/daemon.py tick stages dispatch/gate/review/merge with pipeline stamps claimed under the lock, async dispatch, per-stage error holds, argv-safe notify (93a1b64, 9cb2114); 
- spec_review role + prompts/spec-review.md with numbered excerpts + spawn_spec_review; fit_result trims any list field (41df1ae, 70c0aa5)
outcome: Reviews requested changes 3 times (fit_result shape; daemon blocking dispatch + stamp-before-effect + notify injection); all fixed same branch. Daemon is opt-in until started. Rollback: close PR or revert in reverse order. Next: fresh Planner session so MCP servers load new code, then run the daemon alongside the Planner

## 2026-09-17 Pipeline daemon autostarts inside the orchestrator MCP server
type: decision · goal: T-0043 · tasks: T-0063,T-0064 · provenance: repo
- .orchestrator/pool.toml [daemon] autostart=true, interval_s=30; orchestrator/mcp.py calls daemon.start_background at boot inside try/except (34e9e5d)
- orchestrator/daemon.py acquire_lock on .orchestrator/daemon.lock keeps one instance across the autostart and the CLI; ORCH_DAEMON=0 opts out; stale() skips tasks of closed goals; already_merged() marks ancestor-merged fix-round originals (901306a)
outcome: Chosen over a launcher change in dotfiles (protected path, and a separate process to babysit): the daemon lives exactly as long as the Planner session. Rollback: autostart=false or revert the two commits

## 2026-09-18 orchestrator install: the orchestrator is scaffolded into a target repo as committed files, not run from its own repo against another
type: decision · goal: T-0065 · tasks: T-0070,T-0071,T-0072 · provenance: repo
- orchestrator/install.py — copies .orchestrator/{pool.toml,prompts,protected-paths.txt,memory,plan.md,tasks}, .claude/{hooks,settings.json,skills}, skills/ into the target; rewrites .mcp*.json to uv run --project (this repo) with env ORCH_ROOT=(target); appends ignore lines; writes .orchestrator/tests.sh when the target Makefile has test/lint/typecheck
- alternative that lost: point ORCH_ROOT at this repo and run from the target root (README wording before). Worktrees are cut with git worktree add and carry only committed files, so hooks, prompts and skills must be committed in the target
- first target: kgpt, installed 2026-09-18 14:05 from goal/T-0065 (d410503); executed by claude:sonnet as Codex fallback, reviewed by sonnet (daemon) and opus (T-0072, other account)
outcome: revert path: git revert d410503 in the orchestrator; in kgpt, revert the scaffold commit named in kgpt/.orchestrator/memory/decisions.md

## 2026-09-18 Phase C executor runs in its own container on kenta-server; credentials mounted from host files the human creates
type: decision · goal: T-0073 · provenance: repo
- user decision 2026-09-18 16:40: container on kenta-server (192.168.1.167, Ubuntu 20.04, Docker). Image from this repo (ubuntu:24.04 base with the claude native installer, the codex release binary, uv, git, gh), spec C-O5 T-0084
- alternatives that lost: the MacBook (not always on); a native setup on 20.04 (Codex binary glibc floor unconfirmed for glibc 2.31, credentials in a shared home)
- kgpt reaches a module by URL only (kernel/kgpt_kernel/mcp/client.py:88-144), so the executor service needs no place in the shared kgpt image; the thin kgpt module (modules/orchestrator, kgpt spec C-K2) forwards to it with a per-user bearer
outcome: revert path: stop the compose service on kenta-server and drop the kgpt module manifest; nothing in kgpt depends on it while KGPT_HOME_AVAILABLE gates home modules

## 2026-09-18 Phase D token diet opened; Planner sessions hand over at 150k context; one review per task
type: decision · goal: T-0109 · provenance: repo
- measured 2026-09-18: Planner session 411 turns, avg context 257k, 112 monitor notifications, about 11.2M tokens by the pool metric vs 2.5M for 25 worker runs (16.78 USD, reviews as costly as execution)
- specs D1-D5 (T-0120..T-0124) on goal/T-0109: review policy in pool.toml [review], scout budget, planner_runs decision points with [planner] autonomous=false default, session rules, scorecard by task and goal
- C-O3 goal runner needed three spec-review rounds (T-0081, T-0096/T-0097, T-0104) before v4 T-0115; the fourth round was waived by Planner decision
outcome: revert path: git revert the D commits on goal/T-0109 individually; policy text lives in CLAUDE.md and the orchestrate skill

## 2026-09-18 Restored daemon.py, spawn.py and their tests to 35bf7e2 after the handover commit 3130d61 reverted the C-O6/C-O6b fixes from a stale checkout
type: decision · goal: T-0073 · tasks: T-0083,T-0102,T-0115,T-0127 · provenance: repo
- orchestrator.merge fast-forwards goal/T-0073 from worktrees while the main checkout has that branch checked out; its index and working tree stay at the older commit, so a Planner commit from the main checkout that stages source paths commits the stale content as a revert (3130d61: daemon.py, spawn.py, tests/test_daemon.py, tests/test_spawn.py went back to 4a14ba7)
- symptom 2026-09-18 14:55: the fresh MCP server ran the reverted daemon, gated T-0115 and spawned review T-0127 on sonnet for a sonnet-executed task; free_slots also back to the version that never fires the Claude fallback
- restore: the four files taken from 35bf7e2 via git (no hand edits), suite 116 OK, committed from the main checkout; T-0127 marked failed, opus review spawned by hand
outcome: Revert path: revert the restore commit (that re-applies the accidental revert; not wanted). Rule for Planner commits from the main checkout: inspect status first and stage nothing outside .orchestrator/; if tracked source shows as modified without anyone editing it, the checkout is stale and must be brought back to HEAD before committing. Fix candidate for merge.py: after fast-forwarding a branch that the main worktree has checked out, sync that worktree when it is clean, else warn

## 2026-09-18 Review budget cap raised 1.5 to 3.0 USD; opus reviews get delta-only specs
type: decision · goal: T-0073 · tasks: T-0134,T-0137,T-0139 · provenance: repo
- .orchestrator/pool.toml limits.max_budget_usd.review was 1.5; T-0134 (opus, 389-line delta plus context) died at 1.58 USD after 277 s with nothing posted, then T-0137 with a delta-only spec finished at 1.53 USD and T-0139 approved under the new cap
- a review that dies at the cap costs more than the extra dollar: the full spend is lost and a retry doubles it
outcome: 3.0 for review from 2026-09-18 16:00; review specs name the exact diff range to read and say not to re-read the parent task's diff. Revert path: set review = 1.5 on line 138 of pool.toml

## 2026-09-18 C-O4 serve: spec review waived after three rounds; v4 spec dispatched directly
type: decision · goal: T-0073 · tasks: T-0116,T-0142,T-0144,T-0140,T-0143,T-0146 · provenance: repo
- three sonnet spec reviews (about 0.33 USD and 3 min each) found 15 risks in total, converging from design (requester field outside scope, 409 by string matching, async handlers) to hardening (per-slug clone lock, credential leak through git stderr, clone timeout, status() list shape); the round-3 reviewer was told approve-unless-blocking and still found a real leak, so the rounds paid off
- specs and depends_on are immutable on the bus, so each round recreated the task (T-0116, T-0142, T-0144, v4); dependents T-0117/T-0118/T-0124 still name T-0116 and will be satisfied by marking T-0116 done+merged_into when v4 merges (fix-round bookkeeping)
outcome: same rule as C-O3: after three spec-review rounds the Planner folds the last findings in and dispatches; the opus code review remains. Revert path: none needed (decision only); the v4 task can be failed and a v5 written if the code review shows the spec was wrong

## 2026-09-18 C-O4 serve endpoint merged: bearer-authed starlette API over goals.py, hardened through one security review and two fix rounds
type: decision · goal: T-0073 · tasks: T-0147,T-0152,T-0153,T-0155,T-0158,T-0161 · provenance: repo
- orchestrator/serve.py: plain-def handlers, hmac bytes compare with any exception mapped to 401, per-slug flock spanning clone-check + running-check + goals.start, clone as argv with timeout and redacted log-only stderr, body bound before buffering (Content-Length then streamed abort), every goals.* string redacted and capped before a response, 400/404/409/422/502/503/504 map, repo slug on every entry, cancel idempotent; goals.py gains requester
- reviews: T-0152 opus security (1.39 USD) found the non-ASCII header 500 and body bound gap; T-0155 (1.05) found refusal reasons leaking git stderr and the after-the-fact body check; T-0161 (0.88) approved with low notes (git clone needs a -- separator, missing repos.toml as 503, per-route guards)
- spec went through three sonnet spec reviews (T-0140, T-0143, T-0146) and four task recreations because specs are immutable on the bus
outcome: merged 6f8018d into goal/T-0073 2026-09-18 20:45 (rebased chain be42500 T-0147, 38db9bb T-0153, 6f8018d T-0158). Revert path: git revert the three commits on goal/T-0073 in reverse order. Backlog in plan.md: C-O4 polish task for the T-0161 notes

## 2026-09-18 D1 review policy merged into goal/T-0109 after five execute rounds and four opus reviews
type: decision · goal: T-0109 · tasks: T-0120,T-0151,T-0154,T-0157,T-0160,T-0169,T-0150,T-0159,T-0164,T-0172 · provenance: repo
- daemon.py: [review] table (spec_review_min 6, direct_merge_max 3, two_reviews_from 7, spec_review_tier sonnet) loaded per tick; reviews_expected(t) stamped at gate time; merge_reviewed sweeps done execute tasks with reviews, buckets verdicts exhaustively, holds on failed/held/rejected siblings with a reason, never raises per task; second review for 7-10 never on the executor's model (Claude-executed tasks get both reviews on the non-executing tier); CLAUDE.md, planner.md and README updated to the real rule
- each opus review found a real stall or mis-merge path the previous round had missed (orphaned two-review deadlock, all-failed reviews waiting forever, IndexError on off-vocabulary verdicts); the fix rounds cost about 2 USD of sonnet execution and 5.68 USD of opus review, the most expensive task of the day
- process lessons recorded in plan.md and gotchas: fix rounds whose branch carries unreviewed higher-complexity work must be pre-stamped gated_at or the daemon auto-merges them at c<=3; README Pipeline conflicts between D1 and D2 needed a rebase round
outcome: merged c593794 into goal/T-0109 2026-09-18 23:00 (chain 2e32566, 425775e, 8a82703, 2f37982, c593794 on top of 1b94384 D2). Revert path: git revert the five commits on goal/T-0109 in reverse order, or reset the branch to 1b94384 before any D3+ merge. The T-0172 low notes go to D3 or a D-polish task

## 2026-09-18 C-O7a merged: Planner transcript usage counted into the pool; orchestrator pick for the launcher
type: decision · goal: T-0073 · tasks: T-0167,T-0170,T-0174,T-0175 · provenance: repo
- pool.py: encode_project_dir (every non-alnum char to '-', verified against real projects/ names), tally_planner reads assistant lines incrementally by byte offset from <config_dir>/projects/<encoded ROOT>/*.jsonl, day and window counters gated independently, counters reset with the window/day rollover, state in .orchestrator/planner_usage.json under its own flock; utilization and the daily budget include planner usage; daemon.tick tallies first; cli pick tallies, prints id and expanded config_dir, exits 3 on stderr hold
- reviews: T-0170 (1.59 USD) caught the out-of-window lines being dropped from the day counter and the unexpanded ~ in pick; T-0175 (1.58) approved with low notes: missing-dir path skips the rollover save, tally still calls pool.save(), now= half honoured, null usage values raise, planner_usage.json lacks day/window anchors
outcome: merged f5d2a16 into goal/T-0073 2026-09-18 23:25 (chain 2cf1d45 T-0167, 8c65a97 T-0174). Revert path: git revert the two commits in reverse order. The launcher can now run 'orchestrator pick planner' to choose the account with headroom. T-0175 notes go to a C-O7 polish task

## 2026-09-18 C-O7b merged: orchestrator handover writes an Auto-handover checkpoint into plan.md; the daemon refreshes it every 15 minutes
type: decision · goal: T-0073 · tasks: T-0119,T-0177,T-0179,T-0180 · provenance: repo
- orchestrator/handover.py write(reason): under bus.locked(), builds the section (open goals, children grouped by status incl. failed and other, worktrees of non-merged tasks, last 5 events, fixed Resume sentence, truncated to 120 lines), writes a sibling temp file and os.replace()s plan.md; replaces the LAST Auto-handover heading; cli handover [--reason]; daemon.tick throttles via .orchestrator/handover_state.json
- reviews: T-0177 (0.93 USD) caught failed children vanishing from the snapshot and the non-atomic write; T-0180 (1.51) approved with notes: ROOT's tracked plan.md is dirtied every 15 min by the daemon, throttle check is outside the flock, handover_state.json not gitignored, section-wide truncation untested
outcome: merged fdf9d2f into goal/T-0073 2026-09-19 01:05 (chain 41cf86a T-0119, 3657876 T-0179). Revert path: git revert the two commits in reverse order. Backlog: gitignore handover_state.json; throttle under the flock; D4 T-0123 should make handover refuse while an execute is running

## 2026-09-18 C-O5 executor image merged: multi-arch Dockerfile, hardened compose, runbook, protected container paths
type: decision · goal: T-0073 · tasks: T-0165,T-0171,T-0173,T-0176,T-0178,T-0181,T-0183,T-0184 · provenance: repo
- Dockerfile: ubuntu:24.04 by manifest digest, no platform pin, orch user from ORCH_UID/ORCH_GID, uv/Claude Code/Codex pinned (codex asset per arch with checksum, installer scripts hashed and optionally verified), root-owned /opt/orchestrator with only .venv writable by orch, baked git identity, XDG dirs pre-created; compose: external ORCH_NET network, guarded ORCH_CREDS, six mounts (four creds, work, config repos.toml+pool.toml read-only), no ports, no-new-privileges, cap_drop ALL, memory and pids limits; protected-paths.txt: the host credential root, the four container cred paths, the three container code paths; runbook docs/executor-host.md
- four opus security reviews over three fix rounds (T-0171 1.04, T-0176 2.07, T-0181, T-0184 2.10 USD) found: inert protected path, missing network, config baked into the image, .mcp role configs stripped, no git identity, writable code tree; the emulated amd64 build is impossible on the arm64 laptop (Bun installer segfault), so the amd64 build is a kenta-server step
- T-0184 approved with bring-up notes: .venv is writable executed code and not in protected paths; gh auth setup-git writes a gitconfig that is not mounted; gh is never version-checked in the build; the entrypoint and the installer module run uv without --frozen against a root-owned project dir
outcome: merged 74e1830 into goal/T-0073 2026-09-19 02:00 (chain c1155c4, 94d5e32, 156e120, 88c0798). Revert path: git revert the four commits in reverse order. Bring-up on kenta-server follows docs/executor-host.md; the T-0184 notes go to a C-O5 polish task before the first real deployment

## 2026-09-18 Phase C retrospective: kgpt orchestrator module server side shipped on goal/T-0073 by the Claude fallback while Codex cooled
type: decision · goal: T-0073 · tasks: T-0115,T-0129,T-0147,T-0153,T-0158,T-0167,T-0174,T-0119,T-0179,T-0165,T-0173,T-0178,T-0183,T-0130,T-0135,T-0131,T-0138,T-0141 · provenance: repo
- delivered: goals.py runner (start/status/list/stop, headless Planner launch), serve.py bearer-authed HTTP API, pool planner-usage tally + pick, handover checkpoint command, executor image + compose + runbook, plus pipeline fixes found on the way (orphan reconcile, merge sync of the checked-out worktree, failure reasons, verdict preservation, review policy D1 on goal/T-0109)
- process: every spec of complexity 5-6 needed 1-3 spec-review rounds and every code delivery 1-5 fix rounds; opus reviews found real defects each time (deadlocks, credential leaks, silent stalls); about 14 sonnet executes and 25 opus reviews in session 2, reviews as costly as execution; the old daemon in the running server forced hand dispatch and review swaps all day
- lessons recorded as gotchas: stale main checkout after worktree merges (3130d61), spawn_spec_review runs an execute when given an execute id, dispatch loop breaks on zero slots before later spec reviews, fix rounds carrying unreviewed work must be pre-stamped, docker builds outlive executors, amd64 emulation impossible here
outcome: Phase C code-complete 2026-09-19 02:00; PR goal/T-0073 to main opened for the human. Alternatives that lost: running the orchestrator from its own repo against targets (install scaffold won, 2026-09-18), platform-pinned amd64 image (multi-arch won). Next: kgpt-side module C-K1..C-K4 on the kgpt bus, Phase D3-D5 after rebasing goal/T-0109

## 2026-09-18 goal/T-0109 rebased onto goal/T-0073 head before Phase D3-D5
type: decision · goal: T-0109 · tasks: T-0185 · provenance: repo
- the executor rebased the six D1/D2 commits in wt/T-0185 (only a tick() ordering conflict in daemon.py and a test_pool.py concatenation; README and pool.toml auto-merged); 234 tests green; the Planner then moved the branch ref with git branch -f goal/T-0109 7285bbd (old head c593794)
outcome: Phase D tasks now build on the merged Phase C code (serve, planner usage, handover, executor image). Revert path: git branch -f goal/T-0109 c593794 (the pre-rebase head) while nothing new has merged on top

## 2026-09-18 D5 scorecard by task and goal merged into goal/T-0109
type: decision · goal: T-0109 · tasks: T-0186,T-0191,T-0194,T-0195 · provenance: repo
- scorecard.py by_task()/by_goal() aggregate .orchestrator/runs/*.jsonl by task and goal (children via parent), role split execute/review/spec_review/scout/other in percent of usd, per-goal decision runs from planner_runs.json (- / 0 runs / n runs), pool-wide planner transcript tokens from planner_usage.json day_tokens as one footer line; cli scorecard --by task|goal; default output byte-identical
- reviews: T-0191 (opus) caught the wrong planner_usage key, the pool-wide daily count stamped per goal and challenge/triage usd dropped from totals; T-0195 approved with notes: unguarded json.loads on jsonl lines, Codex runs log no usd so the percent split undercounts them, float total tokens
outcome: merged 8a1a569 into goal/T-0109 2026-09-19 04:45 (chain 38d91fe T-0186, 28d0fad T-0194). Revert path: git revert the two commits in reverse order. The T-0195 notes go to a D5 polish task

## 2026-09-19 D3 Planner as a function merged into goal/T-0109: daemon launches a fresh headless Planner only at decision points
type: decision · goal: T-0109 · tasks: T-0122,T-0187,T-0189,T-0192,T-0196,T-0197,T-0198,T-0199,T-0200 · provenance: repo
- orchestrator/planner_runs.py: decision_points() yields held tasks, finished scout fan-outs and closable goals; run() claims the per-event key under the bus lock, launches goals.launch_planner with an ids-only prompt, records running/exited_early/failed_launch in planner_runs.json (atomic write); reconcile() ages out dead runs and stale claims; two failures on one key escalate to gave_up, a terminal blocking status
- orchestrator/mcp.py: the interactive MCP server registers its pid in .orchestrator/planner_session.json behind the entrypoint; the daemon never launches a decision Planner while a session is attached or no account has headroom
- four spec-review rounds (T-0182, T-0188, T-0190, waived) and three opus security reviews (T-0196, T-0198 request_changes; T-0200 approve at 2.26 USD) on sonnet-executed code; T-0198 caught hold_reason text spliced into the prompt of a permission-bypassing Planner, now the key carries ids only
- T-0200 approve notes for a D3 polish task: _held_at keeps the first stage stamp when a task is held twice; dispatch break after every run() starves later decision points; non-dict planner_session.json counts as a failed launch; except BaseException swallows KeyboardInterrupt between claim and record; a decision Planner that legitimately posts no fix round is scored exited_early
outcome: merged 25404d4 into goal/T-0109 2026-09-19 (chain 2007874 T-0192, 319fcc7 T-0197, 25404d4 T-0199). Revert path: git revert the three commits in reverse order. Alternative that lost: keep the long-lived interactive Planner and only trim its context (D4), because the measured cost came from turns, not from single-turn size

## 2026-09-19 Phase D retrospective: token diet shipped on goal/T-0109 (review policy, scout budget, decision-point Planner, session rules, cost per goal)
type: decision · goal: T-0109 · tasks: T-0120,T-0121,T-0123,T-0185,T-0186,T-0194,T-0192,T-0197,T-0199 · provenance: repo
- delivered: daemon review policy from pool.toml [review] (one review by the other model, two from 7, spec review from 6); scout limits 12 turns / 1.0 USD / 600 s with recall-first prompt; planner_runs.py decision-point launches with claim, reconcile and gave_up; CLAUDE.md and planner.md session rules (150k handover, no forks, one notification per wait); scorecard --by task|goal with a planner transcript footer
- cost by the new scorecard: goal T-0109 33.48 USD, execute 49.9 percent, review 43.7, spec review 6.4; Phase C T-0073 62.59 USD for comparison; all execution on the sonnet fallback while Codex cooled, every review on opus
- process: D1 needed five fix rounds (deadlocks, IndexError in merge_reviewed, review count frozen at gate), D3 four spec rounds and two fix rounds, D5 one fix round, D4 landed clean; a D0 rebase task moved goal/T-0109 onto the Phase C head before D3-D5
- not verified: acceptance 2 (a goal run after D3 shows Planner tokens per invocation well below the 11.2M session baseline) needs a server restarted on the merged code with autonomous decisions on and a real goal; the running MCP server still loads pre-D3 code
outcome: Phase D code-complete 2026-09-19; PR goal/T-0109 to main opened for the human, to merge after PR 7. Next: measure acceptance 2 on the first autonomous goal; polish backlog from the approving reviews (T-0172, T-0195, T-0200 notes) as c2-3 tasks

## 2026-09-19 Phase E opened: code review off by default, Jev (TypeSafe System One) gates tool calls and ranks Planner inputs, polish backlog specced
type: decision · goal: T-0201 · tasks: T-0202,T-0203,T-0204,T-0205,T-0206,T-0207,T-0208,T-0209,T-0210,T-0211,T-0212,T-0213,T-0214,T-0215,T-0216,T-0217,T-0218 · provenance: repo
- user decision 2026-09-19 14:40: reviews cost 45 percent of T-0073 and 44 percent of T-0109 for defects the human PR review would also see; keep spec review from complexity 6, code review only when the diff touches pool.toml [review] security_paths (one review on security_review_tier, never the executing model); the option to keep reviews from complexity 7 lost
- Jev facts (docs.typesafe.ai 2026-09-19): POST api.typesafe.ai/v1/systemone with a Bearer key; questions of type noul, choice or score return probabilities and confidence; 64k tokens per request (32k state plus the longest question); 0.042 USD per M input tokens, output free, 1200 requests per minute; adopters (LiteLLM guardrail threshold 0.2, jev-compactor drops at P of 0.7 or more, pi jev-prune) drop stale tool results verbatim instead of summarizing
- measurements: worker per-turn baseline context about 24k tokens (haiku probe plain 25.8k, strict MCP config 23.8k, bare mode fails auth); execute runs 2.46M cache-read tokens on average over 30-40 turns; spawn.TOOLS never reached the CLI (no allowedTools flag passed); one Planner bus_read call returned 318k chars; the sink is turns and accumulated tool results, not tool schemas
- design: E1 review policy in daemon.py (T-0210, opus) after P4; E2 jev.py client fail-open with redaction and a daily token budget (T-0214); E3 PreToolUse jev-gate hook, log mode first, block mode later (T-0215, opus); E6 jev_rank for recall.py and handover pruning (T-0216); E8 shadow triage of decision points to measure agreement before any launch is skipped (T-0217); E5 compact bus_read rows (T-0211); E7 allowedTools plus bus-only MCP config plus budget lines (T-0212); E4 waste ratio in the scorecard (T-0218). Egress: task specs, tool-call metadata and memory titles go to TypeSafe; file contents never
outcome: goal/T-0201 branched from goal/T-0109 (25404d4); 17 tasks on the bus, polish P1-P8 at c2-3 merge directly under the current daemon; restart the Planner session right after E1 merges so the new policy is live for the Jev tasks. Human steps: TYPESAFE_API_KEY into the Keychain under the f tok name typesafe, then [jev].enabled = true; add the strict MCP config flags to the f orch launcher so the Planner stops loading unrelated MCP servers and plugins each turn

## 2026-09-19 Phase E retrospective: code review off by default, Jev client, tool-call gate, ranking, shadow triage and the polish backlog shipped on goal/T-0201
type: decision · goal: T-0201 · tasks: T-0202,T-0203,T-0204,T-0205,T-0206,T-0207,T-0208,T-0209,T-0211,T-0212,T-0213,T-0220,T-0223,T-0225,T-0214,T-0227,T-0229,T-0237,T-0216,T-0233,T-0238,T-0217,T-0234,T-0230,T-0235,T-0236 · provenance: repo
- delivered (34 commits over goal/T-0109, 384 tests green): P1-P10 polish; E1 review policy in daemon.py with pool.toml [review] code_review=security_paths, fail-closed changed_paths (NUL-safe, rename-safe), review_reason stamps; E1b rule text; E2 jev.py client (fail-open, redact before truncate, daily budget, locked state, cached key, runs/jev/ logs); E3 PreToolUse jev-gate hook in log mode (needed/redundant/destructive per call, runs/jev/gate.jsonl); E5 compact bus_read; E6 jev_rank for recall --goal and handover pruning; E7 allowedTools + bus-only MCP config + turns in run log; E8 shadow triage of decision points with agreement tracking; E4 scorecard waste_pct/blocked/turns and jev footer; P9 pipeline hygiene (ancestor stamp, dirty worktree hold, fix-round base)
- cost by scorecard: T-0201 38.93 USD, execute 82.5 percent, review 14.6, spec review 2.9 (T-0073 45 percent review, T-0109 44); the guard of 35 USD was passed at 18:15 and the user was told; Codex returned at 16:00 machine time and executed the last three tasks at zero Claude cost
- process: seven opus reviews on the Jev and review-policy code each found a real defect (fail-open inverted in jev_rank, budget leak on skipped decisions, rename escaping security globs, prompt path collision); two spec reviews saved a round each; four incidents cost about two hours: the fix-round worktree race, already_merged stamping uncommitted work, the committed pool.toml toggle breaking test_jev on every worktree, and the daemon Codex path never posting results (all in gotchas.md, P9 fixed the first two)
- verified: jev-gate.sh from the main checkout scores a Read with p_needed 0.46 in 704 ms and appends to runs/jev/gate.jsonl; the API returned confidence null on every noul call so far, so block mode (confidence at least 0.6) can never fire until that is understood; per-worker gate evidence and the zero-review merge of a non-security task are pending the first workers under a restarted server
outcome: Phase E code-complete 2026-09-19; PR goal/T-0201 to main opened for the human after PR 7 and PR 8. Backlog: P11 daemon._dispatch_worker posts Codex results; hook does two uv round trips per tool call (fold --enabled into one process or a cached file); read confidence semantics from the Jev docs and adapt the block rule; narrow security_paths if the user wants fewer reviews in this repo; the running server must be restarted to load any of this

## 2026-09-19 Phase F retrospective: Jev in production shipped on goal/T-0240 (one-process gate, block rule without confidence, daemon posts Codex results, scorecard columns, resume argv, red-merge holds, bus sqlite lifetime)
type: decision · goal: T-0240 · tasks: T-0241,T-0245,T-0242,T-0243,T-0250,T-0248,T-0252,T-0249,T-0254,T-0258,T-0256,T-0244,T-0246,T-0247,T-0251,T-0253,T-0255,T-0257,T-0259 · provenance: repo
- delivered: 10 commits df08523..fe7026a on goal/T-0240: F3 daemon._dispatch_worker posts Codex results (df9074f + test fix fd2e078); F1 jev-gate.sh one interpreter call, lazy imports, startup_ms, skip list (b07a5ff); F2 jev.py votes averaging and confidence None, jev_gate block thresholds in pool.toml [jev] with a noconf rule (ea492c5); F6 merge_reviewed holds on a non-merged result, clears merged_at for tests_red (1d5217e); F4 scorecard text tables show calls/waste_pct/blocked/turns (cbd5a6a, f408edb); F7 bus.db() cached connection with explicit close, test_handover counts only handover lines (35c0462); F5 executor.argv_for: resume without -C or -s, sandbox via --config, usage errors logged without consuming a round (d3f452f, fe7026a)
- cost by scorecard: 3.44 USD, all nine sonnet security reviews (orchestrator/*.py matches every task); eleven Codex runs (astra 5, sol 6) at zero Claude cost; one review changed code (T-0251 caught that codex exec resume also rejects -s); T-0245 merged with zero reviews (review_reason none), the first zero-review merge under the security_paths policy
- measurements: gate latency 26 rows median 667 ms max 830, startup_ms 63-80 after F1, so about 590 ms is the Jev network call; waste 77.8 percent on review T-0244 (9 calls), 62.5 on T-0246, 0 on execute T-0242; block mode never fired (confidence null everywhere, gate_mode log); goal acceptance 2 (median under 400 ms) fails on the network, acceptance 3 (Codex task done without Planner help) unverified because the running server loads goal/T-0201 code without F3
- process: the 18:16 session restart killed the in-process daemon and aborted both Codex turns; codex_reply was broken by the Codex CLI (F5); T-0241 merge went red on a flaky thread test and stayed failed with merged_at set (F6); the T-0254 gate failed three times on sqlite ResourceWarnings in test_handover (F7); two scope misses (T-0248 could not touch test_scorecard.py, T-0249 missed -s) needed fix rounds; planner-mode.sh blocks a branch rebase by the Planner so it ran as Codex task T-0258; every Codex result and three superseded merges were posted or stamped by hand (about 12 bus repairs)
outcome: Phase F code-complete 2026-09-19 20:20; PR goal/T-0240 to main opened after PRs 6-9. Alternative that lost: keep the 400 ms latency target, because interpreter cost is now 80 ms and the rest is network; the decision on async scoring or accepting 700 ms goes to the human (reviews/2026-09-19-phase-f-review.md). Next: switch the main checkout to goal/T-0240 and restart the session so F3, F5 and F6 are live, then one tiny Codex task to verify acceptance 3; decide security_paths width and daemon-as-a-process before the next long goal. Revert path: git revert fe7026a d3f452f 35c0462 f408edb cbd5a6a 1d5217e ea492c5 b07a5ff fd2e078 df9074f in that order, or move goal/T-0240 back to df08523 with git branch -f while nothing else has merged on top

## 2026-09-19 Planner checkpoint commit 7297936: plan.md and gotchas for the session 5 resume of Phase G
type: decision · goal: T-0260 · tasks: T-0326,T-0331,T-0336 · provenance: repo
- plan.md rewritten at 23:02 with the corrected task map (G3 held, not merged; G2b, G11, G4 chains marked merged; T-0326 result posted by hand); two gotchas appended
outcome: revert path: git revert 7297936 (touches only .orchestrator/plan.md and .orchestrator/memory/gotchas.md)

## 2026-09-19 Planner checkpoint commit d12ae2c: plan.md task map at 23:20 and two gotchas
type: decision · goal: T-0260 · tasks: T-0337,T-0331,T-0332 · provenance: repo
- G7 v4 and G10 merged; fix rounds T-0341 (G5) and T-0344 (G3) written; review T-0332 re-spawned by hand
outcome: revert path: git revert d12ae2c (plan.md and gotchas.md only)

## 2026-09-19 Phase G retrospective: T-0260 token economy closed, 11 sub-goals merged on goal/T-0260 at 2ca1517, gate green externally, PR 11
type: decision · goal: T-0260 · tasks: T-0264,T-0265,T-0266,T-0267,T-0270,T-0271,T-0273,T-0274,T-0275,T-0276,T-0277,T-0290,T-0324,T-0326 · provenance: repo
- 82 tasks under the goal: 40 execute (20 of them fix rounds), 31 reviews (14 approve, 17 request_changes), 8 spec reviews (6 request_changes; G6 and G7 three rounds each then waived), 3 scouts; 37 commits, 42 files, +3055/-171 over goal/T-0240
- cost 22.38 USD, 442 calls, 636 turns; review 62.8 percent, spec review 24.2 percent, scouts 6.8 percent, Codex execute 6.2 percent; every request_changes review named a real defect (hunk-header duplication, fail-open diff guards, unlocked state file, unguarded lookups, packet ordering, missing tests)
- what cost the most: Codex (luna) five times reported acceptance-named tests as passing without writing them (T-0271, T-0326, T-0273, T-0277, T-0348), each caught by a review round or a Planner diff --stat check plus one codex_reply; the 22:50 session restart orphaned two Codex runs and leaked executor slot counters twice; plan.md carried one wrong merged claim (G3)
- what worked: the Phase F daemon posted every Codex result it dispatched (T-0240 acceptance 3 verified on T-0273), gated, spawned reviews and merged serially without Planner posts; fix rounds cut from the held branch kept every chain linear; a Planner diff --stat check before the review saved review rounds on T-0273, T-0277 and T-0348
outcome: Accepted: tests-green exit 0 on wt/goal-T-0260 at 2ca1517; PR 11 https://github.com/K3NTAW/orchestrator/pull/11 goal/T-0260 to main for the human after PR 10; T-0260 marked done 23:56. Backlog (c2-c3): derive executor running counters from bus state; codex/codex_reply tools post results; re-dispatch requeued reviews; gate checks acceptance-named test ids exist; consistent total_tokens for legacy rows. Revert path for the whole phase: close PR 11 unmerged

## 2026-09-19 Planner checkpoint commit 93f0a7c: Phase G close in plan.md, retrospective and gotchas
type: decision · goal: T-0260 · tasks: T-0260 · provenance: repo
- plan.md rewritten for the closed goal and the docs.kentawaibel.com kickoff facts; decisions.md carries the Phase G retrospective; gotchas.md the slot-counter and review-redispatch rules
outcome: revert path: git revert 93f0a7c (plan.md, decisions.md, gotchas.md only)

## 2026-09-20 docs.kentawaibel.com goal started in its own repo via orchestrator goal start (headless Planner on account B)
type: decision · goal: T-0260 · tasks: T-0001 · provenance: repo
- user request 2026-09-19 23:46: docs.kentawaibel.com with the visual of kentawaibel.com and one entry for the orchestrator; the user granted the rights for the outward-facing steps (Vercel project, domain, deploy)
- repo /Users/k3ntaw/code/docs-kentawaibel created empty with git init, scaffolded by goal start at 085d4d9 (its own .orchestrator, hooks, skills, pool.toml copied from this repo), GOAL task T-0001 on the docs bus, headless Planner pid 28790, log .orchestrator/runs/planner-T-0001.log, budget 10 USD
- facts handed in the goal text: portfolio stack and @theme tokens at k3ntaw-portfolio/src/index.css, markdown content pipeline, vitest+eslint so tests-green.sh (npm test, npm run lint) gates, vercel.json SPA rewrites; deploy explicitly excluded from the headless run
outcome: the interactive Planner adds the Vercel project docs-kentawaibel, the domain docs.kentawaibel.com (DNS already points at Vercel, DEPLOYMENT_NOT_FOUND) and deploys once the docs repo builds green. Revert path: in the docs repo git revert 085d4d9, or delete the repo directory (human)

## 2026-09-20 docs.kentawaibel.com live: docs goal T-0001 closed in its repo, Vercel project docs-kentawaibel, domain added, production deploy from goal/T-0001 ebf4a11
type: decision · goal: T-0260 · tasks: T-0001 · provenance: repo
- headless Planner (account B) finished in 11 min for 6.17 USD: five serial Codex tasks (astra, luna, sol, terra) merged into goal/T-0001 at ebf4a11 with zero fix rounds, zero holds, no reviews; it also created the private GitHub repo K3NTAW/docs-kentawaibel and PR 1 goal/T-0001 to main
- interactive Planner verified tests-green exit 0 and npm run build on wt/deploy, then vercel link --yes --project docs-kentawaibel (prj_ilqCa6qLBtvLKxhi0Mrs0PoT1E2R, created), vercel domains add docs.kentawaibel.com, vercel deploy --prod: deployment FPadPdHG8eNm4Qi1fbCyzV8dMrKu Ready, https://docs.kentawaibel.com 200 with title Docs, /orchestrator 200 via the SPA rewrite
- the deploy was made from the goal branch worktree, not from main; main in the docs repo still holds only the scaffold until the human merges PR 1
outcome: Revert path: vercel rollback (or vercel remove the deployment) on project docs-kentawaibel, domain removal and project deletion are deletes for the human; the repo itself: close PR 1 unmerged. Follow-ups: connect the Vercel project to the GitHub repo for automatic deploys, add a second entry, .vercel/ is untracked in wt/deploy and should be gitignored by a task
