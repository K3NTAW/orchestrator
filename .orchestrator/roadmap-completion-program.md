# Roadmap completion program (user brief, 2026-09-21 08:20)

Goal: bring .orchestrator/phase-i-program.md (P0-P7) and .orchestrator/adaptive-parallelism-program.md (P0-P18) to implementation completeness. Do NOT reimplement completed work. Audit first; classify every requirement as implemented and tested / implemented but insufficiently tested / partially implemented / missing / intentionally experimental (evidence before activation). Then implement remaining work in dependency order. Objective is completeness, not enabling experimental features: probabilistic behaviour stays shadow/off until evidence supports activation.

Core architecture after this goal: token/cost attribution, accepted-task economics, context efficiency, fix-round economics, Jev executor routing, review efficiency, parallelism telemetry, deterministic interference, graph-aware interference, execution waves, critical-path scheduling, duration-aware scheduling, capacity-aware scheduling, efficient scout/subagent usage, parallel specialized reviews, review dedup measurement, merge-pressure awareness, stale-work detection, adaptive concurrency, scheduling outcome measurement, optional Jev scheduling signals, critical-path-aware executor allocation, workflow-strategy learning, selective speculative execution; all deterministic safety invariants preserved.

## P0 Audit before coding
Read both briefs. Inspect at minimum daemon, pool, executor, spawn, scorecard, attribution, baseline, interference, critical_path, jev, jev_gate, jev_route, jev_rank, schedlog, bus, merge, memory/graphify integration, review/scout prompts, pool.toml, tests. Source is truth, not the roadmap's description of old behaviour. Build an implementation matrix over Phase I P0-P7 and AP P0-P18. No tasks for requirements already correctly implemented and tested; preserve behaviour that already satisfies a requirement.

## P1 Finish Phase I
Measurement: tokens/USD per accepted task and goal, tokens and time to first green, time to accepted, first-pass success, fix-round rate, average fix rounds, fix-round token attribution, calls/turns per accepted task, model distribution, pipeline amplification, role/bucket attribution, grouping by task/goal/executor/model/complexity/task class.
Context efficiency: role packets for executor, fix round, scout, review, spec review, challenge, Planner decision where applicable; no repeated transmission of irrelevant goal history, unrelated memory, unrelated completed tasks, unnecessary transcripts; minimum sufficient evidence; measure packet/input size; packet reuse never serves stale task/repo state.
Fix-round economics: scorecard evaluates initial execution cost, accepted-task cost, first-pass success, fix probability, average fix rounds, gate failure, review request_changes, total accepted-task tokens; resume/delta preserved.
Jev executor routing: off/shadow/active; one eligible executor skips Jev; batching; caching; invalidation; bounded cache; timeout; malformed response; unavailable; exhausted budget; privacy/redaction; eligible-set enforcement; scorecard integration; cold start; strong empirical evidence not overridden; fail-open reproduces baseline. Active stays configurable, NOT enabled by this goal.
Routing evaluation telemetry: baseline vs Jev-assisted selection against first-pass success, accepted cost, fix rounds, gate, review, latency, Jev cost.
Review efficiency: complementary roles and overlap telemetry; security review deterministic.

## P2 Duration-aware scheduling (AP P5)
Empirical duration estimates when evidence suffices (complexity band, task class, executor, scope size, module/repo, comparable tasks); robust statistics (median/trimmed); deterministic cold-start priors; record predicted, actual, error, evidence source and sample size; critical path uses empirical duration when supported; no false precision.

## P3 Capacity-aware scheduling (AP P6)
Reason about executor parallel capacity, Claude worker capacity, account headroom, daily budgets, rolling/five-hour caps, quota groups, cooldowns, expected duration, expected critical-path demand. Do not spend scarce premium capacity on low-impact work when it is expected to block critical-path work; do not idle capacity on speculative demand without evidence. Transparent deterministic heuristics; record scheduling reasons.

