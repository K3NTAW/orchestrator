---
name: resume
description: Resume an interrupted goal from plan.md and the bus after a Planner restart or context reset.
---
# Resume
A human reviews merges.
1. Read plan.md: goal, decomposition, decisions, next step.
2. `bus_read(status_not="done")`: held → check `status()` and re-spawn; running with `codex_thread` → one `codex_reply` carrying the task's `resume_hint`; failed → read `reason`/`resume_hint`, then re-spec or escalate.
3. Continue at plan.md's "next step".
