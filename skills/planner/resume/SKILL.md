---
name: resume
description: Resume an interrupted goal from plan.md and the bus after a Planner restart or context reset.
---
# Resume
A human reviews merges.
1. Read plan.md: goal, decomposition, decisions, next step.
2. `bus_read(status_not="done")`: held → check `status()` and re-spawn; running with `codex_thread` → one `codex_reply` carrying the task's `resume_hint`; failed → read `reason`/`resume_hint`, then re-spec or escalate.
3. Continue at plan.md's "next step".

## Session hygiene
- Waiting: one Monitor per wait with an exit condition on the final state, never per status change; no polling.
- Tool output: head, cut, jq; never cat a file over 200 lines; read scout results once.
- Review policy: 1–3 hooks only, auto-merge on a green gate · 4–6 one sonnet reviewer, other family · 7–10 two reviews + security checklist, cross-model when Codex executed, else both reviews on the non-executing Claude tier.
