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
