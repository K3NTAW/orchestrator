# gotchas

## 2026-09-17 guardrails.sh blocks any Bash command text containing the system bin prefix
type: gotcha · goal: manual-2026-09-17 · provenance: repo
- .claude/hooks/guardrails.sh — protected-path check greps the literal prefix anywhere in the command, so heredocs writing a shebang line, and PR bodies quoting one, are blocked
outcome: fix: write such files with the Write tool (path-based check only) or from a body file; planner-mode makes this moot for the Planner

## 2026-09-17 planner-mode treats /var/folders as a temp dir
type: gotcha · goal: manual-2026-09-17 · provenance: repo
- tests/test_orchestrator.py PlannerMode.test_writes — tempfile.mkdtemp lives under /var/folders, so a fake source path under TMP is allowed by design; use a real REPO path to test the block
outcome: test fixture fix pending human commit 2026-09-17
