# Hermes promotion, evaluations, and success metrics

## Promotion controls (P33)

Cache-aware disclosure groups three independent features. Each entry owns one
configuration key; evaluating a recommendation never rewrites configuration.

| Feature | Configuration | Minimum shadow rows |
| --- | --- | ---: |
| context_cache | context_router.cache_mode | 30 |
| tool_cache | tool_disclosure.cache_mode | 30 |
| skill_cache | skills.cache_mode | 30 |
| stale_steering | steering.stale_mode | 20 |

These four features require measured non-inferior first-pass and fix-round
rates, lower effective tokens or USD per accepted goal, and a passing Hermes
evaluation no older than seven days. Missing economics or quality never promotes.
The decision row's cache_mode identifies cache cohorts independently of the
containing router's mode. Stale steering uses only steering rows whose trigger
is stale_severity. Missing or invalid stale_mode collects invalid_config and
uses the shadow default. This branch does not contain the expected upstream
stale_mode reader; wiring that reader belongs to the upstream steering task.

A subject appearing in both modes belongs only to active. Quality comes from
JSON tasks under the supplied state root: first pass means no task points to
that subject with constraints.fix_round_for; fix-round rate is mean direct fix
rounds per subject. Subjects without task records cannot establish quality.
Economics groups those subjects by accepted parent goal and compares observed
run costs per accepted goal. Goals shared by the two task cohorts contribute
the same complete goal cost; these observations do not establish causal savings.

Demotion is stateless: compare the most recent ten distinct active subjects
with the disjoint shadow cohort on every call. A lower first-pass rate or a
higher mean fix-round count demotes an active feature. Fewer than ten active
subjects cannot cause this demotion. Memory tiers gains this windowed demotion;
its existing generic promotion path is otherwise preserved. Fast path retains
its 20-row threshold and two-fix-round demotion. Steering policy retains its
existing 20-row and economics rules. Neither receives the new freshness gate.

`promotion.evaluate(feature, evidence, cfg, root=..., now=...)` refuses with
`hermes_eval_missing_or_stale` for a missing, malformed, failed, future-dated,
or expired report. Omitting root skips only this freshness check and records
`eval_root_missing`; it does not substitute production state. The cache-mode
reader supplies the runtime state root by default, accepts an explicit fixture
root and clock, and returns shadow with a notification unless promotion passes.
Shadow observation preserves worker-visible context. Runtime refusal does not
change the user's configured active value.

## Deterministic evaluations (P34)

Run `orchestrator hermes-eval --json`; `--root PATH` selects the report root.
`hermes_eval.run(root, now=None)` persists only hermes_eval.json in that root's
.orchestrator directory (or directly in an explicitly supplied state directory).
It records ran_at in Europe/Zurich, individual named cases with passed/detail,
and an overall boolean passed. Freshness uses the same injectable clock.

Every fixture group runs in an isolated Python subprocess with its own temporary
ORCH_ROOT, without changing the caller's environment or module state. Memory
calls memory_eval.run against its fixture. Cache uses the shipped role template,
spawn.render, normalize/effective_cost, and tool_catalog.cache_view. Worker
control uses a temporary Git worktree, fake task bus and pool, and injected
sleep/alive/signal delivery; it never signals a real worker or invokes a model.
Security discovers and inspects an inert local external skill, exercises the
registry transition guard, and scans malicious and harmless context. Fast path
uses harness_depth.level and synthetic decision/task rows.

The named cases cover:

- Memory: current HOT decision, WARM retrieval, COLD provenance, irrelevant
  record exclusion, and knowledge preservation after compaction.
- Cache: stable role prefix, differing task suffix, normalization and effective
  cost, and stable tool catalog across differing selections.
- Workers: running-worker inspection, durable steering before delivery,
  cancellation releasing capacity while retaining partial files and tests,
  and replacement prior_worker facts routed from preserved evidence.
- Security: high-risk external skill refused testing, malicious agents file,
  false authority escalation, secret-exfiltration instruction, and harmless
  quoted security documentation.
- Fast path: trivial task plus exact skip list, security minimum depth four,
  architectural depth four, and demotion after two fix rounds.

Fixture inputs contain synthetic names and instructions, never secret values.
No external skill code executes. A failed group makes the overall report fail.

## One-table scorecard (P35)

`orchestrator scorecard --hermes [--json] [--root PATH] [--days N]` joins existing
accepted-goal, efficiency, memory, cache, and overhead reports with observable
worker, context, and inspection records. It performs no compaction or evaluation.
Missing measurements display unknown (JSON null), rather than inferred success.

| Metrics | Definition and source |
| --- | --- |
| accepted_goal_success | Accepted goals divided by root goal/triage tasks |
| first_pass_rate, fix_round_rate | Existing accepted-task efficiency rates |
| tokens_per_accepted_goal, usd_per_accepted_goal | Existing accepted-goal scorecards |
| effective_uncached_tokens_per_accepted_goal | Provider-weighted cache economics for recorded accepted-goal runs |
| latency_per_accepted_goal | Sum of recorded run durations per accepted goal; not critical-path wall time |
| hot_memory_tokens | Current HOT view characters rounded up to token estimate |
| retrieval_precision, retrieval_usefulness | Used/presented and used/retrieved records from the memory scorecard |
| compaction_count | Recorded memory_compaction events; currently unmeasured by the producer |
| cache_hit_ratio, cache_read, uncached | Cache telemetry report buckets |
| cache_invalidations | Recorded disclosure selection changes; a proxy, not provider eviction telemetry |
| effective_context_cost | Cache telemetry provider-weighted token equivalents |
| steering_rate, steering_success | Active steer proposals/active steering decisions; applied/proposed steers |
| cancellations | Current cancelled worker snapshots |
| partial_result_reuse | Active decisions presenting prior_worker evidence |
| fix_rounds_avoided | Explicit recorded avoided rounds; unmeasured when absent, not inferred causality |
| suspicious_context_detected | Unique suspicious or blocked evidence/skill artifacts |
| false_positive_rate | Detected artifacts later marked safe by a human-reviewed transition reason into testing, shadow, or active, divided by detected artifacts |
| blocked_imports | Inspection reports currently blocking ordinary imports through high risk or blocked scan |
| orchestration_amplification, orchestration_cost_share, orchestration_latency_share | Existing overhead totals across accepted goals |

Memory retrieval and cache telemetry use the requested day window. Accepted-goal
and efficiency metrics retain their existing all-time attribution; worker and
security metrics describe persisted current records. This report does not claim
that all rows share a common experimental cohort. No production measurements
are claimed here and no dependencies were added.
