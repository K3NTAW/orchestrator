---
name: orchestrate
description: Run a software goal through the tri-model lifecycle (scouts → atomic specs → Codex executor → review → serial merge → retrospective). Use for any feature, refactor or bug goal handed to the Planner.
---
# Orchestrate
You are the Planner; a human approves every merge to main. You never edit source.
1. Read `.orchestrator/plan.md`; skill `memory` → `recall.sh index "<goal terms>"` before reading memory files wholesale; `bus_read(status_not="done")`. If plan.md has a goal → skill `resume`.
2. Classify complexity 1–10 (1–3 trivial, 4–6 multi-file, 7–10 cross-cutting/security). Write it into plan.md. Complexity 4 or less → no scouts; the Planner greps for what it needs and moves to step 5.
3. Fan out at most two scouts per goal, unless plan.md names why more are needed: `bus_create_task(role="scout")` then `spawn_scout` (account B), or an Agent Teams teammate whose task description contains `bus:<id>` (account A; the TaskCompleted hook posts its result). Enumeration scouts ("list X with path:line") go on haiku; judgement scouts go on sonnet; opus only with a written reason in the task spec.
4. Batch `bus_events(since)`; read each result once (≤1,500 tokens each) and summarize straight into plan.md — never re-read a scout result once summarized. Findings <0.7 confidence you will act on → `spawn_challenge`.
5. Synthesize → overwrite plan.md → skill `write-spec` per atomic task (≤5 files each), naming `depends_on` on any task that needs another merged first and filing tests at `tests/test_<module>.py`.
6. Start `orchestrator daemon` (or call `orchestrator daemon --once`); the daemon dispatches ready tasks, gates, spawns reviews, merges on approve, dispatches dependents. The Planner intervenes on held tasks (spec_review or review request_changes, gate_red): write the fix-round spec with depends_on=[the held task].
7. Accept only when `.claude/hooks/tests-green.sh wt/<id>` exits 0 in your own shell. Review per complexity policy — external gate stays for anything the Planner merges by hand. `merge(id)`, then `graph.sh update` (skill `memory`).
8. Retrospective: skill `memory` → `record.sh draft <GOAL>` then `record.sh add ...` (dated entry, hook-enforced), `record.sh set architecture` when layout changed. Complete the GOAL task. Open PR goal/<parent> → main.

## Session hygiene
- Waiting: one Monitor per wait with an exit condition on the final state, never per status change; no polling.
- Tool output: head, cut, jq; never cat a file over 200 lines; read scout results once.
- Review policy: 1–3 hooks only, auto-merge on a green gate · 4–6 one sonnet reviewer, other family · 7–10 two reviews + security checklist, cross-model when Codex executed, else both reviews on the non-executing Claude tier.
