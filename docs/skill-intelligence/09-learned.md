# Learned skill proposals

Learned skills are inert proposals derived from repeated task evidence. They are
never written into the active `skills/` tree.

## Sources and thresholds

`orchestrator skills learn` deterministically mines three sources:

- ordered per-role tool-call signatures from the Jev gate log, with targets
  reduced to path stems, script paths, or command verbs;
- case-folded instruction sentences of at least eight words repeated across
  execute, scout, and review specifications;
- repeated Planner notes or failure kinds on fix-round specifications.

A pattern needs distinct-task support of at least `--min-support` (default 3)
and at least 80% accepted or merged outcomes. A one-off is not a candidate.
Confidence is successes divided by support. Evidence strength is separately
reported as `min(1, support / 10)` and only orders results. Proposals below the
support threshold or 0.6 confidence are refused; later weak evidence marks an
existing record stale.

## Draft contents

The compact P6 draft contains Trigger, Objective, Procedure, Tools, Evidence
requirements, Output contract, Stop conditions, and Failure/recovery. Its
learned provenance block records source tasks, successes, counterexample
failures, the procedure signature, confidence, evidence strength, and synthesis
time. Re-proposals update the same signature's version and lifecycle history.

Drafts live only at
`STATE/skills/quarantine/learned/<slug>/SKILL.md`, are registered as discovered,
and immediately transition to quarantined. `orchestrator skills show <id>`
shows the record and evidence.

## Promotion and reversion

Nothing is automatically trusted, executed, tested, shadowed, or activated.
The inspect gate must pass before testing; successful evaluation can lead to
shadow mode, and only Stage 10 evidence-driven promotion can make a skill
active. Human review and all normal policy gates remain required.

To revert a proposal, delete its learned quarantine folder and its entries in
`discovered.json` and `state.json`. Active skill files are unaffected.
