# Orchestrator Adaptive Parallelism & Subagent Efficiency (user brief, 2026-09-21)

## Goal
Improve Orchestrator's parallel execution, scheduling, and subagent usage so independent work completes concurrently while minimizing: merge conflicts, stale work, duplicated agent reasoning, unnecessary scouts, token waste, quota contention, critical-path latency.

The objective is NOT to maximize the number of simultaneous agents. The objective is: use the smallest useful set of agents, running the right independent work at the right time, to minimize time and resources per accepted goal without reducing quality.

Implement after or alongside the measurement foundations from the Orchestrator Efficiency & Jev roadmap (Phase I, .orchestrator/phase-i-program.md history). Do not optimize concurrency without telemetry.

## Core principles
1. Parallelism is not automatically efficiency. More agents can increase duplicated context, merge conflicts, stale assumptions, quota consumption, review burden, fix rounds. Only parallelize when expected benefit exceeds coordination cost.
2. Preserve atomic tasks: explicit acceptance criteria, scope, dependencies, small atomic implementation tasks. No oversized tasks merely to reduce scheduling complexity.
3. Integration remains controlled: execution may be parallel; integration stays serialized through the existing merge machinery; human approval remains required for main.
4. Prefer orthogonal agents: parallel agents answer different questions or modify independent surfaces. No substantially identical work unless a deliberate, measured speculative-execution experiment.

## P0 Parallelism telemetry
Before changing scheduling behavior, measure current behavior. Track per goal: maximum and average concurrent executors; maximum and average concurrent Claude workers; executor idle time; worker idle time; queue wait time; task execution time; review time; merge wait time; goal wall-clock duration; critical-path duration where derivable; task dependency wait time; quota/capacity blocking; merge conflicts; rebase failures; stale-work failures; fix rounds caused by concurrent changes; duplicated scout/review work where measurable.
Record why a ready task was not dispatched: dependency, executor capacity, budget, cooldown, account capacity, predicted interference, merge pressure, other.
Establish a baseline before enabling adaptive scheduling.

## P1 Explicit task interference model
Determine whether two dependency-ready tasks are actually safe and useful to execute concurrently. The dependency DAG is necessary but insufficient: tasks without an explicit dependency can touch coupled surfaces.
Deterministic signals first (cheap): exact file overlap, scope overlap, same directory/module, shared public API, shared schema, shared configuration, shared migration, shared tests, shared generated artifacts, explicit dependency. Produce an interference classification or score: interference(T1, T2). Do NOT begin with an LLM call per task pair.
Hard conflicts prohibit concurrent execution (define conservatively): same source file, same migration surface, explicit dependency, one task changing an interface the other consumes.
Soft conflicts reduce scheduling preference without blocking: same module, adjacent tests, shared dependency, related subsystem.

## P2 Graph-aware interference
After deterministic interference works, use graphify/code-graph information to detect semantic coupling that paths miss (T1 changes an interface, T2 changes a consumer). Graph evidence is an additional signal. Graph availability is not mandatory; fall back to deterministic interference when the graph is absent or stale.

## P3 Execution waves
Group ready tasks into safe, useful concurrent waves. Instead of "dispatch every ready task until parallel_limit": "select the best set of mutually compatible ready tasks". Goal DAG -> ready tasks -> interference analysis -> capacity analysis -> execution wave -> parallel dispatch. Example: ready T1 auth implementation, T2 navbar, T3 auth tests, T4 database cleanup; T1 and T3 strongly coupled -> wave 1 = T1, T2, T4; wave 2 = T3. Preserve dependency correctness; avoid unnecessary serialization.
Waves respect: depends_on, executor role compatibility, complexity range, executor capacity, daily budgets, quota cooldown, account restrictions, interference constraints, existing safety policy.

## P4 Critical-path scheduling
Prioritize tasks that determine goal completion time. Estimate the goal DAG's critical path. For each ready task consider downstream dependency depth, number of blocked descendants, estimated task duration, expected review duration, expected fix-round probability, whether the task unlocks other work. A task that unlocks several long-running downstream tasks generally receives capacity before an independent low-impact task. Conceptually priority = critical_path_importance + unblock_value + downstream_cost, adjusted by expected resource cost and success probability. Do not implement this exact equation blindly; design and test an appropriate ranking function.

