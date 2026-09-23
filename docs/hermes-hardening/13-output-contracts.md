# Worker output contracts

`orchestrator.contracts.SCHEMAS` describes the actual role prompts in
`.orchestrator/prompts/scout.md`, `review.md`, `spec-review.md`, and `challenge.md`.
Execute describes the envelope produced by `executor.post_tool_result`; its
prompt (`execute.md`) requests prose, not a JSON object. The legacy Claude
fallback emits only summary, executed_by and review, and therefore fails the
execute envelope contract. Shadow deliberately preserves that payload.

| Role | Required fields before common defaults |
| --- | --- |
| scout | findings: list of claim:string, evidence:list[string], confidence:number, provenance:list[string]; open_questions:list[string]; suggested_next:list[string]; blocked:string or null |
| review | verdict:approve or request_changes; comments:list of path:string, line:integer, issue:string, severity:low or med or high |
| spec_review | verdict:approve or request_changes; risks:list of path:string, line:integer, issue:string, severity:low or med or high; suggested_spec_changes:list[string] |
| challenge | verdict:confirmed or refuted or uncertain; evidence:list[string]; note:string |
| execute | summary:string; commit:string (empty allowed); executed_by:string; usage:object or null; thread:string or null; rounds:integer |

All roles have confidence (finite number from 0 to 1, default 0.0) and provenance
(list of strings, default repo), matching `bus.post_result`. Execute optionally
accepts files_changed and tests_run as string lists and test_result as pass,
fail or not_run. Review optionally accepts summary:string. Additional metadata
is preserved, including packet_version and previous_commits. Severity uses
`med`, exactly as the prompts do.

`repair(role, result)` returns a copy with deterministic, idempotent coercions:
strings become lists for list fields, numeric confidence strings become numbers,
and verdict aliases become canonical values (approved/LGTM to approve,
changes_requested to request_changes; agree/confirm to confirmed,
disagree/refute to refuted, unknown/partial to uncertain). Case and surrounding
whitespace are normalized. It never migrates findings into comments, renames
files into files_changed, or invents required fields. This follows the T-1112
shape-preserving specification superseding the earlier alias sketch.

`validate(role, result)` returns ok, errors and repaired. Validation includes
common bus defaults. On failure repaired is null; errors contain only schema
paths and error descriptions, never result values. A valid candidate is returned
in repaired even if only the common defaults were added.

`recover(role, result, *, session=None, thread=None, budget_usd=0.5)` is called
only in active mode. It tries deterministic repair, then at most one callable
session/thread adapter receiving a schema-only prompt and the USD cap. An adapter
returns result, tokens and usd; over-budget or invalid replies retain the original
result. The return contains result, repaired_by (none, deterministic or model)
and tokens. Adapters carry their provider/account/worktree context; bare session
identifiers cannot safely launch a process by themselves.

The Claude adapter resumes the same session with the same account and model,
`--max-budget-usd`, a 120-second maximum timeout, no built-in tools, and an empty
MCP configuration. Attempts are logged as output_repair runs with usage; repair
usage joins the reservation's release accounting. A failed attempt returns to
legacy behavior: reviews without a parseable verdict fail, other roles preserve
the original result. “rerun” is a decision label; this feature does not enqueue a
new task. Previously posted review verdicts still take precedence over an
unparseable final response.

Codex posting validates its complete envelope and can apply deterministic
repair. Its current CLI resume interface does not support a hard USD cap, so no
model repair adapter is supplied there. Invalid Codex envelopes retain legacy
behavior. This keeps the recovery budget enforceable rather than estimating a
cap after an unbounded call. A future budget-enforcing adapter can use the thread
parameter without changing the contract interface.

`process` runs at all three posting boundaries: spawn non-execute results,
Claude fallback execute results, and Codex execute results. Mode is read through
`promotion.mode("contracts")` (with the caller's config snapshot). The dated
pool setting starts in shadow. Shadow emits output_contract decision rows only;
it never resumes a worker or changes the stored payload. Off skips validation.
Active applies successful repair. Roll back by setting contracts.mode to shadow
or off; no migration is needed.

Decision candidates are accept, repair and rerun, with a schema:role hard
constraint, role and mode, deterministic ok/error_count/repaired_keys, the first
validation error or valid as reason, and full errors plus raw_ok in extra.
Repaired keys exclude common defaults. Invalid shadow results with an available
bounded session select repair; without one they select rerun. Valid results that
need coercion select repair. Active failures select rerun after the single attempt.

Promotion requires 30 shadow rows, repair success at least 90%, and no increase
in actual failed task results. Collection reads decision rows and reports sample
count, raw ok rate, selected repair rate, and deterministic/model repair success.
Failure-rate delta compares completed task statuses for active versus shadow
subjects, deduplicated within each mode. Missing cohorts remain unmeasured and
block promotion; schema validity is not a substitute for execution success.

## Real repository measurement

Read-only snapshot on 2026-09-23 of
`/Users/k3ntaw/code/orchestrator/.orchestrator/tasks/*.json` in the main checkout.
All tasks with non-null results and a supported role were counted, regardless of
status. No task payloads or logs were modified or copied. “ok” means strict
validation after bus defaults; “repairable” means coercion makes an otherwise
invalid shape valid; “invalid” means deterministic repair still fails.

| Role | Results | ok | repairable | invalid |
| --- | ---: | ---: | ---: | ---: |
| review | 298 | 281 (94.30%) | 0 (0%) | 17 (5.70%) |
| spec_review | 148 | 131 (88.51%) | 0 (0%) | 17 (11.49%) |
| challenge | 2 | 2 (100%) | 0 (0%) | 0 (0%) |
| scout | 33 | 25 (75.76%) | 0 (0%) | 8 (24.24%) |
| execute | 416 | 11 (2.64%) | 0 (0%) | 405 (97.36%) |

These are historical snapshot shares, not evidence of model repair success.
Historical execute envelopes often omit required fields; no missing facts were
inferred to inflate the rate. The snapshot alone does not meet promotion criteria.
To reproduce, load each task JSON read-only, group by role, call validate, then
compare strict schema errors on a copy with common defaults to distinguish ok
from repairable. Record only the aggregate counts.
