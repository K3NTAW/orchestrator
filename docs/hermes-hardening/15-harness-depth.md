# Harness depth and small-task fast path

`harness_depth.level(task, *, tasks, cfg, history)` returns a deterministic level,
ordered reasons, and `eligible_fast_path`. It does not mutate tasks or read state.
The daemon calls it after `bus.ready()` admits dependencies; unresolved dependencies
are therefore not a depth rule. `tasks` is retained in the interface for callers.

Rules run in this order:

| Level | Rule |
| --- | --- |
| 4 | Architectural constraint or Planner taxonomy, security scope path, migration scope path, complexity at least 8, or goal container |
| 3 | Complexity 6–7 or persisted `constraints.route = "spec_review"` |
| 2 | Complexity 4–5, more than three scope files, or a scope glob whose file count is unknown |
| 1 | Complexity at most 3 and at most three literal scope files |
| 0 | Level 1, all non-gate acceptance lines are test IDs, and class first-pass rate is at least 0.8 with at least five defined samples |

Security patterns come from `[review].security_paths`; migration patterns come
from `[review.areas.migrations].paths`. Architectural classification uses
`planner_taxonomy.classify` at dispatch, including interface and migration area
signals. Existing real tasks carry architecture through this classifier, rather
than a persisted architectural constraint. The explicit constraint is also
honoured when a Planner supplies it. The taxonomy already considers complexity 7
architectural, so that level-4 rule precedes the nominal complexity-7 level-3 rule.

History is `scorecard.efficiency(by="class")["groups"]`. The sample denominator
is `first_pass_defined_count`, never accepted task count. Unknown first-pass
outcomes cannot create level 0. The daemon constructs the history table once at
the start of dispatch in each tick and stores it on the pool; every candidate
uses the same table.

## Modes and boundaries

`[harness].depth_mode` ships as `shadow` (2026-09-23). `off` disables observation.
Shadow records one `harness_depth` decision per execute dispatch, with candidates
0–4, selected level, reasons, and `extra.would_skip`; it does not stamp a level
or change the packet. Active stamps `pipeline.harness_level` and
`pipeline.harness_mode`; only levels 0–1 take the fast path.

The skip list is `jev_route`, `skill_routing`, and `spec_review`:

- The daemon's `_dispatch_worker` bypasses its Jev route call.
- Claude `run_worker` bypasses `_prepare_skills` and uses `skills=None`, including
  its subsequent skill telemetry path. Other roles are not implicitly marked
  fast just because they review a fast task. The shared preparation helper also
  returns no choice for a stamped task reaching the Codex preparation boundary.
- Dispatch bypasses spec review for an eligible fast task, including a lowered
  configured spec-review threshold. An explicit spec-review route is level 3.

The context router on this branch is pure deterministic routing and contains no
Jev call. It remains enabled. There is no strategy lookup in dispatch and no scout
suggestion mechanism, so neither appears in `would_skip`. The executor's own
active model-routing policy remains separate from the daemon's Jev observation.

Hooks, tests-green, security review based on the actual changed paths, and human
PR review remain unchanged. A task whose declared scope matches a security path
is level 4. A small declared scope followed by a security-sensitive diff still
triggers the existing security review at the gate.

Each active fast dispatch emits exactly one empty `skill_selection` decision
with reason `fast_path`, and a `jev_route` run row with the same reason. These
explicit skips retain dispatch samples for skill-routing and Jev-routing
promotion reports; they are not Jev classifier verdicts or skill exposures.

## Promotion

`promotion.FEATURES["fast_path"]` maps to the harness depth mode. Initial
promotion requires at least 20 shadow depth rows at level 0–1. Once accepted
active outcomes exist, their defined first-pass rate must be no lower than the
shadow baseline and mean lineage tokens per accepted task must be lower.
Overlapping subjects are excluded from the shadow comparison. Unknown quality
or token comparisons cannot sustain active operation. A fast task needing two
fix rounds causes demotion, even before enough samples accumulate; linked fix
chains and recorded lineage counts are both checked, including unmerged tasks.

Before applying skips the daemon calls `promotion.evaluate("fast_path", ...)`.
An unmet criterion runs that tick as shadow and emits one refusal notification.
The configuration is not rewritten. Promotion remains a human configuration
choice, and this evidence is observational rather than a causal estimate.

## Real bus distribution

Snapshot on 2026-09-23, using the main checkout's `.orchestrator/tasks` and runs
(the task worktree has no task bus): all 338 merged execute tasks, including
historical fix tasks, classified with this branch's pool configuration and
current class history.

| Level | Tasks | Share |
| --- | ---: | ---: |
| 0 | 0 | 0.00% |
| 1 | 17 | 5.03% |
| 2 | 2 | 0.59% |
| 3 | 0 | 0.00% |
| 4 | 319 | 94.38% |
| Total | 338 | 100.00% |

The broad `orchestrator/*.py` security pattern dominates this repository.
Mechanical history has nine defined samples with first-pass rate 7/9, below the
level-0 threshold. This is a retrospective distribution, not promotion evidence:
only newly recorded shadow rows count toward the 20-row threshold.

To reproduce, load every task JSON from the main checkout, select
`role == "execute"` and a truthy `merged_into`, compute `history_table(root)`
once, and count `level(task, tasks=all_tasks, cfg=pool_config, history=history)`
by its `level` field. No task or run files need to be changed.