## P4 Orthogonal scout/subagent efficiency (AP P7, P8)
Scouts stay limited; no agent explosion. Explicit objectives: implementation-map, dependency-map, architecture, test-surface, failure-history, security-context, migration-impact, API-consumers. Before a scout, check memory, bus results, previous scout findings, code graph; skip when fresh evidence answers. Multiple scouts orthogonal. Compact structured findings: finding, source/location, confidence, relevance, unresolved uncertainty; no transcripts downstream; challenge preserved. Telemetry: scouts considered, skipped for reusable evidence, objective, duplicate/overlap, tokens, downstream use.

## P5 Parallel specialized reviews (AP P9, P10)
Required reviews run concurrently unless dependent. Reviewer A: acceptance, correctness, regression, tests. Reviewer B: adversarial, security, edge cases, hidden assumptions, interactions. Measure distinct findings, overlap, second-review-added findings, severity, tokens per useful finding, request_changes, accepted outcome. Never remove required reviews for overlap; improve prompts/context first.

## P6 Merge-pressure awareness (AP P11)
Measure tasks waiting for merge, queue depth, merge wait, rebase frequency and conflicts, stale-work events, tasks invalidated by earlier merges, coupling of queued candidates. Scheduler may reduce dispatch of highly coupled work only when serial integration is a demonstrated bottleneck; independent work continues; no throttling for one waiting task; reason exposed.

## P7 Complete stale-work protection (AP P12)
Signals: relevant scoped files changed, public interface changed, relevant tests changed, graph relationships changed, dependency changed, overlapping earlier merge. Deterministic first; when configured, rebase/revalidate before expensive review. Record base, goal head, changed relevant paths, risk, action, conflict outcome. Never silently discard work.

## P8 Adaptive concurrency (AP P13)
Concurrency 0..hard_max from independent ready tasks, hard/soft interference, critical-path position, executor capacity, quota headroom, expected duration, historical rework, merge pressure. Coupled group low; independent modules higher; shared interfaces/migrations conservative. Never bypass hard capacity. Log hard max, selected concurrency, reasons, tasks considered, expected benefit, observed outcome.

## P9 Scheduling scorecard (AP P14)
Per wave/task pair: predicted interference, actual merge conflict, stale-work event, fix round, predicted and actual duration, queue duration, critical-path impact, accepted outcome, total cost. Measure hard-conflict precision, soft-conflict usefulness, unnecessary serialization, conflict rate, stale-work rate, goal latency, cost. No ML.

## P10 Jev scheduling shadow layer (AP P15)
Only ambiguous decisions where deterministic evidence is insufficient and the answer can change scheduling: semantic interference, stale-assumption risk, whether more scouting reduces risk. Never for explicit dependencies, exact file conflicts, obvious independence, single valid schedule. No O(n^2); batch; cache while state unchanged. off/shadow/active, default shadow. Cannot override dependencies, hard conflicts, capacity, safety. Fail open. Measure whether Jev disagreement predicts conflicts/rework better than deterministic scheduling.

## P11 Critical-path-aware executor allocation (AP P16)
Combine executor economics with scheduling importance: expected accepted-task cost, first-pass success, fix-round cost, duration, critical-path importance, downstream unblock value, capacity. NOT critical path = strongest model; estimate positive expected value per task; cheaper proven-safe executors for non-critical work; hard eligibility wins; Jev optional signal only.

## P12 Workflow strategy learning (AP P18)
Explicit strategy ids (direct_execute, scout_execute, dependency_scout_execute, architecture_impact_scouts_then_decompose, execute_specialist_review, security_context_strong_execute_security_review, parallel_wave, others derived from real workflows). Record per strategy: repo, task class, complexity, characteristics, steps, models, tokens, cost, time, first-pass, fix rounds, gate, review, accepted outcome. Strategy scorecard. Observe only first; then off/shadow/active; shadow answers "what strategy would evidence have preferred" without changing behaviour; active requires evidence; cold start = existing Planner policy; learned strategy never removes deterministic safety.

