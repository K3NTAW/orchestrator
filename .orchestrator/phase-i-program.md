# Phase I program: Orchestrator efficiency and Jev decision layer (user brief, 2026-09-20 17:30)

Goal: reduce tokens, cost, unnecessary model calls and fix-round amplification without reducing accepted-task quality, safety, first-pass success or human control. Jev evolves from a tool-call gate into an optional fast decision signal for orchestration. Evidence-driven. Optimize for minimum resources per accepted task/goal subject to non-inferior quality, never raw token reduction. Implement in priority order; never activate probabilistic optimizations before the telemetry to evaluate them exists.

## P0 Measurement foundation (first)
Phase G's 5.96M tokens per accepted goal is history, not the current baseline. Attribute usage to: Planner, scouts, executor, executor fix rounds, spec review, code review, challenge, memory recall (where measurable), Jev, other/unclassified. Track per task, goal, role, executor/model, complexity band, task type where available.
Metrics: tokens per accepted task; tokens per accepted goal; tokens to first green (creation until first tests-green pass); first-pass success rate (execute tasks green without an executor fix round); fix-round rate; average fix rounds per task; tokens on fix rounds; cost per accepted task and goal; calls and turns per accepted task; time to first green; time to accepted task; model distribution.
pipeline_amplification = total_pipeline_tokens / executor_tokens, with the breakdown Planner, Scout, Execution, Fix rounds, Spec review, Code review, Jev, Other, Total, Amplification. Observation metric only.
Preserve baseline data so later phases compare Jev off vs shadow vs active, before/after context optimization, by executor/model, complexity, task category, review configuration. No production Jev routing on global averages.

## P1 Role-specific context compilation
Deterministic packets per role. Executor: spec, acceptance, scope, dependencies, relevant memory, relevant scout findings, necessary repo context; never unrelated completed tasks, full goal history, unrelated memory, all scout output, verbose bus history. Review: spec, acceptance, relevant diff, relevant tests and gate result, necessary security/risk context. Scout: exact question, bounded repo context, relevant memory titles/evidence, expected output structure. Spec review: only what decides implementable, scoped, testable. Memory recall cheapest-first (local notes, bus evidence, claude-mem, graphify) and stop when sufficient. Deduplicate unchanged context within a workflow where safe; cache or reference deterministic context; never reuse stale context after relevant state changes. Measure input-token sizes by role before and after; success = lower tokens without meaningful degradation in first-pass success, gate success, review defect detection, accepted quality.

## P2 Fix-round economics
Scorecard per executor/model and complexity band / task category: initial execution tokens, first-pass green rate, fix-round probability, average fix rounds, total tokens and cost to accepted task, gate failure reasons, review request_changes rate. Evaluate executors on cost per accepted task, not cost per initial execution; measure, do not hard-code. Preserve Codex resume for fix rounds; avoid rereading unchanged state.

## P3 Jev executor routing, shadow mode
Task -> hard eligibility constraints -> Jev classification -> scorecard evidence -> candidate ranking -> executor. Pool answers which executors are allowed and available; Jev what characteristics the task requires; scorecard which eligible executor performed well on comparable work. Jev never replaces pool or scorecard, never controls routing in shadow mode. Few high-value typed questions (localized/simple? substantial reasoning? large-context? debugging/reproduction? architectural? elevated risk? stronger executor materially improves first-pass?). Privacy: no repository file contents; bounded/redacted spec, metadata, memory titles, safe structured state. Hard constraints authoritative: enabled, role, complexity range, parallel capacity, daily budget, quota cooldown, account restrictions, fallback rules, hold policy. No Jev call when it cannot change the decision (one eligible executor, constraints decide, disabled, budget exhausted, valid cached classification). Batch classifications in one request; invalidate on material task change. Fail open to baseline routing on disabled/failure/timeout/budget/malformed/insufficient evidence.

## P4 Routing telemetry and evaluation
Record baseline-selected executor, Jev-assisted hypothetical executor, eligible candidates, Jev signals/probabilities, scorecard evidence, candidate scores, Jev latency, usage/cost, actual outcome (first-pass green, fix rounds, gate, review, total accepted-task cost). Compare by complexity, task type, executor, classification. Disagreement is not improvement; the hypothetical must predict better downstream outcomes.

## P5 Active Jev routing
Only after sufficient shadow evidence. Config off | shadow | active, default safe. Active modifies ranking among eligible candidates: candidate_score = configured_weight x empirical_score x jev_suitability_adjustment (design from measured data). Extreme probabilities must not overwhelm strong empirical evidence. Cold start safe. Failure reproduces baseline.

## P6 Review token optimization
Only after P0 review telemetry. Never remove reviews to save tokens; current policy is the floor. Two-reviewer tasks: complementary roles (A: acceptance, correctness, regressions, tests; B: adversarial, security, edge cases, hidden assumptions). Security-path requirements stay deterministic. Smallest sufficient review packet; measure effect on findings.

## P7 Experimental Jev decision points (each introduced independently)
Scout necessity; context escalation; review escalation (never overrides mandatory security review); Planner relaunch. Keep measuring the PreToolUse gate independently of routing.

## Safety invariants (deterministic unless separately reviewed)
Planner cannot edit source; scope guard enforced; tests-green required to merge; security paths get security review; hooks are a hard floor; task dependencies enforced; merge serialized; human approval for main; probabilistic Jev decisions cannot bypass hard pool/safety constraints.

## Testing requirements
Telemetry: task attribution, goal aggregation, fix-round attribution, role attribution, unknown usage, amplification, first-pass success. Context: role packets contain required info, exclude unrelated state, stale context invalidated, relevant memory available. Jev routing: off, shadow, active, unavailable, timeout, malformed, budget exhausted, single eligible, multiple eligible, cooldown, sparse scorecard, strong historical evidence, strong conflicting Jev signal, classification caching and invalidation, shadow never changes routing, active can affect ranking, failure reproduces baseline. Safety: Jev cannot bypass hard routing or merge constraints.

## Evaluation standard
Success = total tokens/cost per accepted task and goal down, subject to non-inferior first-pass success, tests-green outcomes, review defect detection, accepted quality, security requirements, human control. Secondary: time to first green, fix-round rate, amplification, calls/turns, routing latency, model distribution. If an optimization reduces quality, revert it even if it saves tokens.

## Planner instructions
Multi-phase program, not one task. Inspect existing telemetry, scorecard, routing, context construction, Jev integration, review prompts, fix-round paths first; recall memory before scouting; scouts only where uncertainty warrants; atomic tasks with acceptance, dependencies, scope; complete and measure earlier priorities before later ones alter production behaviour; record architecture decisions and revert paths in memory; compare each phase against the previous baseline and preserve results.
