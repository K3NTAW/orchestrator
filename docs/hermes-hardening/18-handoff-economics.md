# Handoff economics

`orchestrator scorecard --handoffs` reports cache retention, lost cache, uncached reconstruction,
packet size, duplicated evidence, latency, and effective cost for each observed handoff. Effective
handoff cost is uncached reconstruction plus lost cache weighted by the successor provider's cache
read ratio, plus packet tokens. The text view includes medians per handoff kind; JSON preserves the
individual rows.

The report covers Planner escalation and execute fix-round model changes. Same-model fix rounds are
retained as a separate kind so their reconstruction cost remains visible. Candidate route-cost
objects also carry the measured effective handoff cost in shadow; selection behavior is unchanged.

## Read-only observation, 2026-09-23

The command was run read-only with the explicit root
`/Users/k3ntaw/code/orchestrator/.orchestrator`. It found 144 reconstructable pairs. Median effective
handoff cost was 186,027.25 token-equivalents for executor changes, 136,206.875 for same-model fix
rounds, and 16,844.45 for Planner escalations. Median latency was 448.41 seconds, 403.15 seconds,
and 6,568.17 seconds respectively. Median packet sizes were 1,373.25, 1,522.625, and 955.25 tokens.

Pre-task routing rows lack the derived handoff cache fields; the report reconstructs them from the
constituent run rows where those rows contain normalized cache usage and packet metadata. A field is
reported as unmeasured when the required packet size or event time is absent.
