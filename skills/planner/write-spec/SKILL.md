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
Title verb+object; what/why ≤12 lines citing `path:line`. Acceptance: 2–5 observable checks; tests `tests/test_<module>.py::test_name` (unittest.TestCase unless pytest). Declare disjoint Scope, read_scope, interface_contract, task_class (mechanical/unfamiliar/debugging/architectural/security), route (straightforward/full), read_only=false, timeout_s, budget_turns, dependencies. Complexity ≥5: spec review. Warn >5 files; split by behavior/dependency. Test paths resolve from repo root (`crates/<crate>/tests/x.rs`, never `tests/x.rs`); put unchanged assertions in their own criterion. Render `.orchestrator/prompts/execute.md`; `bus_create_task(role="execute", tier="astra", complexity=N, parent=GOAL_ID, ...)`.

## Tools
Read; bus_create_task.

## Evidence requirements
Acceptance, Scope, scout citations, interfaces for dependent work.

## Output contract
One complete atomic execute task; parseable test IDs.

## Stop conditions
Stop after task creation.

## Failure/recovery
TaskCreated hook rejects missing Acceptance/Scope. No unnamed deps. Human reviews merges.
