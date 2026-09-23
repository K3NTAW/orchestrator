# Steering policy (P14)

The daemon evaluates each running execute task after dispatch. Goal containers
are excluded. The new `[steering]` defaults are `mode = "shadow"`,
`stuck_after_s = 900`, `out_of_scope_events = 3`, and `min_interval_s = 1800`.
Modes are off, shadow and active. Unknown modes fall back to off and emit an
`invalid_config` observation. Shadow mode only writes decisions; active mode can
steer. Cancellation remains shadow-only: active cancel proposals become steers.

`steering_policy.evaluate(task, *, tasks, registry_doc, stale_evidence,
gate_history, cfg, critical, now)` returns action, trigger, reasons and message,
plus deterministic evidence and severity. The stale input is the mapping returned
by `stale.evidence()`: stale_paths, risk, risk_reasons, signals, moved_count and
graph are required. Missing inputs yield continue with `missing_input`; other
unexpected evaluation errors yield continue with `evaluation_error`.

Triggers are evaluated in this order:

1. `security_concern`: stale paths intersect read scope and review security paths.
   Empty review security paths use `orchestrator/*.py`. This is a steer unless
   the task is noncritical and has no commits beyond its merge-base with
   `goal/<parent>`, in which case it proposes cancel.
2. `dependency_changed`: medium or high stale risk with changed read-scope paths;
   steer and name those paths.
3. `stuck`: a running task has no registry event for at least stuck_after_s;
   steer and name the last event time. This interprets stuck as a steering trigger.
4. `out_of_scope`: more than out_of_scope_events distinct dirty paths outside both
   read and write scope; steer and name the paths.
5. `repeated_failure`: the last two stored gate-red signatures on the fix lineage
   are equal and nonempty; steer and name the repeated signature.

Otherwise continue. Messages name the trigger and evidence, never repeat the spec.
The daemon collects gate history by following constraints.fix_round_for through
the root, including constraints.failure_signature only for tasks whose
pipeline.gate_reds is positive, oldest first. It does not recompute signatures.
Critical means first in the scheduler's critical_path rank over running and ready
execute tasks, with the same eligibility, dependency graph and duration estimates.
The daemon passes that ranked list into its decision loop.

Scope matching uses fnmatch for globs and exact/prefix matching for literal
entries. Read scope is derived from bus scope exactly as in spawn._packet_body:
tests/, scope parent directories, and existing local Python imports. A repository
root parent permits reads throughout the repository; no read_scope bus field is
required. Each evaluation runs one `git status --porcelain -z --untracked-files=all`
with a five-second timeout. Renames use the destination path. Paths containing
__pycache__, .orchestrator, .venv, node_modules or .git components and files ending
in .pyc are ignored. Missing worktrees, timeout and nonzero Git exit skip the scope
trigger with `git_unavailable`. The cancel guard runs merge-base and
`git rev-list --count <base>..HEAD` with the same timeout; unavailable Git prohibits
a cancel proposal.

Decisions use kind steering, candidates continue/steer/cancel, standard decision
fields, deterministic evidence and hard constraints. Extra metadata contains
trigger, severity, critical, action, candidate_action, evidence_hash, message_chars
and outcome. Validation requires steering metadata and rejects any key named
message, including nested keys. Decision rows never store the message text or
exception text. Worker control retains its existing redacted delivery record.

Steering reuses the harness depth result passed from dispatch; it never calls
harness_depth.begin_tick or evaluates fast_path promotion. Off returns before
reading tasks or touching other state.

A reverse scan selects the last 500 steering rows for each task, skipping unrelated
kinds and tasks before applying the limit. This preserves interval state across
restarts and unrelated decision traffic. Only successful active steering starts
the interval. Errors and disappeared workers can retry. Shadow observations and
continue observations with unchanged (trigger, evidence_hash) are deduplicated
against the last row in the same mode. Active unchanged proposals are suppressed
during the interval and may act again after expiry. Changed evidence inside the
interval may be logged but cannot steer. Active tasks already steering or cancelling,
or with a pipeline steer epoch newer than their registry epoch, are skipped.

The steering_policy promotion feature requires shadow steer/cancel proposals from at least
20 distinct task lineages. Repeated ticks cannot inflate this count. Each active
tick calls promotion.evaluate for steering_policy; refusal notifies and uses shadow
without changing configuration. Initial activation requires the shadow sample
threshold. Once successful active observations exist, mean fix rounds must be lower than the shadow baseline and
mean accepted-lineage token cost must not rise. Collection uses the retained
steering decision window, counts proposals including downgraded cancellations,
and follows bus fix_round_for chains to count descendant rounds. A lineage counts
once per cohort; successfully steered lineages are excluded from the shadow
baseline. It reports steered/non-steered and shadow means, and uses the existing
efficiency scorecard for accepted-lineage token totals. Missing measured token
usage or a missing comparison cohort blocks promotion. These are observational
cohorts, not a causal estimate. No new dependencies are required.
