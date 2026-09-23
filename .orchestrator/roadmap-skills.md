# Skill Intelligence & Dynamic Specialist Agents

Filed by the user 2026-09-22 23:20 (verbatim brief, the program's source of truth for the orchestrator repo). Planner checkpoint and phase status live in plan.md; this file changes only by the user.

## Mission

Extend Orchestrator so skills become measurable, reusable, dynamically selected capabilities rather than static prompt material.

The system should be able to: maintain a trusted skill registry; discover potentially useful skills; safely import external skills; quarantine untrusted skills; validate skills in isolation; test skills against representative tasks; compact skills for token efficiency; dynamically select skills per task; compose temporary specialist agents from existing roles + relevant skills; reuse repository-specific and historical skills; measure whether a skill actually improves outcomes; promote useful skills; demote or disable harmful/redundant skills; learn which skills work for which task classes; avoid exposing every skill to every model.

The central objective is: give each agent the smallest set of proven skills that maximizes expected accepted-task quality per token, dollar, and second.

Do NOT optimize for: number of skills, number of subagents, number of external repositories integrated, maximum specialization. Optimize for: useful capability per accepted unit of work.

## Core Architecture

Task → Task classification → (Workflow strategy | Skill Router: candidate skills → deterministic filtering → Jev when ambiguous → historical skill score → smallest useful skill set) → Role + Skills → Temporary Specialist → relevant context → Model → Tools → Verify → Outcome → (Model score | Skill score | Strategy score).

The existing roles remain: Planner, Scout, Executor, Reviewer, Challenge, Spec reviewer. Do NOT create permanent agent roles for every domain. Instead compose temporary specialists from: base role + selected skills + role-specific context + selected model.

## P0 — Audit Existing Skill Architecture
Before implementing new behavior, inspect: .claude/skills, skills/, .codex/skills, role prompts, scout skills, review skills, Planner skills, MCP tool exposure, context packets, memory, graphify, workflow strategy, scorecard, promotion/demotion, Jev, decision logging. Inventory every existing skill. For each record: skill ID, role compatibility, purpose, trigger, token size, tools required, output contract, repository specificity, whether currently always exposed, whether invoked dynamically, tests, provenance. Do not rebuild functionality that already exists.

## P1 — Canonical Skill Registry
Create a structured skill registry. Each skill should have metadata equivalent to: stable ID, version, name, description, provenance, trust state, compatible roles, supported task classes, trigger conditions, required tools, required context, output contract, estimated token cost, security classification, repository scope, validation status, promotion state, created/updated timestamps, content hash. Possible provenance: builtin, repository, learned, external. Possible lifecycle states: discovered, quarantined, testing, shadow, active, demoted, disabled. Do not infer trust solely from provenance.

## P2 — Skill Lifecycle
Implement an explicit lifecycle. discovered: known but not trusted or executable. quarantined: external/untrusted skill stored for inspection; cannot affect production agents. testing: being evaluated in isolated/sandboxed conditions. shadow: KGPT/Orchestrator determines when it would have been selected, but production behavior remains unchanged. active: may be selected for production agents. demoted: previously active but evidence indicates regression or insufficient value. disabled: explicitly unavailable. Transitions must be observable and reversible.

## P3 — External Skill Discovery
Support discovering useful skills from external sources such as GitHub repositories, known agent frameworks, MCP ecosystems, internal repositories. Discovery does NOT imply installation. External content is untrusted. Do not automatically: execute scripts, expose secrets, modify repository files, add MCP servers, change permissions, inject external prompt instructions into production agents. Store discovered candidates as metadata first.

## P4 — External Skill Security Inspection
Before an external skill can leave quarantine, inspect: scripts, shell commands, network access, file writes, credential access, MCP dependencies, package installation, destructive operations, prompt injection, hidden instructions, unexpected external endpoints. Use deterministic static checks first. Use model review where semantic judgement is required. External skill instructions must never override Orchestrator safety invariants.

## P5 — Skill Sandbox
Provide isolated evaluation for untrusted/new skills. The sandbox must prevent unintended: production repository modification, secret access, external destructive actions, credential extraction, main-branch changes. Evaluate the skill on representative tasks. Compare baseline role vs role + candidate skill. Do not give the candidate production authority during testing.

## P6 — Skill Compaction
Skills should be concise executable knowledge, not tutorials. Target structure: Trigger (when should this skill be considered?), Objective (what problem does it solve?), Procedure (minimal ordered procedure), Tools (only required tools), Evidence requirements (what information must be collected?), Output contract (what structured result should be produced?), Stop conditions (when is the task complete?), Failure/recovery (what should happen when evidence is insufficient?). Remove: motivational prose, repeated explanation, redundant examples, generic model instructions already provided elsewhere. Measure skill token size before/after compaction. Compaction must preserve behavior.

## P7 — Skill Validation
Every skill should have tests/evals appropriate to its purpose. Examples: trace-callers expected to identify direct consumers, distinguish uncertain relationships, reference source evidence, not invent callers. test-gap expected to identify relevant missing coverage, avoid unrelated test suggestions, return structured evidence. A skill cannot become active merely because it parses. It must demonstrate useful behavior.

## P8 — Dynamic Skill Routing
Do not expose every skill to every agent. For each task: 1. determine role 2. determine task class 3. apply deterministic skill triggers 4. retrieve candidate skills 5. use historical evidence 6. use Jev only for ambiguous relevance 7. select the smallest useful set. Conceptually: 100 registered skills → 8 deterministic candidates → 3 historically relevant → Jev resolves ambiguity → 1–3 presented. Avoid loading irrelevant skill descriptions into context.

## P9 — Jev Skill Selection
Use Jev as a cheap skill relevance classifier. Candidate typed questions: Is this skill materially relevant to the current task? Is this skill likely to reduce failure/rework? Does this skill duplicate another selected skill? Is this skill worth its context/token cost? Does this task require this specialist capability? Do not call Jev for obvious trigger matches. Do not call Jev independently for hundreds of skills. Use deterministic candidate reduction first. Batch classification where economical. Cache only while task state, skill version, relevant repository state remain unchanged.

## P10 — Skill Disclosure Levels
Like tool disclosure, skill disclosure should be progressive. Level 0: ID + one-line description. Level 1: trigger + objective + output. Level 2: full compact skill instructions. Only selected skills reach Level 2. This reduces prompt/context cost.

## P11 — Temporary Specialist Composition
Do not create dozens of permanent agents. Compose specialists dynamically. Example: Base Scout + trace-callers + security-impact + auth-related evidence → temporary authentication-impact scout. Another: Base Reviewer + migration-safety + database-invariants → temporary migration reviewer. Another: Base Executor + React conventions + component-testing → temporary frontend executor. The specialist exists only for the task/job.

## P12 — Specialist Packet
A temporary specialist should receive: base role instructions, selected compact skills, task specification, acceptance criteria, relevant context, required tools. Do not send: entire skill registry, unrelated skills, unrelated memory, irrelevant tools, unrelated task history. Integrate with the Context Intelligence program.

## P13 — Skill Interaction / Conflict Detection
Multiple skills may conflict. Detect: contradictory procedures, overlapping instructions, incompatible tool assumptions, redundant skills, conflicting output contracts. Use deterministic metadata where possible. If conflict is ambiguous, use a judgement step. Do not silently combine conflicting active skills. Record resolution.

## P14 — Skill Scorecard
Create empirical skill evaluation. Track skill × role × task class × complexity × repository × model × workflow strategy. Metrics: number of uses, first-pass success, fix-round rate, gate-red rate, review request_changes, accepted-task success, tokens, cost, latency, tool calls, downstream defects, human intervention. Where possible compare role without skill vs role + skill.

## P15 — Marginal Skill Value
The important metric is not "Does this skill appear on successful tasks?" It is "Does adding this skill improve expected outcome enough to justify its cost?" Estimate marginal quality effect, marginal token effect, marginal latency effect, marginal cost effect. A skill that adds 2,000 tokens but prevents expensive fix rounds may be valuable. A skill that adds 2,000 tokens and changes nothing should be demoted.

## P16 — Skill Redundancy Measurement
Measure overlap between skills. Example: repo-map and trace-callers may sometimes retrieve overlapping information. Track duplicated evidence, duplicated tool calls, overlapping findings, additional unique findings. Use this to avoid loading redundant skill combinations.

## P17 — Skill Promotion Framework
Integrate with existing promotion/demotion infrastructure. A skill moves testing → shadow → active only when sufficient evidence exists. Promotion should consider quality, accepted-task economics, first-pass success, fix rounds, token overhead, latency, safety. Do not promote based on one successful run.

## P18 — Automatic Skill Demotion
An active skill should be demotable. Triggers may include increased fix rounds, increased token use without quality benefit, repeated irrelevant activation, outdated repository assumptions, conflicts with newer skills, security concern, tool/API incompatibility. Demotion should be reversible.

## P19 — Repository-Specific Skills
Allow repositories to define local skills. Examples: architecture conventions, migration procedure, internal API patterns, testing conventions, deployment constraints, common gotchas. Repository skills should normally outrank generic external skills when evidence shows they are more applicable. Do not automatically generalize repository-specific knowledge globally.

## P20 — Learned Skills
Introduce a controlled mechanism for proposing new skills from repeated successful behavior. Do NOT let an agent directly write and activate its own permanent instructions. Pipeline: repeated pattern detected → skill candidate → structured draft → quarantine/testing → eval → shadow → promotion. Potential triggers: same successful procedure repeated across tasks, same Planner instruction repeated, recurring fix strategy, repeated scout investigation sequence, recurring review checklist. This creates learning without uncontrolled self-modification.

## P21 — Skill Synthesis
When proposing a learned skill, derive it from evidence. Include source tasks, successful outcomes, repeated procedure, relevant failures, scope of applicability, counterexamples, confidence. Do not generalize from a single anecdote.

## P22 — Skill Versioning
Skills evolve. Every material change creates a new version. Track outcomes separately by version. Do not allow an improved-looking rewrite to inherit the old version's performance score automatically. Support rollback.

## P23 — Skill Freshness
Skills can become stale when repository architecture changes, tool interfaces change, frameworks update, APIs change, workflow policy changes. Track dependencies where practical. Mark potentially stale skills for revalidation. Do not silently trust old procedural knowledge forever.

## P24 — Skill + Model Interaction
Some skills may provide greater value to weaker models. Example: Luna + strong procedural skill may approach stronger model without skill. Measure this. Track skill × model → outcome. This is particularly important for the broader KGPT direction: Jev + cheap model + high-quality skills + good context → potentially strong cost-adjusted performance. Do not assume skill effects transfer equally across models.

## P25 — Skill + Workflow Interaction
Skills may also depend on workflow. Example: trace-callers may be highly valuable for dependency_scout_execute but less useful for localized_direct_execute. Track skill × workflow strategy × task class. This should integrate with existing workflow-strategy learning.

## P26 — Skill + Context Interaction
Integrate with Context Intelligence. A skill should be able to declare what evidence it requires. Example: trace-callers requires changed symbols, graph evidence, relevant source. The context router should use this declaration when compiling the packet. Do not independently rediscover the same evidence for each skill.

## P27 — Skill + Tool Disclosure
A skill should declare required/optional tools. If selected: required tool schemas may be disclosed. If not selected: those tool schemas need not be loaded. Example: trace-callers → graph/search tools; browser-debug → browser tools. This connects skill routing to tool-context economy.

## P28 — Skill Evidence Sharing
Multiple specialists working on one goal should reuse factual evidence. Example: dependency scout → caller evidence; executor → consumes caller evidence; reviewer → consumes factual caller evidence but independently judges correctness. Share facts. Do not share verdicts that would compromise independent review.

## P29 — Skill Decision Logging
Extend structured decision logging. For each skill decision record: task, role, candidate skills, deterministic triggers, Jev signals, historical evidence, selected skills, rejected skills, reason, skill versions, estimated token overhead, eventual outcome. Do not store hidden chain-of-thought.

## P30 — Skill Economy Metrics
Add: Skill tokens per accepted task (total skill-instruction tokens consumed). Skill overhead ratio (skill_tokens / total_model_input_tokens). Skill utility (useful outcome improvement relative to skill overhead). Skill reuse rate (how often durable skills avoid rediscovery/reasoning). Skill recovery rate (how often an initially hidden skill later had to be loaded; high recovery may indicate overly aggressive skill filtering).

## P31 — Skill Discovery from External Repositories
Create a safe optional discovery workflow: external repository → identify candidate skill documents → extract metadata → quarantine → inspect → compact → sandbox → benchmark → review → shadow → promote. Do NOT clone/execute arbitrary repositories merely to discover skills. Prefer static content retrieval first. Any executable dependency requires explicit sandbox validation.

## P32 — External Skill Provenance
Every external skill must preserve: source repository, source path, source commit/version, retrieval date, license where relevant, content hash, modifications made during compaction. Never lose provenance after importing.

## P33 — External Skill Update Detection
Optionally detect when an upstream skill changes. Do not automatically replace the active local version. Instead: new upstream version → candidate version → quarantine → diff → test → shadow → promote.

## P34 — Security Invariants
External or learned skills cannot override: Planner source-edit restrictions, task scope, tests-green, security review, dependency rules, merge serialization, human approval of main, trust/provider policy, secret handling, tool permissions. Skills are capability modules, not policy authorities.

## P35 — Avoid Agent Explosion
Do NOT create one permanent agent per language, per framework, per repository, per skill. Prefer Base Role + Task + Selected Skills + Selected Context + Selected Tools + Selected Model. This should remain the dominant composition model.

## P36 — Skill Evaluation Suite
Build representative tests. At minimum: Routing (irrelevant skill excluded; obvious skill deterministically selected; ambiguous skill resolved; only minimal set selected; unavailable skill handled). Lifecycle (external skill starts quarantined; quarantined skill cannot affect production; testing does not mutate production; shadow does not alter production behavior; promotion requires evidence; demotion works; rollback works). Security (malicious skill cannot override policy; executable external skill remains sandboxed; secret access blocked; external network behavior controlled). Compaction (compact version retains required procedure; token count reduced; output contract preserved). Specialists (correct temporary specialist composition; unrelated skills excluded; conflicts detected; required tools exposed). Scorecard (usage attribution; model interaction; workflow interaction; marginal-value measurement; insufficient evidence handled safely). Learned skills (repeated behavior may create candidate; one-off behavior cannot auto-promote; candidate enters quarantine/testing; activation requires evaluation).

## P37 — Shadow-First Rollout
Introduce new skill intelligence incrementally. Recommended order: Stage 1 Registry + inventory + telemetry. Stage 2 Compaction + metadata + static triggers. Stage 3 Dynamic skill disclosure in shadow. Stage 4 Skill scorecard. Stage 5 Active routing for existing trusted skills. Stage 6 Temporary specialist composition. Stage 7 External skill quarantine/testing. Stage 8 Learned skill proposals. Stage 9 Jev skill routing. Stage 10 Evidence-driven promotion/demotion. Do not implement "download arbitrary GitHub skill and run it" as an early feature.

## P38 — Integration with Existing Orchestrator Intelligence
Skill Intelligence must integrate with: Model routing (which model should execute?), Planner routing (Opus or Fable?), Context routing (which evidence should be presented).
