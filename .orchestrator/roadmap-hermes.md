# Roadmap: Hermes-Inspired Memory, Cache, Worker Control & Security Hardening (user brief, verbatim, received 2026-09-23 12:00)

Goal: Hermes-Inspired Memory, Cache, Worker Control & Security Hardening

Mission

Study the current Hermes Agent implementation and selectively adopt architectural ideas that improve Orchestrator without weakening or duplicating its existing strengths.

Do NOT attempt to turn Orchestrator into a general-purpose Hermes clone.

Orchestrator remains a specialized adaptive software-engineering control plane.

Preserve its existing advantages:

* task DAG
* worktree isolation
* deterministic hooks
* tests-green
* serial integration
* human merge to main
* multi-model routing
* Planner routing
* Jev
* scorecards
* shadow evaluation
* promotion/demotion
* adaptive scheduling
* critical-path analysis
* interference detection
* context routing
* skill intelligence
* workflow strategy learning

The goal is to adopt only the Hermes patterns that fill remaining weaknesses.

Primary targets:

1. hot/warm/cold memory
2. searchable historical memory
3. prompt-cache economics
4. live worker steering
5. worker cancellation with partial-result preservation
6. structured worker outputs
7. hostile-context / prompt-injection scanning
8. stronger external-skill security
9. cache-aware context routing
10. orchestration-overhead measurement

Do not add another major routing abstraction unless repository evidence proves one is necessary.

⸻

P0 — Audit Current Orchestrator Against Current Hermes

Before coding, inspect the current Orchestrator implementation.

Also inspect the current Hermes Agent repository/documentation for the relevant features.

Create an adopt/adapt/skip matrix.

At minimum compare:

Memory

* always-loaded memory
* historical memory
* search
* compaction
* session/task history
* provenance
* memory size limits

Context

* prompt caching
* stable prefixes
* context compression
* cache invalidation
* tool-schema stability

Delegation

* child agents
* parallel workers
* worker inspection
* live steering
* cancellation
* partial results
* structured output validation

Security

* prompt-injection detection
* context-file scanning
* external skill safety
* credential filtering
* tool/action protection
* isolation

Do NOT assume Hermes’s implementation is automatically better.

Adopt only patterns that improve Orchestrator’s software-engineering workflow.

⸻

P1 — Memory Architecture Redesign

Current memory is useful but accumulating substantial text in:

* decisions.md
* gotchas.md
* architecture.md
* model-notes.md
* plan.md

Move toward three memory tiers.

HOT MEMORY

Small, bounded, high-value memory available cheaply.

Target contents:

* current architecture invariants
* current critical gotchas
* recent high-value decisions
* active repository constraints
* important model/tool behavior

Define a strict token/size budget.

Suggested starting target:

2,000–5,000 tokens total.

Do not blindly use this exact number if current evidence suggests another threshold.

HOT memory should be optimized for frequent retrieval.

⸻

WARM MEMORY

Structured indexed historical knowledge.

Examples:

* decisions
* gotchas
* architecture changes
* retrospectives
* model observations
* successful workflow strategies
* skill outcomes

Store metadata:

* ID
* type
* title
* date
* repository
* affected components
* tags
* provenance
* outcome
* revert path
* superseded status
* source task/goal

Warm memory should NOT be injected automatically.

Retrieve it when relevant.

⸻

COLD MEMORY

Complete historical archive.

Examples:

* old task records
* full retrospectives
* superseded decisions
* historical plans
* old run evidence
* archived gotchas

Cold memory should remain searchable but should almost never enter context directly.

Use it to reconstruct provenance or investigate historical questions.

⸻

P2 — Memory Migration

Do not destroy existing memory.

Build a migration path from current Markdown memory.

Parse existing:

decisions.md
gotchas.md
architecture.md
model-notes.md

into structured indexed records.

Preserve original Markdown as archive/provenance.

Never silently delete historical decisions.

Migration must be reversible.

⸻

P3 — Searchable Historical Memory

Implement cheap deterministic historical search.

Prefer:

* SQLite FTS5
* existing structured DB/index
* similarly lightweight local indexing

before using an LLM.

Support queries by:

* text
* component
* file
* task class
* model
* decision type
* date
* tag

Conceptual flow:

memory question
→ HOT memory
→ WARM FTS
→ structured filters
→ graph/semantic retrieval if needed
→ COLD archive

Stop when sufficient.

Do not call an LLM just to search memory.

⸻

P4 — Memory Compaction

