# Adaptive Planner Routing — program brief (user, 2026-09-21 16:00; GOAL id assigned in plan.md)

Objective: replace the static Planner = Fable 5.1 architecture with evidence-driven Planner routing:
deterministic daemon → is Planner reasoning required? → Opus 5 for normal Planner work → Fable 5.1 for
difficult/high-value reasoning → downstream outcomes → Planner scorecard → improved future routing.
Primary objective: substantially reduce unnecessary Fable usage while maintaining or improving the quality of
accepted goals. Do not simply replace Fable with Opus; do not minimise Fable at the expense of quality.

Invariant: the existing Fable Planner is the quality baseline; any routing must show non-inferior downstream
quality (spec-review approval, first-pass-green, fix-round rate, avg fix rounds, gate failures, review
request_changes, replanning frequency, accepted-goal success, human intervention, accepted-goal tokens/cost/latency).
Insufficient evidence → keep Fable or stay in shadow.

Phases (user's numbering): P0 audit + Fable-only baseline · P1 Planner usage telemetry (per invocation and per
accepted goal, tokens per material decision, no chain-of-thought) · P2 decision taxonomy (initial_goal_plan,
scout_results, held_task, architectural_replan, fix_strategy, closable_goal, retrospective, ambiguous_requirement,
other; purpose over complexity) · P3 eliminate Planner calls that need no Planner (record planner_skipped reason/
event/goal) · P4 compact delta-oriented decision packets, invalidated on state change, size measured · P5 explicit
Planner model tiers (default opus, escalation fable) in repo config conventions · P6 modes off|shadow|active,
shadow keeps Fable in production and records a non-mutating Opus decision · P7 evaluate by downstream outcomes,
Planner scorecard grouped by model × decision type × band × class × architectural × repo · P8 Opus default
candidate after shadow evidence, per responsibility · P9 Fable escalation: hard rules vs soft signals, transparent
and configurable · P10 compact Opus→Fable escalation packet · P11 router: hard constraints + decision
characteristics + historical evidence (+ optional Jev) answering "least expensive tier with sufficient evidence
of preserving quality" · P12 Jev routing signal, shadow first, typed batched questions, no file contents, fail open,
never bypasses hard escalation · P13 economics: Planner cost per accepted goal and total pipeline cost after
planning · P14 promotion criteria per decision class, insufficient samples → shadow · P15 automatic re-escalation
(repeated spec rejection, related fix rounds, invalidated assumptions) tracked and penalising Opus per class ·
P16 Planner scorecard with cold-start priors and CLI · P17 usage protection: Fable reserve, thresholds, hard-Fable
decisions HOLD with a visible reason when Fable is unavailable, never silently downgrade · P18 configuration with
safe defaults · P19 tests (existing behaviour, telemetry, elimination, context, shadow, active, scorecard, Jev,
quality: never promote Opus on token savings alone) · P20 Fable-only baseline saved before routing changes, then
compare.

Non-negotiable: Planner cannot edit source · human merges to main · tests-green mandatory · scope enforcement ·
deterministic security policy · routing cannot bypass task/merge safety · Fable stays as escalation/recovery ·
hard-Fable holds when Fable unavailable · shadow outputs never mutate production · no chain-of-thought storage.
Anti-goals: replace Fable everywhere; Fable reviewing every Opus decision; optimise purely for fewer Fable tokens;
route solely by complexity; trust self-reported confidence alone; Jev deciding; judging by prose similarity;
activating before measurement; moving architectural reasoning to weaker models because quota is low.
Success: 14 criteria (routable Planner role, Fable-only behaviour available, deterministic calls eliminated,
compact delta packets, Opus shadow without production effect, outcomes attributable, scorecard per class,
explicit observable escalation, compact escalation packet, optional Jev shadow signal, active possible but not
enabled without evidence, hard decisions never silently downgrade, Fable usage per accepted goal measurable,
pipeline quality is the optimisation constraint). Implementation order: P0, P1, P2, P3, P4, then the rest.
