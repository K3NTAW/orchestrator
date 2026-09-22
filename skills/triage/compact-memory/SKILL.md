---
name: compact-memory
description: Compact decisions to at most 300 lines and deduplicate gotchas while preserving outcomes.
roles: [triage, planner]
task_classes: [mechanical]
triggers: [compact, "over budget", dedupe]
tools: [Read, Edit, bus_post_result]
requires_context: [memory_entry, decision]
output: "compacted memory and five-line diff summary"
security: internal
repo: orchestrator
---
## Trigger
Monthly, compact, dedupe, or memory over budget.

## Objective
Compact `.orchestrator/memory/decisions.md` to ≤300 lines and dedupe gotchas.md without losing outcomes.

## Procedure
Fold superseded decisions into successors with one-line “superseded YYYY-MM-DD”. Overwrite in place.

## Tools
Read, Edit; bus_post_result.

## Evidence requirements
Preserve every dated decision outcome and every gotcha whose fix remains current.

## Output contract
Post a 5-line diff summary via bus_post_result.

## Stop conditions
Stop at ≤300 decision lines with gotchas deduplicated.

## Failure/recovery
Never drop a current fix. A human reviews merges.