## P5 Duration-aware scheduling
Estimate task duration from historical scorecard/telemetry where evidence suffices (complexity, task type, executor, scope size, language, module, comparable tasks). Use estimates in wave scheduling and critical-path calculations. Cold start stays simple and deterministic; no false precision on sparse evidence.

## P6 Capacity-aware scheduling
Understand resources globally: executor parallel limits, Claude account headroom, daily budgets, five-hour caps, quota groups, cooldowns, expected task duration, expected downstream demand. Avoid consuming premium capacity on low-priority tasks when critical-path work is likely to need it; do not reserve capacity when no such demand exists. Measure utilization before complex reservation logic.

## P7 Orthogonal scout strategy
Keep scouts limited; do NOT increase scout count because parallel execution is available. When multiple scouts are used, assign different objectives (implementation-map, dependency-map, architecture, test-surface, failure-history, security-context, migration-impact, API-consumers). Before launching another scout, check whether memory, bus results, previous scout findings or graph data already answer the question. Do not pay a model to rediscover durable evidence.

## P8 Scout result structure
Scouts return compact structured evidence suitable for reuse: finding, source/location, confidence, relevance, unresolved uncertainty. Planner summarizes each finding once; downstream workers consume the compact result, never entire transcripts. Low-confidence findings that materially influence planning use the existing challenge mechanism.

## P9 Parallel specialized reviews
For tasks requiring multiple reviews, run independent reviews concurrently when policy permits; never wait for Reviewer A before starting B unless B depends on A. Avoid identical roles: Reviewer A correctness (acceptance, functional correctness, regression risk, test sufficiency, implementation mistakes); Reviewer B adversarial/risk (security, edge cases, unsafe assumptions, unexpected interactions, failure modes). Security requirements remain deterministic. Parallel review reduces latency without weakening coverage.

## P10 Review deduplication
Measure overlap between multi-review findings. If two reviewers repeatedly produce equivalent findings, investigate prompt specialization, duplicated context, narrowing one review, marginal information. The goal is to maximize useful independent evidence per review token, not to eliminate second reviews. Track unique findings where practical.

## P11 Merge-pressure awareness
Measure completed tasks waiting for merge, average merge wait, rebase frequency, rebase failures, tasks invalidated by earlier merges. If the merge queue saturates, the scheduler may reduce dispatch of highly coupled work while continuing independent work. Do not throttle merely because the queue is non-empty; react only on evidence of integration contention.

## P12 Stale-work detection
Before or during integration, detect when a task's assumptions may be stale because another task merged first: base branch advanced across relevant files, dependency graph changed, public interface changed, relevant tests changed, task scope overlaps newly merged changes. Prefer cheap deterministic checks. When stale risk is high, rebase/revalidate before expensive downstream review where possible. Never silently discard work. Record stale-work events for scheduler learning.

## P13 Adaptive parallelism
Once telemetry exists, stop treating concurrency as only a fixed maximum. Determine useful concurrency from the number of independent ready tasks, interference, critical path, executor capacity, quota headroom, merge pressure, historical failure rate, expected duration (one coupled group -> 1-2; five independent modules -> higher; shared interfaces -> lower). Never exceed configured hard capacity: adaptive parallelism selects below the maximum, it does not bypass limits.

## P14 Scheduling scorecard
Track outcomes of scheduling decisions. For pairs/waves record predicted interference, actual merge conflict, stale-work event, fix round, execution duration, queue duration, goal impact. Determine over time whether interference heuristics are predictive. Transparent heuristics and measured outcomes first; no ML.

## P15 Jev as a scheduling signal
Only after deterministic scheduling and telemetry exist. Jev may cheaply answer ambiguous scheduling questions (semantic interference, stale-assumption risk, whether more scouting reduces risk). Jev is an additional signal and must NOT override explicit dependencies, hard file conflicts, capacity or safety constraints. No O(n^2) Jev calls; consult only where the answer can change scheduling. Fail open to deterministic scheduling. Shadow evaluation before active use.

## P16 Critical-path model allocation
Combine scheduling with executor routing: a critical-path task may justify a model with better first-pass success at higher initial cost; an independent non-blocking task may favor a cheaper executor. Selection eventually considers expected accepted-task cost + expected completion latency + critical-path impact. Do not globally route critical-path tasks to the strongest model; use scorecard evidence.

