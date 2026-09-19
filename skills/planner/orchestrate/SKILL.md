---
name: orchestrate
description: Route a software goal through planning, atomic specs, execution, review, serial merge, and retrospective. Use for any feature, refactor or bug goal handed to the Planner.
---
# Orchestrate
You are the Planner; a human approves every merge to main. You never edit source.
1. Read `.orchestrator/plan.md`; skill `memory` → `recall.sh index "<goal terms>"` before reading memory files wholesale; `bus_read(status_not="done")`. If plan.md has a goal → skill `resume`.
2. Classify complexity 1–10 (1–3 trivial, 4–6 multi-file, 7–10 cross-cutting/security) and route the goal below. Write the classification, route, and any named uncertainty into plan.md. The default is **zero scouts**: grep for what you need first.

## Routing table

| Goal type | Route |
| --- | --- |
| Clear, localized change | Brief spec, one executor, deterministic gates, human PR. |
| Uncertain location or behaviour | Record one named uncertainty in plan.md; run one targeted investigation (a scout), then write the spec. |
| Independent changes | Give separate workers separate specs, each with an explicit interface contract. |
| High-risk or architectural | Detailed planning, spec review, execution, and independent review. |

3. For the uncertain route only, create one targeted scout with the named uncertainty from plan.md: `bus_create_task(role="scout")` then `spawn_scout` (account B), or an Agent Teams teammate whose task description contains `bus:<id>` (account A; the TaskCompleted hook posts its result). Batch `bus_events(since)`; read its result once (≤1,500 tokens) and summarize it straight into plan.md — never re-read a scout result once summarized. Findings <0.7 confidence you will act on → `spawn_challenge`.
4. Synthesize → overwrite plan.md → skill `write-spec` per atomic task. Warn above five files; split by independently verifiable behaviour and dependency boundaries, never merely to satisfy the count. Name `depends_on` on any task that needs another merged first and file tests at `tests/test_<module>.py`.
6. Start `orchestrator daemon` (or call `orchestrator daemon --once`); the daemon dispatches ready tasks, gates, spawns reviews, merges on approve, dispatches dependents. The Planner intervenes on held tasks (spec_review or review request_changes, gate_red): write the fix-round spec with depends_on=[the held task].
7. Accept only when `.claude/hooks/tests-green.sh wt/<id>` exits 0 in your own shell — that's the merge bar; no code review by default. When the merged diff touches a `pool.toml` `[review]` security_paths glob, exactly one review fires on `security_review_tier`, never the executing model, security checklist always on — `pipeline.review_reason` records why. `merge(id)`, then `graph.sh update` (skill `memory`).
8. Retrospective: skill `memory` → `record.sh draft <GOAL>` then `record.sh add ...` (dated entry, hook-enforced), `record.sh set architecture` when layout changed. Complete the GOAL task. Open PR goal/<parent> → main.

## Session hygiene
- Waiting: one Monitor per wait with an exit condition on the final state, never per status change; no polling.
- Tool output: head, cut, jq; never cat a file over 200 lines; read scout results once.
- Review policy: gate = tests green + hooks, no code review by default; a diff touching `[review]` security_paths gets one review on `security_review_tier` (never the executing model, security checklist always); spec review from complexity 6 on sonnet stays; an orphaned result gets one review; the human reviews every PR.
