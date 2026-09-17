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