Implement automatic bounded compaction.

When HOT memory exceeds budget:

1. identify low-current-value records
2. preserve them in WARM/COLD memory
3. retain pointers/provenance
4. update compact HOT representation

Never remove:

* active safety constraints
* unresolved gotchas
* current architecture invariants
* decisions still governing active behavior

Compaction should reduce always-loaded tokens without destroying knowledge.

⸻

P5 — Memory Relevance Scorecard

Measure whether retrieved memory helps.

Track:

* records retrieved
* records presented
* memory tokens
* downstream use
* first-pass success
* fix rounds
* repeated retrieval
* irrelevant retrieval

Eventually estimate:

memory utility / token

Do not optimize by simply retrieving less.

⸻

P6 — Prompt-Cache Telemetry

Raw context tokens are not enough.

Track cache economics where providers expose the information.

Per invocation record:

* uncached input tokens
* cached input tokens
* cache creation tokens
* cache read tokens
* cache hit ratio
* stable-prefix size
* dynamic suffix size
* cache invalidation reason where identifiable

Add:

effective_context_cost

Do not treat cached and uncached tokens as economically equivalent.

⸻

P7 — Stable Prompt Prefixes

Audit role prompts and context construction for unnecessary prefix mutation.

Identify content that can remain stable:

* base role
* fundamental safety rules
* stable tool catalog
* stable repository rules
* stable skill metadata

Move highly dynamic information toward the suffix where provider caching semantics benefit from this.

Do NOT sacrifice correctness merely to preserve cache hits.

⸻

P8 — Cache-Aware Context Router

Extend the current HIDE / SHORT / LONG / FULL context router.

Current decision approximately:

relevance
+
token cost
+
quality risk

Add:

cacheability
+
cache invalidation cost

Conceptually:

context_value =
expected_quality_value

effective_token_cost

handoff_cost

cache_invalidation_cost

Do not implement this literal formula without evidence.

The important principle:

A 5,000-token cached stable prefix may be cheaper than repeatedly generating different 2,000-token prefixes that destroy cache reuse.

⸻

P9 — Cache-Aware Tool Disclosure

The current tiered tool disclosure system should also consider prompt caching.

Dynamically changing tool definitions may invalidate provider prompt caches.

Measure whether:

aggressive tool hiding

actually saves more than:

stable cached tool catalog.

Potential architecture:

stable Level-0 catalog
+
dynamic selected Level-2 schemas

This may preserve a stable prefix while still avoiding large schemas.

Test rather than assume.

⸻

P10 — Cache-Aware Skill Disclosure

Apply the same principle to skills.

Keep stable:

* compact skill catalog / IDs

Dynamic:

* full instructions for selected skills

Measure whether this provides better effective context economics than completely rebuilding skill sections each call.

⸻

P11 — Worker Runtime Registry

Create a durable registry for running workers.

For every active worker track:

* task ID
* role
* model
* provider
* PID/process identity
* worktree
* branch
* start time
* last event
* current stage
* current tool if safely available
* token usage
* cost
* elapsed time
* parent
* children
* status

Possible statuses:

starting
running
waiting
steering
cancelling
cancelled
done
failed
held

This registry must survive enough process failure to support reconciliation.

⸻

P12 — Worker Inspection

Expose read-only worker inspection.

Planner/operator should be able to ask:

* what is running?
* how long?
* which task?
* which model?
* which worktree?
* what was the last observable event?
* is progress being made?
* what children exist?
* what resources are consumed?

Do not expose hidden chain-of-thought.

Use structured runtime events only.

⸻

P13 — Live Worker Steering

Add controlled steering of running workers where the underlying executor supports it.

Examples:

* provide newly discovered evidence
* correct a misunderstanding
* narrow scope
* clarify acceptance criteria
* tell worker to inspect a specific failure
* tell worker to stop pursuing an invalid approach

Steering must be recorded.

A steering message should include:

* task
* reason
* new evidence/instruction
* source
* timestamp

Do not silently rewrite the original task specification.

The original spec remains immutable history.

⸻

P14 — Steering Policy

Do not steer workers continuously.

Steering should occur only when expected value exceeds interruption cost.

Candidate triggers:

* newly merged dependency changes assumption
* Planner discovers critical evidence
* worker appears stuck
* worker is pursuing out-of-scope work
* test evidence invalidates current approach
* security concern emerges

Measure whether steering:

* prevents fix rounds
* reduces latency
* increases token usage
* causes confusion

⸻

