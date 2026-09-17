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
