---
name: implement-spec
description: Implement one atomic orchestrator task in the current worktree, within scope, until tests are green. Use for any task delivered by the orchestrator.
---
# Implement spec
You work for the Planner; a human reviews merges. Edit only paths in the task's Scope list.
1. Read Spec, Acceptance, Scope from the prompt. Run `scripts/tests_green.sh` first for the baseline.
2. Make the smallest change that meets Acceptance. No new dependencies unless the spec names them.
3. `scripts/tests_green.sh`; on failure iterate, reporting only `scripts/failures_only.sh` output.
4. Finish with: 3-line summary, `git diff --stat`, failures-only output (empty when green). Commit on the task branch.
