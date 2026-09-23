# Orchestration overhead

`orchestrator scorecard --overhead` reports the orchestration work attached to each accepted goal. Add
`--goal T-dddd` for one goal or `--json` for machine-readable rows. The report ends with aggregate totals and
column medians.

## Definitions

- **Orchestration tokens** are normalized tokens for planner, planner-decision and shadow-planner work, scouts,
  reviews, challenges, JEV decisions, memory, routing, and environment-policy runs. If a row contains provider
  usage, the bus usage normalizer supplies `total_tokens`; otherwise the row's `total_tokens` or `est_tokens` is
  used. Planner transcript tokens are attributed from `planner_usage.json` when available, in proportion to each
  goal's goal-tagged rows for the current day. Explicit per-goal planner-run tokens take precedence.
- **Execution tokens** are normalized tokens for `execute` and `fix` runs.
- **Amplification** is orchestration tokens divided by execution tokens. It is undefined when execution tokens
  are zero.
- **Cost share** is orchestration-run USD divided by all measured run USD for the goal. Planner transcript usage
  has no USD field, so it adds tokens but not estimated cost.
- **Goal seconds** is wall-clock time from the first to the last goal-tagged run row. **Latency share** is the sum
  of orchestration-run durations divided by that wall-clock interval. Concurrent runs can therefore make the
  share exceed 1. A single timestamp has a zero interval and an undefined share.
- **Unattributed tokens** are tokens in run rows without a `goal_id`. They are reported separately and are not
  assigned speculatively.

## Repository measurement

Measured on 2026-09-23 from this repository's `.orchestrator/runs` data. Thirteen early accepted goals have no
goal-tagged run rows because they predate this telemetry; their zeroes mean “not measured,” not “no overhead.”
Planner transcript attribution reflects the current-day `planner_usage.json` snapshot and will change as that
daily counter advances.

| Goal | Orchestration tokens | Execution tokens | Amplification | Orchestration USD | Goal USD | Cost share | Orchestration s | Goal s | Latency share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| T-0001 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0004 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0005 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0043 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0065 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0073 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0109 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0201 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0240 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0260 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0353 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0445 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0489 | 0 | 0 | — | 0.00 | 0.00 | — | 0.0 | 0.0 | — |
| T-0561 | 41,610,101 | 32,039,915 | 1.299 | 26.27 | 272.30 | 0.096 | 8,582.6 | 25,207.2 | 0.340 |
| T-0674 | 39,681,287 | 17,216,973 | 2.305 | 22.77 | 156.22 | 0.146 | 7,107.0 | 7,509.4 | 0.946 |
| T-0755 | 1,312,093 | 1,590,817 | 0.825 | 0.71 | 13.02 | 0.055 | 214.8 | 1,730.3 | 0.124 |
| T-0760 | 71,058,526 | 46,470,766 | 1.529 | 42.53 | 403.72 | 0.105 | 11,421.6 | 37,525.1 | 0.304 |
| **Total** | **153,662,007** | **97,318,471** | **1.579** | **92.29** | **845.26** | **0.109** | **27,326.0** | **71,972.0** | **0.380** |
| **Median** | **0** | **0** | **1.414** | **0.00** | **0.00** | **0.101** | **0.0** | **0.0** | **0.322** |