P15 — Worker Cancellation

Allow safe cancellation of running workers.

Cancellation should:

1. signal worker
2. stop new tool activity
3. preserve worktree
4. preserve committed work
5. capture partial structured result
6. update bus/runtime registry
7. release capacity
8. record reason

Do not automatically delete partial work.

⸻

P16 — Partial Result Preservation

Cancelled or failed workers may still have useful evidence.

Capture where available:

* files inspected
* findings
* tests run
* hypotheses
* commits
* diff
* errors
* unresolved questions

Do NOT capture hidden reasoning.

Store structured observable evidence.

A future worker should be able to reuse this evidence without rereading everything.

⸻

P17 — Structured Worker Output Contracts

Strengthen worker result schemas.

Each role should have a machine-validatable output contract.

Examples:

Scout

* findings
* source locations
* confidence
* unresolved uncertainty

Executor

* changed files
* commits
* tests run
* test result
* unresolved issue

Reviewer

* verdict
* findings
* severity
* source locations
* required changes

Challenge

* claim examined
* evidence
* agreement/disagreement
* confidence

Validate results before accepting them into the pipeline.

⸻

P18 — Output Recovery

If a worker returns malformed structured output:

do not immediately rerun the entire worker.

Attempt:

1. deterministic parsing/repair where safe
2. bounded schema-repair request
3. only then rerun if necessary

Measure recovery tokens.

⸻

P19 — Hostile Context Scanner

Introduce deterministic scanning for untrusted instruction-bearing content.

Targets include:

* imported SKILL.md
* AGENTS.md
* CLAUDE.md
* README instructions
* external prompts
* external MCP descriptions
* downloaded agent configuration
* repository-local instruction files from untrusted repositories

Look for patterns such as:

* ignore previous instructions
* reveal credentials
* upload secrets
* modify permissions
* disable safeguards
* hidden shell execution
* unexpected network exfiltration
* instructions claiming higher authority

Scanning should not automatically treat every suspicious phrase as malicious.

Produce:

safe
suspicious
blocked

with evidence.

⸻

P20 — Context Trust Classification

Every instruction-bearing evidence object should carry trust metadata.

Example:

SYSTEM
TRUSTED_REPO
LOCAL_USER
EXTERNAL
UNTRUSTED

Hard authority order remains deterministic.

External content can provide information.

It cannot override higher-authority policy.

Jev must not decide authority.

⸻

P21 — External Skill Hardening

Extend the current skill quarantine system.

Before external skill testing:

* scan prompt/instruction content
* inspect scripts
* inspect network endpoints
* inspect requested tools
* inspect credential references
* inspect package dependencies
* inspect filesystem access

Produce a structured risk report.

High-risk external skills remain quarantined until explicitly reviewed.

⸻

P22 — Credential Filtering

Ensure worker/tool context does not unnecessarily expose credentials.

Audit:

* MCP environments
* subprocess environments
* logs
* Jev payloads
* external skills
* browser/network tools

Use allowlisted environment propagation where practical.

Never log secret values.

⸻

P23 — Orchestration Overhead Metric

This is critical.

Measure the cost of the harness itself.

Define:

orchestration_tokens =
Planner

* scouts
* reviews
* Jev
* routing/classification
* skill selection
* context selection
* memory retrieval reasoning
* scheduling reasoning

execution_tokens =
tokens directly used to implement the task

Then:

orchestration_amplification =
orchestration_tokens / execution_tokens

Also track:

orchestration_cost / accepted_goal_cost

and:

orchestration_latency / accepted_goal_latency

The harness should not become more expensive than the work it coordinates without measurable quality benefit.

⸻

P24 — Small-Task Fast Path

Add a deliberate collapse path.

For obvious localized low-risk work:

task
→ minimal context
→ executor
→ deterministic tests
→ merge path

Avoid unnecessary:

* scouts
* Jev calls
* Planner relaunches
* skill routing
* workflow strategy lookup
* context-model judgement
* extra review

Hard safety requirements still apply.

The increasingly sophisticated harness must remain cheap for simple work.

⸻

P25 — Fast-Path Eligibility

Use deterministic conditions first.

Candidate requirements:

* low complexity
* localized scope
* no security paths
* no architecture change
* no migrations
* no unresolved dependencies
* historically high first-pass success
* straightforward acceptance criteria

Jev may assist only for ambiguous cases.

Measure fast-path outcomes carefully.

⸻

P26 — Adaptive Harness Depth

