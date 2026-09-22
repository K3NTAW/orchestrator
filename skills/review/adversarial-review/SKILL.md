---
name: adversarial-review
description: Review a scoped diff you did not write against acceptance criteria.
roles: [review, spec_review]
task_classes: ["*"]
triggers: []
tools: [Read, bus_post_result]
requires_context: [source_chunk, test_result]
output: "review verdict JSON via bus_post_result"
security: internal
repo: "*"
---
## Trigger
Any review or spec_review task.

## Objective
Find unmet acceptance, out-of-scope behavior, missing error paths, and vacuous tests.

## Procedure
Review only -U3 hunks. Read one path only when a hunk is ambiguous. For complexity ≥7 apply `references/security-checklist.md`.

## Tools
Read; bus_post_result.

## Evidence requirements
Every comment names its path, line, issue, and severity.

## Output contract
Return ONLY `{"verdict":"approve|request_changes","comments":[{"path":"","line":0,"issue":"","severity":"low|med|high"}]}` via bus_post_result.

## Stop conditions
Stop after the scoped diff is judged against acceptance.

## Failure/recovery
Never read the whole repo. A human reviews merges.
