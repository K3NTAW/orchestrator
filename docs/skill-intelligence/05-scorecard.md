# Skill scorecard

The scorecard is descriptive and read-only. It joins each skill-bearing run to the execute root of its task graph, including fix, review, spec-review, and unambiguous scout relationships. A run without a resolvable execute root keeps outcome fields unknown.

## Dimensions and metrics

`by_skill` groups by skill and any combination of `role`, `task_class`, `band`, `model`, `strategy`, and `repo`. The repository dimension is `orchestrator`. Counts are runs; boolean metrics are rates; numeric metrics are arithmetic means. Missing measurements are `None`, never zero.

Lineage outcomes are first-pass success, fix rounds, gate reds, review requests for changes, acceptance, accepted tokens and USD, latency, and tool calls. Accepted cost is reported only for accepted roots. Skill overhead is mean level-2 skill tokens per use.

## Marginal value

For each group, the comparison is runs that used the skill versus runs that neither used nor selected it. Every delta is:

`mean(with skill) - mean(without skill)`

The reported deltas cover first pass, fix rounds, accepted tokens, accepted USD, and latency. Both sides must meet `promotion.min_samples` (default 20), otherwise the result is marked insufficient.

Verdicts are reporting labels:

- `valuable`: quality is no worse and tokens do not increase, or first-pass quality improves by more than 0.05 while tokens rise by at most 10%.
- `harmful`: first-pass quality declines.
- `costly`: tokens increase while quality is unchanged (within 0.05) or unknown.
- `neutral`: all other measured cases.

## Redundancy proxies

Co-use count is the number of runs using both skills. Path overlap is Jaccard overlap of aggregate paths read by each skill in its single-skill runs. It is unknown when either side lacks single-skill evidence. Duplicate scripts are identical `script:<path>` entries in both registry tool declarations. Unique findings remain unknown until Stage 6 evidence sharing exists.

Use `orchestrator scorecard --skills --group-by model`, `--group-by strategy,task_class`, `--marginal <skill>`, or `--redundancy`. These reports do not change routing, promotion, or skill state. Revert by reverting the scorecard commit; no data migration or state rollback is needed.
