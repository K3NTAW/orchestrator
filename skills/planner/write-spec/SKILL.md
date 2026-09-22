---
name: write-spec
description: Write one atomic execute-task spec with one acceptance cluster and explicit scope.
roles: [planner]
task_classes: ["*"]
triggers: [spec, acceptance, decompose, atomic]
tools: [Read, bus_create_task]
requires_context: [scout_finding, source_chunk, decision]
output: "one bus execute task with complete atomic spec"
security: internal
repo: orchestrator
---
## Trigger
Splitting a synthesized plan into an atomic task.

## Objective
Create one independently verifiable execute-task spec.

## Procedure
Title: verb + object. Spec: what/why in ≤12 lines with scout `path:line`. Acceptance: 2–5 observable checks. Tests use `tests/test_<module>.py::test_name` and unittest.TestCase unless pytest is a dependency. Declare disjoint Scope, read_scope, interface_contract, task_class (`mechanical`, `unfamiliar`, `debugging`, `architectural`, `security`), route (`straightforward` or `full`), read_only=false, timeout_s, budget_turns, dependencies, and any allowed dependency. Complexity ≥5 gets spec review. Warn above five files; split only by behavior/dependency boundaries. Render `.orchestrator/prompts/execute.md`; call `bus_create_task(role="execute", tier="astra", complexity=N, parent=GOAL_ID, ...)`.

## Tools
Read; bus_create_task.

## Evidence requirements
Acceptance and Scope are mandatory; cite scout findings; explicit interfaces for parallel/dependent work.

## Output contract
One atomic execute bus task with complete fields and parseable test IDs.

## Stop conditions
Stop after task creation.

## Failure/recovery
The TaskCreated hook rejects missing Acceptance/Scope. No new dependencies unless named. A human reviews merges.