## P17 Selective speculative execution (experimental, LOW priority)
Do NOT enable generally. For rare critical-path tasks with high historical fix-round probability, high retry cost and spare capacity, investigate two executors on one specification -> deterministic tests -> choose the viable result -> normal review/merge. Roughly doubles execution cost; must show net benefit; start in simulation/shadow. Never speculate because capacity is idle.

## P18 Workflow strategy learning (long term)
Learn which workflow performs well per task category (UI fix: execute -> tests; migration: dependency scout -> execute -> migration tests -> review; architecture: architecture + impact scouts -> decomposition -> waves -> specialized reviews; security-sensitive: security context -> stronger execution -> deterministic security review). Store outcomes by workflow strategy; let evidence influence the Planner's workflow choice; not fully autonomous until evidence suffices.

## Scheduler architecture target
Goal -> Planner -> atomic task DAG -> ready-task set -> dependency filter -> interference analysis -> critical-path analysis -> capacity/quota analysis -> execution-wave selection -> executor routing -> parallel execution -> tests/hooks -> parallel specialized review where required -> serial merge -> outcome telemetry -> scorecard/scheduler evidence. Supporting signals: memory (historical decisions), graphify (semantic dependencies), scorecard (executor outcomes), Jev (optional cheap classification), telemetry (cost, tokens, latency, contention).

## Hard safety invariants
Adaptive scheduling MUST NOT bypass: task dependencies, scope restrictions, Planner source-edit prohibition, tests-green, mandatory reviews, security review requirements, executor/account limits, merge lock, serial goal-branch integration, human approval for main. Parallelism is an optimization layer, not a safety layer.

## Anti-patterns
Agent explosion; duplicate scouts; duplicate reviews; parallel planners (use targeted scouts/challenges instead); blind max-concurrency dispatch; LLM scheduling everywhere; Jev O(n^2) scheduling; premature speculative execution.

## Implementation priority
0 parallelism and contention telemetry; 1 deterministic interference model; 2 execution waves; 3 critical-path scheduling; 4 graph-aware interference and stale-work detection; 5 capacity/duration-aware scheduling; 6 orthogonal scout objectives and scout-result reuse; 7 parallel specialized reviews and review-overlap measurement; 8 merge-pressure awareness; 9 adaptive concurrency below hard limits; 10 scheduling scorecard / heuristic validation; 11 Jev scheduling signals in shadow; 12 critical-path-aware executor allocation; 13 workflow-strategy learning; 14 selective speculative execution only if evidence justifies it. Do not implement every priority simultaneously. Measure each major behavioral change.

## Success metrics
Primary: wall-clock time per accepted goal, tokens per accepted goal, cost per accepted goal, first-pass success rate, fix-round rate. Parallelism-specific: critical-path duration, average executor utilization, ready-task queue time, dependency wait time, merge wait time, merge conflict rate, stale-work rate, rebase failure rate, useful concurrency, scout duplication, review finding overlap. Quality constraints: tests-green outcomes, review defect detection, accepted-task quality, security requirements, human control. A scheduler is NOT better merely because concurrency increased, more agents ran, or wall-clock decreased; if concurrency lowers first-pass success or rework raises total cost materially, it is not an improvement.

## Evaluation
Compare new scheduling against the existing scheduler. Retain enough information to estimate what baseline scheduling would have done, what adaptive scheduling did, whether completion, resource use and conflicts/rework changed. Probabilistic/Jev decisions run in shadow before active control.

## Planner instructions
Multi-phase scheduling program. First inspect: daemon dispatch behavior, dependency handling, executor capacity, worktree creation, merge queue, graphify data, scorecard, worker spawning, review scheduling, existing telemetry. Recall memory before scouting. Verify behavior in source; this document may not match the implementation. Scouts only where uncertainty warrants. Planner must not edit source. Atomic specifications with explicit acceptance, scope, dependencies; never one task for the whole roadmap. Preserve backward-compatible scheduling where appropriate. Every adaptive feature has: deterministic fallback, observable decision reason, telemetry, safe configuration, tests, revert path. Simple deterministic improvements before probabilistic ones. At the end of each phase record whether the change improved (1) accepted-goal latency, (2) accepted-goal token/cost efficiency, (3) first-pass success, (4) contention/rework.

The central scheduling question: what is the smallest set of independent work that should execute concurrently right now to advance the goal's critical path as quickly and cheaply as possible without increasing rework or reducing quality?
