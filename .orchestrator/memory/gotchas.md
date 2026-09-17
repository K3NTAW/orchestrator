# gotchas

## 2026-09-17 guardrails.sh blocks any Bash command text containing the system bin prefix
type: gotcha · goal: manual-2026-09-17 · provenance: repo
- .claude/hooks/guardrails.sh — protected-path check greps the literal prefix anywhere in the command, so heredocs writing a shebang line, and PR bodies quoting one, are blocked
outcome: fix: write such files with the Write tool (path-based check only) or from a body file; planner-mode makes this moot for the Planner

## 2026-09-17 planner-mode treats /var/folders as a temp dir
type: gotcha · goal: manual-2026-09-17 · provenance: repo
- tests/test_orchestrator.py PlannerMode.test_writes — tempfile.mkdtemp lives under /var/folders, so a fake source path under TMP is allowed by design; use a real REPO path to test the block
outcome: test fixture fix pending human commit 2026-09-17

## 2026-09-17 tests-green.sh must run under uv outside the MCP server
type: gotcha · goal: T-0001 · tasks: T-0002 · provenance: repo
- .claude/hooks/tests-green.sh:17 — without pytest on PATH it runs plain python3; the system python3 is 3.9 and the tests import tomllib (3.11+)
- merge() inherits the orchestrator MCP server env, which uv run started, so the venv python is first on PATH there
outcome: Planner runs the gate as: uv run .claude/hooks/tests-green.sh wt/<id>. Follow-up: make the hook prefer uv run when pyproject.toml exists (hooks dir, needs a worker task)

## 2026-09-17 Guardrails.test_edit_protected_path was checkout-relative
type: gotcha · goal: T-0001 · tasks: T-0002,T-0003 · provenance: repo
- tests/test_orchestrator.py:177 — REPO/.claude/settings.json is only protected in the main checkout; in wt/T-xxxx it is unprotected, so the merge gate fails for every task
outcome: T-0003 derives the path from protected-paths.txt. Lesson: any test that asserts on a protected path must use the list, not REPO

## 2026-09-17 planner-mode redirect heuristic fires on the co-author trailer
type: gotcha · goal: T-0001 · provenance: repo
- .claude/hooks/planner-mode.sh — the '>' in 'Co-Authored-By: ... <noreply@anthropic.com>' matches the redirect regex, so inline git commit -m with the trailer is denied; heredoc bodies with angle brackets too
outcome: Planner writes commit messages and PR bodies to the scratchpad and uses git commit -F / gh --body-file (temp path mention is allowed). Follow-up: strip <...@...> before the redirect check

## 2026-09-17 Verify negative scout findings with a direct listing
type: gotcha · goal: T-0005 · tasks: T-0008 · provenance: repo
- scout T-0008 reported no gpt-luna/terra/sol ids on disk at 0.85 confidence; the ids are gpt-5.6-luna, gpt-5.6-sol, gpt-5.6-terra in the Codex models_cache.json; the grep pattern assumed a gpt-name prefix
outcome: before acting on an absence claim, list the source directly; scout specs should ask for a full listing plus the match, not a pattern match alone

## 2026-09-17 Worker results over MAX_RESULT_CHARS were silently lost
type: gotcha · goal: T-0005 · tasks: T-0009,T-0010,T-0015 · provenance: repo
- orchestrator/bus.py:75-80 post_result raises when the result exceeds 6000 chars; orchestrator/spawn.py run_worker called it unguarded in a daemon thread, so the task stayed running with no process
outcome: T-0015 adds spawn.fit_result plus a fail-safe status; until merged, scout specs carry an explicit output cap of 4,500 chars

## 2026-09-17 challenge.md is repo-only; web claims cannot be challenged with it
type: gotcha · goal: T-0005 · tasks: T-0014 · provenance: repo
- .orchestrator/prompts/challenge.md:2 — 'refute it with concrete evidence from the repo'; the T-0014 challenger never fetched a page and returned confirmed on repo grounds
outcome: for web or tool-output claims, write the challenge spec with explicit URLs and say the prompt template's repo instruction does not apply; or add a challenge-web.md template (follow-up)

## 2026-09-17 Review affinity lives only on account B, so a B-executed fallback task gets a same-account review
type: gotcha · goal: T-0005 · tasks: T-0016,T-0021 · provenance: repo
- .orchestrator/pool.toml — account A role_affinity has no review; B has review and execute; B1 executed as claude:opus on B and its sonnet review also runs on B
outcome: different model still holds (opus vs sonnet); add review to account A affinity or make Pool.pick(review) exclude the executing account (follow-up spec); until then note same-account-review on the PR

## 2026-09-17 Review worktrees are created from origin/main, so stacked tasks get reviewed without their dependencies
type: gotcha · goal: T-0005 · tasks: T-0017,T-0026 · provenance: repo
- orchestrator/spawn.py ensure_worktree(task_id, base=origin/main) is used for review workers too; scoped_diff diffs origin/main...HEAD of the reviewed task; T-0026 judged executor.py in a checkout lacking B1 and reported pick_executor as nonexistent
outcome: Planner pre-creates the review worktree off the reviewed task branch before spawn_review; follow-up spec: ensure_worktree base = reviewed task branch for role review, diff base = parent goal branch; until then the first review of any stacked task is suspect

## 2026-09-17 scrapling 0.4.15 plain Fetcher imports the browser client libraries
type: gotcha · goal: T-0005 · tasks: T-0023,T-0036 · provenance: repo
- scrapling.engines.static imports toolbelt.convertor (curl_cffi, playwright, patchright) and toolbelt.fingerprints (browserforge) unconditionally; the Python packages are required for import even though no browser is installed (scrapling install is never run)
outcome: pyproject lists scrapling plus those four explicitly instead of the [fetchers] extra; msgspec and protego dropped; camoufox absent. Revisit when scrapling makes them lazy

## 2026-09-17 artificialanalysis.ai does list GPT-5.6 Luna/Terra/Sol and Claude Opus 5; scout T-0007 was wrong
type: gotcha · goal: T-0005 · tasks: T-0007,T-0014,T-0036 · provenance: repo
- live fetch 2026-09-17 16:17 UTC (bench.json unmatched_names): GPT-6 Astra (max), GPT-5.6 Luna (max), GPT-5.6 Terra (max), GPT-5.6 Sol (max), Claude Opus 5 (Adaptive Reasoning, Max Effort), Claude Fable 5.1 (...), GPT-5.5 Pro (xhigh)
- display names carry a parenthetical effort suffix; bench.match compared whole strings, so 0 of 8 hints matched
outcome: B4c fix: match on the name with parentheticals stripped, prefer the (max) variant; lesson: a 0.6 negative web finding plus a repo-only challenge is not evidence, fetch and look
