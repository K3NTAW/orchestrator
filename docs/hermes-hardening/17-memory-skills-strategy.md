# Memory, skills, and workflow strategy

Recurring procedural gotchas and decisions can seed learned-skill proposals. Records are grouped by shared component when at least three records span at least two distinct dates and contain procedural language. Candidate confidence is `min(1, records / 5) * min(1, distinct dates / 3)` and evidence strength is the record count. The normal 0.6 proposal threshold, quarantine, testing, shadow, and activation lifecycle remains in force.

Memory-sourced proposals preserve their source record IDs. Their roles are the union of roles in source task files, defaulting to `execute` when those files provide none. Activating the skill adds a `skill:<skill id>` tag to every source record; the generated HOT view then points at the learned skill instead of repeating the procedural body.

Every successfully merged execute lineage writes one WARM strategy record. Fix rounds resolve to their lineage root, and the deterministic ID `strategy-<root task id>` prevents duplicate records. The record outcome is either `first_pass` or `fix_rounds:<count>`; tags contain only task class, complexity band, and strategy name. Its body records tokens, cost, duration, executor, and fix rounds.

Use `orchestrator memory strategies` to aggregate count, first-pass rate, median tokens, and median duration by strategy. Optional `--task-class` and `--band` filters let the Planner query comparable work, and `--json` emits structured output.
