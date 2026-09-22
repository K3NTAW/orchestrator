---
name: orchestrate
description: Route a software goal through planning, atomic specs, execution, review, merge, and retrospective.
roles: [planner]
task_classes: [security, architectural, debugging, mechanical, unfamiliar, "*"]
triggers: [feature, refactor, bug, goal]
tools: [Read, Search, bus_read, bus_create_task, bus_events, spawn_scout, spawn_challenge, merge, Bash]
requires_context: [memory_entry, scout_finding, test_result, review_finding, decision]
output: "completed goal with green gate, merged tasks, retrospective, and PR"
security: internal
repo: orchestrator
---
## Trigger
Any feature, refactor, bug, or software goal given to the Planner.

## Objective
Route the goal through planning, atomic execution, serialized merge, and retrospective.

## Procedure
Read `.orchestrator/plan.md`; memory `recall.sh index "<goal terms>"`; `bus_read(status_not="done")`; use resume if a goal exists. Classify 1–10 (1–3 trivial, 4–6 multi-file, 7–10 cross-cutting/security), record route/uncertainty, and default to zero scouts.

Routes: localized → brief spec/one executor/gates/human PR; uncertain → one named investigation; independent → disjoint specs with interface contracts; high-risk/architectural → detailed plan, spec review, execution, independent review.

For uncertainty: `bus_create_task(role="scout")`, then `spawn_scout` or Agent Teams task containing `bus:<id>`; batch `bus_events(since)`, read once, summarize into plan.md. Acted-on confidence <0.7 → `spawn_challenge`. Overwrite plan.md; use `write-spec` per atomic task; warn over five files; split only on behavior/dependency boundaries; name `depends_on` and `tests/test_<module>.py`.

Start `orchestrator daemon` or `orchestrator daemon --once`; for held spec_review/review request_changes/gate_red, write a fix-round spec depending on the held task. Accept only when `.claude/hooks/tests-green.sh wt/<id>` exits 0 locally. Security-path diffs get exactly one `security_review_tier` review, never the executing model, checklist always; record `pipeline.review_reason`. `merge(id)`, then `graph.sh update`.

Retrospective: `record.sh draft <GOAL>`, `record.sh add ...`, and `record.sh set architecture` after layout changes. Complete GOAL; open PR goal/<parent> → main.

## Tools
Read/Search; bus tools; spawn tools; daemon, gate, merge, and memory commands.

## Evidence requirements
Record classification, route, uncertainty, cited scout facts, local green gate, and retrospective.

## Output contract
Produce an updated plan, accepted atomic tasks, serialized merges, retrospective, and goal PR.

## Stop conditions
Stop when the GOAL is complete and PR opened, or a held task needs a fix-round spec.

## Failure/recovery
Never edit source. One Monitor per wait; no polling. Limit output and read scout results once. Human reviews every PR; no default code review beyond stated policy.
