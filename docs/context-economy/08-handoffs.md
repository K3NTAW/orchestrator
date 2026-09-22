# Handoff economics

The handoff scorecard groups execute runs into repair lineages. It reports the
first executor, ordered executors and token cost per round, resume versus fresh
repairs, acceptance, and the reason for each executor change.

Expected route cost is:

`average first-round cost + P(fix) × (average fix cost + average reconstruction) + P(handoff) × average destination-round cost`

Every term includes its sample count. An executor/class pair remains
insufficient below `[promotion].min_samples`; no numeric recommendation is
reported for it. `start_strong` means the cheapest first round is not the route
with the lowest measured total expected cost.

Fresh-round reconstruction uses `context.presented_tokens` when recorded.
Otherwise it uses bounded uncached input tokens and marks the lineage as
estimated. Planner escalation packets use recorded payload size divided by
four and are marked estimated. Missing Planner shadow or usage fields are
reported as `unmeasured`, never filled with a guess.

Executor decisions are observation-only. Shadow records the baseline executor,
eligible candidates, task class, and measured costs, but launches the executor
already selected by the existing router. The current `active` value is also
observation-only and emits a warning.

Run `orchestrator scorecard --handoffs` for the text report or add `--json` for
machine-readable output. To revert collection, set `[handoff] mode = "off"`.
