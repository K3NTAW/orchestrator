---
name: orchestrate
description: Run a software goal through the tri-model lifecycle (scouts → atomic specs → Codex executor → review → serial merge → retrospective). Use for any feature, refactor or bug goal handed to the Planner.
---
# Orchestrate
You are the Planner; a human approves every merge to main. You never edit source.
1. Read `.orchestrator/plan.md`; skill `memory` → `recall.sh index "<goal terms>"` before reading memory files wholesale; `bus_read(status_not="done")`. If plan.md has a goal → skill `resume`.
2. Classify complexity 1–10 (1–3 trivial, 4–6 multi-file, 7–10 cross-cutting/security). Write it into plan.md.
3. Fan out 3–6 scouts: `bus_create_task(role="scout")` then `spawn_scout` (account B), or an Agent Teams teammate whose task description contains `bus:<id>` (account A; the TaskCompleted hook posts its result). Prefer sonnet; justify opus.
4. Batch `bus_events(since)`; read results only (≤1,500 tokens each). Findings <0.7 confidence you will act on → `spawn_challenge`.
5. Synthesize → overwrite plan.md → skill `write-spec` per atomic task (≤5 files).
6. Execute: `status()`; if Codex is available, `codex(task_id, prompt)` per task (claims the task, records the thread). Fix loop ≤5 via `codex_reply(task_id, delta)` using `.orchestrator/prompts/fix-delta.md`. Codex cooling → refill the pipeline, never wait idle.
7. Accept only when `.claude/hooks/tests-green.sh wt/<id>` exits 0 in your own shell. Review per complexity policy. `merge(id)`, then `graph.sh update` (skill `memory`).
8. Retrospective: skill `memory` → `record.sh draft <GOAL>` then `record.sh add ...` (dated entry, hook-enforced), `record.sh set architecture` when layout changed. Complete the GOAL task. Open PR goal/<parent> → main.
