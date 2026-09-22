---
name: implement-spec
description: Implement one atomic orchestrator task in the current worktree, within scope, until tests are green.
roles: [execute]
task_classes: ["*"]
triggers: []
tools: [Read, Edit, Bash, "script:scripts/failures_only.sh"]
requires_context: [source_chunk, test_result]
output: "3-line summary, git diff --stat, and failures-only output; committed task branch"
security: internal
repo: orchestrator
---
## Trigger
Any execute task from the orchestrator.

## Objective
Implement one atomic task within its Scope and leave tests green.

## Procedure
Read Spec, Acceptance, Scope. Run `scripts/tests_green.sh` first. Make the smallest accepted change. Run it again; iterate on failure. Commit on the task branch.

## Tools
Read, Edit, Bash; `scripts/failures_only.sh`.

## Evidence requirements
Acceptance checks and tests-green result.

## Output contract
Finish with: 3-line summary, `git diff --stat`, failures-only output (empty when green).

## Stop conditions
Stop when Acceptance passes or budget ends.

## Failure/recovery
Report only `scripts/failures_only.sh` output. Edit only paths in Scope. No new dependencies unless the spec names them.