Long-term, allow the harness to choose how much orchestration a task deserves.

Conceptually:

LEVEL 0
deterministic direct execution

LEVEL 1
model routing + minimal context

LEVEL 2
skills + context intelligence

LEVEL 3
scouts + adaptive scheduling

LEVEL 4
full architecture/review pipeline

The goal is:

orchestration proportional to task difficulty and risk.

Do not implement a giant new abstraction if existing complexity/routing fields can express this cleanly.

⸻

P27 — Evidence Reuse from Cancelled Workers

Integrate partial-result preservation with the existing evidence system.

Example:

Worker A cancelled after discovering:

* relevant files
* failing test
* dependency relationship

Worker B should receive those facts rather than rediscover them.

Mark provenance:

worker_partial

and preserve freshness/hash information.

⸻

P28 — Steering + Stale-Work Integration

Use existing stale-work detection.

If another merge invalidates a running worker’s assumptions:

instead of always allowing it to finish and then rebasing:

stale detector
→ evaluate severity

Low:
continue

Medium:
steer with changed evidence

High:
cancel/restart or hold

Measure which action produces the best accepted-task economics.

⸻

P29 — Steering + Critical Path

Prioritize steering decisions for critical-path workers.

A non-critical worker can sometimes finish naturally.

A critical-path worker pursuing an invalid assumption may justify immediate intervention.

Do not automatically interrupt every worker on every upstream change.

⸻

P30 — Memory + Skills Integration

Skills should not duplicate memory.

Memory answers:

“What has happened / what do we know?”

Skills answer:

“How should this type of work be performed?”

When repeated procedural knowledge appears in memory:

candidate skill
→ quarantine/testing
→ skill lifecycle

Once promoted, memory may reference the skill rather than repeating its full procedure.

⸻

P31 — Memory + Strategy Integration

Workflow strategy outcomes should live in structured WARM memory / scorecards rather than growing Markdown indefinitely.

Planner retrieval should be able to ask:

“What strategies worked for this kind of task?”

without reading historical retrospectives.

⸻

P32 — Cache + Handoff Economics

Extend existing handoff scorecards.

For:

Opus → Fable
executor → stronger executor
worker → replacement worker

measure:

* cached context retained
* cache lost
* uncached reconstruction
* handoff packet tokens
* duplicated evidence
* latency

Routing should eventually consider effective handoff cost rather than raw model price.

⸻

P33 — Promotion Framework

Integrate new adaptive features into existing:

off
shadow
active

Candidates:

* cache-aware context routing
* worker steering policy
* stale-work steering
* small-task fast path
* adaptive harness depth

Promotion requires:

* sufficient sample size
* non-inferior quality
* improved accepted-goal economics

Do not activate merely because tests pass.

⸻

P34 — Evaluation

Create representative evals.

Memory

* current decision found in HOT
* older decision found in WARM
* historical provenance found in COLD
* irrelevant history excluded
* compaction preserves knowledge

Cache

* stable prefix remains stable
* dynamic suffix changes safely
* cache telemetry correct
* tool disclosure does not unnecessarily destroy cache

Worker control

* inspect running worker
* steer worker
* steer evidence logged
* cancel worker
* capacity released
* partial result retained
* replacement worker reuses evidence

Security

* malicious external skill
* malicious AGENTS.md
* fake authority escalation
* secret exfiltration instruction
* harmless security documentation not falsely blocked

Fast path

* trivial task collapses pipeline
* security task cannot use unsafe fast path
* architecture task receives full orchestration
* fast-path regression causes demotion

⸻

P35 — Success Metrics

Primary:

* accepted-goal success
* first-pass success
* fix-round rate
* tokens / accepted goal
* effective uncached tokens / accepted goal
* cost / accepted goal
* latency / accepted goal

Memory:

* HOT memory tokens
* retrieval precision
* retrieval usefulness
* compaction frequency

Cache:

* cache hit ratio
* cache-read tokens
* uncached tokens
* cache invalidations
* effective context cost

Workers:

* steering rate
* steering success
* cancellations
* partial-result reuse
* fix rounds avoided

Security:

* suspicious context detected
* false-positive rate
* blocked malicious imports

Harness:

* orchestration amplification
* orchestration cost share
* orchestration latency share

⸻

P36 — Anti-Goals

Do NOT:

* turn Orchestrator into a general personal assistant
* copy Hermes wholesale
* add messaging integrations
* add general cron/productivity features
* replace current scheduler
* replace current scorecards

do this next
