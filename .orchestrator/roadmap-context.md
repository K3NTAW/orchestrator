# Jev Context Intelligence & Harness Economy

Filed by the user 2026-09-22 14:10 (verbatim brief, the program's source of truth for the orchestrator repo). Planner checkpoint and phase status live in plan.md; this file changes only by the user.

## Mission

Extend Orchestrator's existing adaptive architecture so it optimizes not only which model runs, when tasks run and which workflow runs, but also: exactly which context each model receives; how much detail each piece of context receives; which tools are disclosed; which instructions are loaded; when retrieval is necessary; when evidence can be reused; when model handoffs are economically worthwhile; when proposed actions are redundant, unnecessary, or risky.

Central objective: minimize total tokens, latency, model calls, retrieval work, and tool overhead per accepted goal while preserving or improving accepted-goal quality. Do NOT optimize by removing useful reasoning. Optimize by removing irrelevant context, repeated context, duplicated retrieval, unnecessary tool schemas, unnecessary instructions, redundant model handoffs, redundant reads/searches, redundant tool calls.

Jev becomes a cheap System-One decision layer underneath the existing expensive agents. It must remain optional, measurable, bounded, fail-open where appropriate, subordinate to deterministic safety constraints, shadow-first.

## Core principle

Today: task → model → workflow → scheduling. Extend to: task → retrieve candidate evidence → classify evidence relevance → compile minimum-sufficient context → disclose minimum-sufficient tools → load relevant conditional instructions → select model → execute → gate actions → verify → measure outcome.

Target architecture: Goal/Task → deterministic metadata → (candidate evidence, task characteristics) → Context Router (with Jev) → HIDE / SHORT / LONG / FULL → role-specific packet → (tool disclosure, conditional rules) → Model Router (scorecard + Jev) → model → proposed action → Action Gate (deterministic + Jev) → tools → outcome → attribution/scorecard → promotion/demotion.

## P0 — Audit before implementation
Inspect: role-specific context compilation; Planner packets; Planner escalation packets; executor packets; fix-round packets; scout packets; review packets; spec-review packets; challenge packets; memory retrieval; graphify; repository exploration; tool descriptions/schema exposure; MCP tool exposure; Jev client; Jev tool gate; Jev executor routing; Jev Planner routing; Jev scheduling; scorecard; attribution; decision logging; promotion/demotion; executor routing; Planner routing; scheduler.
Determine: where tokens enter model context; which context is repeated; which agents independently retrieve the same evidence; which tool schemas are always exposed; which instructions are always loaded; how much context is role-specific already; where Jev can decide before expensive work; where deterministic logic is preferable to Jev; where handoffs cause context reconstruction. Do NOT reimplement existing optimizations. Create a baseline before behavior changes.

## P1 — Context economy telemetry
For every model invocation record: role, model, task, goal, packet version, total input tokens, task/spec tokens, acceptance tokens, memory tokens, repository evidence tokens, scout evidence tokens, previous-result tokens, tool-definition tokens, instruction tokens, conversation/history tokens, other tokens. Deterministic approximation where exact tokenizer attribution is impractical. Track candidate context tokens (everything available before filtering), presented context tokens (sent), context reduction ratio (presented / candidate), context amplification (repeated presentation of the same logical evidence across pipeline agents: logical_evidence_tokens_sent_across_pipeline / unique_logical_evidence_tokens), exposing evidence repeatedly sent Planner → executor → reviewer → security reviewer → fix round. Lower context is not automatically better; always correlate with quality.

## P2 — Canonical evidence objects
Reusable structured evidence: evidence ID, source type, source location, repository commit/version, content hash, task relevance metadata, summary variants, provenance, freshness, security/trust classification. Source types: source chunk, test result, scout finding, memory entry, graph finding, previous task result, review finding, architecture note, decision, external documentation. IDs let workers reference the same evidence without rediscovery. Do not force every model to consume the same representation.

## P3 — Query-time context routing
A context router; importance depends on task, role, current decision, repository state. Per candidate chunk: HIDE (not presented), SHORT (minimal structured description, e.g. "auth/session.py — session creation and refresh logic; changed recently by T-0412"), LONG (detailed summary preserving relevant interfaces, behavior, risks, relationships), FULL (relevant raw chunk, bounded, not necessarily the whole file). Deterministic relevance first; Jev only when ambiguous.

## P4 — Deterministic context rules first
Deterministic FULL: files explicitly in scope when implementation requires editing them; exact failing test output; exact acceptance criteria; current task specification. Deterministic HIDE: unrelated completed-task transcripts; unrelated memory; unrelated tool output; stale evidence superseded by newer evidence. Jev only for ambiguous relevance.

## P5 — Jev context classification
Typed questions: materially relevant to completing the task? requires full evidence rather than a summary? could hiding it materially increase failure risk? primarily background rather than actionable? relevant to this specific role? Batch evidence; avoid one request per tiny chunk; coarse retrieval before Jev (repository → deterministic top 30 → grouping → Jev ranking → top evidence → HIDE/SHORT/LONG/FULL). Never run Jev over the entire repository.

## P6 — Role-aware context routing
Same evidence, different representation per role: executor FULL; reviewer relevant diff + interface summary; security reviewer security-relevant diff + trust-boundary context; Planner architecture/interface summary; scout FULL only when answering about its implementation. No universal packet.

## P7 — Query-time summarization
Summaries conditioned on current task, role, requested decision; the same file gets different summaries. Cache only with a key of source hash + query/task class + role + summary level; invalidate on source change.

## P8 — Shared retrieval layer
A goal/task evidence pool: retrieval → pool → Planner (summary), executor (full), reviewer (diff/summary). Share facts/evidence, not verdicts; reviewers stay independent.

## P9 — Retrieval deduplication
Before Read, Grep, Glob, graph lookup, scout, memory retrieval, repository-map call: check whether equivalent fresh evidence exists; reuse when safe. Track retrieval attempted, cache/evidence hit, retrieval skipped, tokens avoided, stale evidence invalidation. Never reuse across repository versions without validating freshness.

## P10 — Tiered tool disclosure
Level 0 catalog (compact identity + one line, e.g. codex — implement software tasks; browser — interactive browser automation; memory — retrieve durable project knowledge; graph — inspect code relationships); Level 1 selected tool description; Level 2 full schema only when likely used. task → tool category classification → compact catalog → selected tools → full schemas → model. Measure tool-definition tokens before/after, tool-selection success, wrong-tool rate, missing-tool recovery.

## P11 — Jev tool disclosure
Jev may classify useful tool categories (repository search? graph lookup? memory retrieval? a scout? Codex execution? another reviewer?). Jev cannot remove mandatory tools required by role/safety. Support controlled disclosure escalation when a model discovers it needs a hidden optional tool; avoid restarting the task.

## P12 — Conditional instruction loading
Audit static prompts; convert instructions specific to languages, directories, frameworks, security domains, task classes, roles, tools into conditional modules (auth → auth-security instructions; migrations → migration instructions; frontend → frontend conventions; review role → review rules). Fundamental safety rules are never conditional; the base prompt stays minimal but sufficient.

## P13 — Instruction selection
Deterministic matching first (scope glob, language, task class, role, repository metadata); Jev only for ambiguous semantic relevance. Record which modules loaded; measure instruction tokens.

## P14 — Handoff economics
Handoffs cost: context reconstruction, escalation packet, warm-up/new-session overhead, repeated retrieval, duplicated reasoning, latency. Define handoff_cost and expected_route_cost = initial_model_cost + expected_handoff_cost + expected_retry_cost + expected_downstream_cost. Do not always start cheap → escalate when escalation is historically likely; sometimes start strong is cheaper overall.

## P15 — Planner handoff economics
Apply to Opus → Fable: Opus tokens before escalation, escalation packet tokens, Fable tokens after, duplicated context, resulting quality, whether starting Fable directly would have been cheaper. Route on expected total cost-to-decision, not model tier alone. Do not weaken Fable escalation quality protections.

## P16 — Executor handoff economics
Same for cheap executor → stronger executor/fix round, using first-pass probability, fix-round economics, cost-to-accepted-task, plus context reconstruction/handoff cost. The cheapest first attempt may not be the cheapest accepted task.

## P17 — Semantic action gating
Extend the Jev tool gate: evaluate tool, arguments, task, scope, role, current evidence, previous equivalent calls; classify necessary, redundant, risky/destructive, repeated, stale, likely low-value. Deterministic security rules remain above Jev; Jev cannot approve what policy denies.

## P18 — Repeated read/search suppression
Read, Grep, Glob, graph lookup, memory recall as token amplifiers: before execution check whether equivalent information was already retrieved, whether the source changed, whether another evidence object contains it, whether the query is meaningfully different. Jev only for ambiguous semantic equivalence. Measure repeated reads avoided, repeated searches avoided, token savings, false suppression/recovery.

## P19 — Trust-aware routing
Trust/data classes PUBLIC, INTERNAL, SENSITIVE, SECRET by repository/config policy. Hard policy decides which providers, models, external services and Jev requests may receive each class; routing happens within the permitted set; Jev cannot override; no secrets or raw sensitive content to Jev.

## P20 — Jev privacy boundary
Audit every Jev call site; each explicitly defines what leaves the machine. Prefer metadata, bounded specifications, titles, classifications, hashes, redacted summaries. Avoid secrets, credentials, full source unless policy permits, private raw memory, sensitive logs. Add tests.

## P21 — Context verification
Shadow evaluation: baseline packet vs optimized packet on selected tasks; track first-pass green, fix rounds, review findings, gate reds, accepted cost, accepted tokens; record whether hidden evidence later had to be retrieved; define context_recovery_rate (high means over-aggressive filtering).

## P22 — Tool disclosure verification
Measure hidden tool later requested, task failure due to unavailable tool, schema escalation frequency, tool-selection accuracy. Quality must remain non-inferior.

## P23 / P24 / P25 — Modes
Context router, tool disclosure and conditional instructions each support off / shadow / active; shadow computes and logs the optimized packet without affecting the model; default shadow until evidence exists; fundamental safety instructions never conditional.

## P26 — Unified harness decision log
Extend the decision log with kinds context_selection, evidence_reuse, retrieval, tool_disclosure, instruction_loading, model_routing, planner_routing, scheduling, action_gate, workflow_strategy; each with candidates, hard constraints, deterministic evidence, historical evidence, Jev evidence, selected action, rejected alternatives, reason, mode, eventual outcome. No chain-of-thought.

## P27 — Context scorecard
By role, model, task class, complexity, repository area, packet version: candidate context tokens, presented tokens, reduction, first-pass success, fix-round rate, recovery rate, accepted cost, accepted tokens, latency. Objective: minimum sufficient context, not minimum context.

## P28 — Tool economy scorecard
Tools disclosed, schemas disclosed, schema tokens, tools actually used, hidden-tool recovery, redundant calls, failed calls, repeated calls, Jev blocks, false blocks where detectable. tool_schema_efficiency = used_tool_schema_tokens / presented_tool_schema_tokens; useful_tool_call_rate. Define exact production metrics carefully.

## P29 — Handoff scorecard
Source model, destination model, reason, source tokens, handoff packet tokens, destination tokens, duplicated context estimate, final outcome, accepted cost, latency; decide when start cheap → escalate is worse than start strong.

## P30 — Promotion / demotion
Integrate context router, tool disclosure, conditional instructions, semantic read suppression, handoff-aware routing into the promotion framework. Promotion needs sample size, non-inferior quality, measurable efficiency improvement. Recommend demotion on lower first-pass success, more fix rounds, more recovery retrieval, more missing-tool events, higher accepted cost, significant quality regression. Never optimize a local metric while total accepted-goal economics worsen.

## P31 — Evaluation suite
Representative evals for context/tool economy: localized fix (large reduction expected); cross-cutting architecture task (less aggressive); security task (security instructions and evidence must remain); test failure (failing output and relevant code/tests stay full); documentation task (summaries over full chunks); review task (diff + acceptance + relevant interfaces; no implementation transcript); repeated fix round (reuse prior evidence, send delta). Measure all.
