# Context router v1

The library represents context as immutable, content-addressed `Evidence` and
routes each candidate deterministically to `HIDE`, `SHORT`, `LONG`, or `FULL`.
Execute and review packet builders use it for shadow telemetry and guarded active routing.

## Evidence schema

`Evidence` records a 12-hex content-addressed id, source type, location, commit,
full content hash, caller-bounded raw content, short (160 character) and long
(600 character) summaries, task-relative relevance, provenance, observation
time, and derived trust. Repo and memory provenance are trusted. The goal-bound
`EvidencePool` writes JSONL under `.orchestrator/evidence/`; it applies the local
`jev.redact` sanitizer before persistence and deduplicates by id.

## Routing rules

Rules are first-match-wins. A task's own spec/acceptance and relevant failing
test output are full. Unrelated memory-like evidence and stale source/test
evidence are hidden. Matching prior results, findings, decisions, and memory
are long. Unmatched cases are long and explicitly placed in the ambiguous
bucket.

| Role | In-scope source | Other source | Tests | Role-specific evidence |
|---|---|---|---|---|
| execute | FULL | SHORT | failing/error FULL | base rules |
| review | LONG | SHORT | FULL | review/prior results SHORT |
| security_review | LONG (security glob FULL) | SHORT | FULL | review/prior results SHORT |
| planner | SHORT | SHORT | failing/error FULL | decisions/architecture LONG |
| scout | FULL | SHORT | failing/error FULL | base rules |

Unknown roles use the execute profile. Scope accepts `scope` or its
`write_scope` alias. Relevance is recomputed for every task; the value stored on
an Evidence object is only a cache for the task that created it.

## Shadow logging

Packet builders log a `context_selection` decision containing only
ids and levels, full-selection reasons, per-level counts, token estimates,
reduction ratio, ambiguity count, mode, and rules version. It will not log
evidence content or chain-of-thought.

## Shadow wiring

Execute and review packet builders now create goal-bound evidence candidates and
route them whenever `[context_router] mode` is `shadow` or `active`. The original
packet body is sent unchanged in shadow; headers identify the effective mode. Selection decisions are recorded as `context_selection` rows, while
packet and run context metadata carry routed token estimates, reduction ratio,
hidden and ambiguous counts, rules version, and up to 200 evidence ids.

`orchestrator scorecard --context` reports average shadow routed tokens,
reduction, hidden candidates, and ambiguous-candidate rate for each role and
goal. `shadow_unmeasured` counts runs that predate the wiring or lack routing
metadata without treating their ordinary packet telemetry as unmeasured.

To disable the experiment, set `[context_router] mode = "off"` in `pool.toml`.
To remove the wiring entirely, revert its implementation commit.

## Not in this task

- Jev ambiguity classification (P5)
- Query-time summarisation and summary caching (P7)

## Active

Set `[context_router] mode = "active"` in `pool.toml` after running
`uv run orchestrator context-eval`. Active is refused with a notification and
falls back to the shadow packet when `.orchestrator/context_eval.json` is
missing, malformed, more than seven days old (using `ran_at`), or does not have
`suite_passed = true`. The evaluation table includes `active`, the complete
active packet length in characters, and checks protected sections in both modes.
To revert immediately, set the mode back to `"shadow"`.

Execute replaces only gotchas, decisions, evidence, and read_scope. Each
candidate is tagged with its section in `Evidence.relevance["section"]`.
HIDE items are omitted; SHORT gives a one-line summary, LONG a longer summary,
and FULL the verbatim content. Read-scope source candidates are always SHORT.
In-scope source files are never injected: FULL means the executor opens the
file. Symbols and the task contract, verification, dependencies, and test
sections retain their existing behavior. The 4,800-character cap includes the
header; trimming removes whole routed items, SHORT before LONG before FULL.
As before, an oversized task contract is kept whole and flagged in the header.

Review and security review put routed findings and previous results under
`## routed-findings`, immediately after fix-round context when present.
The legacy fix-round heading remains but findings move to the routed section.
Diff and security sections retain their existing rendering and diff budget.
Headers end with `routed=active` or `routed=shadow`.

The read-economy gate resolves the most recent task selection through the
same goal evidence pool. An allowed Read of a source selected HIDE or SHORT
under active mode logs `evidence_reuse` with reason `recovery_read` and the
evidence id. FULL, LONG, and shadow selections do not count. P21
`recovery_rate` is recovery Read events divided by HIDE items in active
selection rows, per role; a zero denominator reports zero. SHORT recovery
reads count in the numerator, so this diagnostic can exceed one. Both
`scorecard --economy` and promotion evidence use these same rows.
