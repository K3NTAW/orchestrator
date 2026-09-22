# Read economy shadow gate

The PreToolUse gate classifies `Read`, `Grep`, and `Glob` calls before any Jev request.
The deterministic kinds are `first_read`, `repeated_read_unchanged`,
`repeated_read_changed`, `repeated_search`, `narrower_search`,
`evidence_available`, and `other`.

An unchanged read has the same path, modification time, and size as an earlier read.
An exact search repeats tool, path, pattern, and flags. A narrower search keeps the path
and contains an earlier pattern. Fresh goal evidence produces `evidence_available`.

Shadow rows in `runs/jev/gate.jsonl` contain `read_kind`, `tokens_estimate`, and
`would_suppress`. Every classified call also records an `action_gate` decision whose
selected action is `allow`. `orchestrator scorecard --reads` groups counts by role and
task class, including avoidable tokens, avoided Jev calls, and the false-suppression
proxy (a proposed suppression followed by an edit to the same path within five calls).

## P18 rule

Jev is used only for ambiguous semantic equivalence: changed repeated reads, narrower
searches, or another read/search kind with a prior call on the same path. First reads
never consume Jev budget.

An active policy would suppress deterministic repeats with a structured
`already read at <call>` reply. It is disabled until the false-suppression proxy is zero
over the configured minimum sample count. `block_repeats` remains false and
`guardrails.sh` remains the sole destructive-command authority.

## Active

Set `[jev].read_suppression = "active"` to deny exact unchanged Read repeats and
identical searches before Jev sampling. Different Read ranges and narrower searches
are always allowed. The reply cites the earlier call index; each exact signature gets
only one suppression before an override permanently allows it for that session, and
the session cap defaults to 20 suppressions.

Active mode automatically falls back to shadow unless the preceding seven days contain
at least 50 `would_suppress` rows and `false_suppression_rate` is no more than
`max_false_suppression` (default 0.02). The safety result is cached for ten minutes.

To revert immediately, set `read_suppression = "shadow"` (or `"off"`). This leaves the
classifier, telemetry, existing Jev behavior, and guardrail authority intact.
