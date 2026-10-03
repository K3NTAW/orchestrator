---
name: resume
description: Resume an interrupted goal from plan.md and the bus after a restart or context reset.
roles: [planner]
task_classes: ["*"]
triggers: [resume, interrupted, restart]
tools: [Read, bus_read, status, codex_reply]
requires_context: [previous_result, decision]
output: "continued goal from the recorded next step"
security: internal
repo: orchestrator
---
## Trigger
Resume, interrupted, restart, or an existing goal in plan.md.

## Objective
Continue the goal safely from recorded state.

## Procedure
Read plan.md '## Now' and goals; status lines are in .orchestrator/plan-log.md; do not read it, grep it only for a missing fact. Call `bus_read(status_not="done")`: held → `status()`/re-spawn; running + `codex_thread` → one `codex_reply` with `resume_hint`; failed → read `reason`/`resume_hint`, re-spec or escalate. Continue at next step.

## Tools
Read; bus_read, status, codex_reply.

## Evidence requirements
Use plan.md and current bus state once.

## Output contract
Continue the goal from its recorded next step.

## Stop conditions
Stop on normal goal completion or escalation.

## Failure/recovery
One Monitor per wait; no polling. Limit output; read scout results once. Apply review policy: 1–3 hooks/auto-merge green; 4–6 one other-family sonnet; 7–10 two reviews plus checklist, cross-model when Codex executed.
