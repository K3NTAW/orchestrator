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

Executor shadow decisions record the baseline executor, eligible candidates,
task class, and measured costs, but do not change routing.

## Active

With `[handoff] mode = "active"`, the post-allocation baseline is replaced by
the eligible executor with the lowest expected route cost only when every
eligible executor has at least `[promotion].min_samples` observations and the
relative saving exceeds `min_gain` (default 0.15). Pool eligibility, cooldowns,
complexity ceilings, and budgets remain authoritative. Active allocation takes
precedence: handoff records `allocation_active` and preserves its choice.

Each dispatch stamps `pipeline.handoff` with the baseline, selection, switch,
gain, reason, and timestamp. At most `max_active_share` (default 0.5) of the
local day's stamped launches are switched, preserving baseline comparison
data. Set `[handoff] mode = "shadow"` to revert active routing while retaining
measurement, or `"off"` to disable handoff decisions.

Run `orchestrator scorecard --handoffs` for the text report or add `--json` for
machine-readable output. To revert collection, set `[handoff] mode = "off"`.