## P13 Selective speculative execution (AP P17)
Experimental, OFF by default. Eligibility: critical-path, high historical fix-round probability, high retry cost, spare capacity, budget, safe to duplicate. Shadow estimator first ("would speculation have paid?"). Explicit experimental mode: same immutable spec, executor A and B, independent worktrees, deterministic tests, deterministic result selection, normal review, normal serial merge; never merge both; never bypass review or human approval. Measure duplicated cost, avoided retries, latency saved, accepted cost, quality.

## P14 Unified decision observability
Every adaptive decision explainable: candidates, hard constraints, deterministic evidence, historical evidence, Jev evidence, selected action, rejected alternatives, reason, confidence/sample size, eventual outcome. No chain-of-thought; structured evidence.

## P15 Shadow-first promotion framework
Standard off/shadow/active for Jev routing, adaptive scheduling, Jev scheduling, workflow-strategy influence, speculation. Explicit evaluation criteria: accepted cost and tokens, first-pass success, fix rounds, gate success, review findings, security, goal latency. Never promote for "implemented", fewer tokens, Jev disagreement, or tiny-sample latency. Insufficient evidence = stay shadow.

## P16 Final roadmap audit
Second audit; machine-readable .orchestrator/roadmap-status.json mapping every requirement to status (implemented_active | implemented_shadow | implemented_off | partial | missing | not_applicable), implementation files, tests, configuration, telemetry, activation state. Goal incomplete while any non-experimental requirement is partial or missing; experimental ones may be complete while shadow/off.

## Required testing (minimum)
Duration: cold start, sufficient history, sparse history, outlier resistance, prediction telemetry. Capacity: executor full, quota cooling, account budget, critical-path contention, idle capacity, no unnecessary reservation. Scouts: duplicate evidence prevents scout, orthogonal allowed, structured results, low-confidence challenge, stale evidence does not suppress. Reviews: parallel spawn, specialized roles, overlap telemetry, mandatory security. Merge pressure: independent continues, coupled deferred, no trivial throttling, reason telemetry. Adaptive concurrency: coupled, independent, hard max, no ready work, quota constrained, merge-pressure constrained. Scheduling scorecard: predicted vs actual, stale outcomes, duration prediction, malformed telemetry tolerance. Jev scheduling: off, shadow, active, fail open, no O(n^2), hard conflict wins, dependency wins, cache invalidation, privacy. Critical-path allocation: cheap preferred when equivalent, stronger only when justified, eligibility wins, sparse evidence safe. Workflow learning: attribution, aggregation, cold start, shadow recommendation, insufficient evidence, safety invariants, repo/task-class separation. Speculation: default off, ordinary ineligible, critical-path eligible, insufficient budget, no spare capacity, exactly one result, tests/review/merge still required.

## Performance
Scheduler cost about O(tasks + relevant_edges); deterministic pairwise acceptable for small ready sets, cache for large; Jev never across all pairs; orchestration overhead must not dominate pipeline cost.

## Safety invariants (mandatory)
Planner no source edits; acceptance required; scope enforced; dependencies enforced; worktree isolation; tests-green merge bar; deterministic security review; required reviews never probabilistically skipped; executor/account limits hard; serial merge; goal work on goal branch; human approval for main; Jev fails open; learned strategy never overrides safety.

## Anti-goals
Not max agents, max concurrency, min raw tokens, cheapest initial executor, fewest reviews/scouts, most Jev decisions. Optimize lowest expected resource use and wall-clock per accepted goal at maintained or improved quality.

## Implementation strategy
Large goal; no giant task. Dependency-aware atomic tasks; parallelize independent modules; serialize heavily shared files (daemon.py) where interference warrants; use waves; narrow scope; use existing baselines (the brief text ended here: "Use existing baseline").
