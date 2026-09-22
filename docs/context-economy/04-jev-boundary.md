# Jev outbound privacy boundary

Every production site declares at import time and identifies itself to `jev.ask`.
The disabled gate avoids network imports; its declaration registers when Jev loads.
Bound adapters preserve older injected transports that accept only two arguments.
Fields below are the exact top-level state keys; parentheses describe nested data.
All sites disallow raw source by contract. This flag documents caller intent;
redaction does not classify arbitrary text or prove that it contains no source.

| Site | Fields | Caps | Redaction | Mode |
| --- | --- | --- | --- | --- |
| gate | task (title, spec, acceptance, scope, role), recent, proposed (tool_name, input) | spec 1500; 20 recent calls; recent/proposed input 300 each | tool inputs locally; state centrally | sample/log/block |
| route | spec, acceptance, scope, complexity, task_class, memory_titles | spec 1500; 10 memory titles | text locally; state centrally | off/shadow/active |
| planner | decision_type, band, task_class, architectural, ambiguous, risk_class, signals, subject_id, subject_title, spec_excerpt, goal_title, scope_count, scout_count, failing_test_count, spec_review_request_changes, auto_fix_rounds_used, signature_repeated | state 4000; titles 200; spec 300; 10 signals × 100 | text locally; state centrally | off/shadow/active |
| sched | pairs (a, b, titles, scope, spec_excerpts, reasons) | default 8 pairs; spec 400 each | text locally; state centrally | off/shadow/active |
| points | title, spec, acceptance_count, scope, complexity, task_class, memory_titles, deterministic_evidence | title 300; spec 600; 100 scope × 300; 10 titles × 200; evidence depth 5, 100 entries, strings 600 | text locally; evidence content fields stripped; state centrally | off/shadow/active |
| rank | string state: goal_text; question criteria: item texts | default 40 items/batch | goal and item texts inside rank; state centrally | optional, fail-open |

Each declaration caps serialized state at 100,000 characters except planner (4000).
The effective cap is the smaller of that declaration and `[jev].max_state_chars`.
Central redaction still runs before truncation. Rank criteria are question data,
so they are redacted locally and are outside the serialized-state character cap.

Unknown top-level dictionary keys are dropped without mutating the caller's state.
Each such call increments `undeclared_calls` and emits one fixed warning via notify.
Legacy calls without a declared site still work, with the same counter and warning.
Warnings contain neither payloads nor key names. Nested fields remain caller-owned;
the registry does not recursively validate schemas or inspect files.

## Data-class policy (P19)

`[jev].max_data_class = "INTERNAL"` permits PUBLIC and INTERNAL, refuses SENSITIVE
and SECRET. `ask(data_class=...)` defaults to INTERNAL. The configured ceiling uses
PUBLIC < INTERNAL < SENSITIVE < SECRET; unknown classes/ceilings fail closed.
Refusal returns None before key lookup or transport, increments `refused_calls`,
and emits a fixed warning. No current caller labels data SENSITIVE.

## Revert

Set `[jev].enabled = false` to stop all Jev requests while retaining local fallbacks.
Revert the T-0774 commit to remove declarations, enforcement, and the policy key
as one change; existing redaction and fail-open behavior predate this boundary.
