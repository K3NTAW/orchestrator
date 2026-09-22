# Context router v1

The library represents context as immutable, content-addressed `Evidence` and
routes each candidate deterministically to `HIDE`, `SHORT`, `LONG`, or `FULL`.
It is pure packet-building infrastructure; spawn-packet wiring is a later task.

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

The later wiring task will log a `context_selection` decision containing only
ids and levels, full-selection reasons, per-level counts, token estimates,
reduction ratio, ambiguity count, mode, and rules version. It will not log
evidence content or chain-of-thought.

## Not in this task

- Jev ambiguity classification (P5)
- Query-time summarisation and summary caching (P7)
- Active routing mode (P21)
