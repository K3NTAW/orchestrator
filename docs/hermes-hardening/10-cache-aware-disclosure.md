# Cache-aware disclosure

## 1. Context routing shadow

Every routed evidence item records its cacheability, effective cache-write cost, and cache-adjusted
level. Cacheability is evaluated in order: worker-partial provenance is dynamic; test results are
dynamic; source chunks pinned to the routing head are stable; everything else is semi. Effective
cost is the presented text's character count divided by four, multiplied by one plus the provider's
cache write ratio. Configured ratios override the telemetry defaults; a missing provider produces no
cost.

The only adjustment is `LONG` to `SHORT` when an item is dynamic, its effective cost exceeds
`cache_downgrade_tokens` (default 600), and its routing reason is a generic source-type fallback:
`test_result` or `worker_partial`. These fallbacks apply only after all named rules have been checked.
All named-rule reasons are exempt: `in_scope_file`, `security_path`, `failing_output`, `acceptance`,
`skill_required_context`, `read_scope`, `section_items`, `prior_worker_overlap`, `unrelated`,
`unrelated_memory`, `stale`, `ambiguous`, and `dependency`. `HIDE` never changes. Shadow mode records the proposed
level without changing presented context. Active cache mode changes presentation only while the
effective context-router mode is also active.

Reproduce the aggregate with:

    orchestrator scorecard --context --cache-shadow

It reports the number and date range of `context_selection` rows and the share of evidence items whose
cache-adjusted level differs from the explicit pre-adjustment `presented_level` map. This also counts
adjustments applied in active mode. Older rows without that map are excluded from the share; when no
comparable items exist the share is `unknown`. Both reports use the same root. With no matching rows it prints
`no context_selection rows`.
