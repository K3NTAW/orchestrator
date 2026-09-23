# Hermes hardening audit

This program borrowed bounded memory, cache-aware prompt construction, and defensive context handling from
Hermes Agent, but retained the orchestrator's existing control plane. Hermes keeps small memory and user files
stable in the system prompt, exposes explicit memory edits, stores searchable session history in SQLite/FTS5,
orders prompt material from stable to volatile, compresses older conversational context, scans repository context
for instruction injection, restricts subprocess environments, and redacts credential-shaped log data. Its
documented subagent controls did not establish the inspection, steering, cancellation, partial-result, or
role-specific structured-output contracts needed here. Tirith scanning, command approval, containers, SSRF
controls, direct-message pairing, cron, and messaging solve a different workflow and were not imported.

The result is an adopt/adapt/skip program rather than a port. "Adopt" means the underlying mechanism fits this
repository; "adapt" means its idea is retained behind the orchestrator's telemetry, evidence, or shadow-first
contracts; "skip" means it is deliberately outside the program.

## Adopt, adapt, or skip

| Area | Decision | Merged implementation |
| --- | --- | --- |
| Always-loaded memory | Adapt | `memory_store.py` and `memory_hot.py` generate a token-bounded HOT view while preserving pinned constraints, gotchas, invariants, and governing decisions. |
| Historical memory and search | Adopt | `memory_store.py` maintains a rebuildable SQLite index with FTS5 when available and a parameterized LIKE fallback; Markdown remains the COLD archive. |
| Compaction | Adapt | `memory_hot.py` deterministically compacts only the generated HOT view; source records are retained in the structured store and Markdown archive. |
| Memory provenance | Adopt | `memory_store.py` records provenance, outcomes, revert paths, supersession, source tasks, and source goals; migration is reversible by rebuilding from Markdown. |
| Prompt-cache telemetry | Adopt | `cache_telemetry.py` normalizes provider cache buckets, computes effective context cost, and reports rendered prefix identity and size from run metadata. |
| Stable prompt prefixes | Adapt | Role rules precede the task packet boundary in the merged prompt templates; `spawn.py` measures the rendered prefix and task-specific suffix. |
| Cache-aware context, tool, and skill disclosure | Adapt | `context_router.py`, `tool_catalog.py`, `skill_router.py`, and `spawn.py` record cache-aware proposals; shadow modes do not change worker-visible content. |
| Worker registry and inspection | Adopt | `worker_registry.py` stores bounded snapshots and structured events; `orchestrator workers` provides read-only inspection. |
| Steering | Adapt | `worker_control.py` records and redacts a message, interrupts the process, and resumes the recorded Claude session or Codex thread without mutating the original task contract. |
| Cancellation and partial results | Adopt | `worker_control.py` terminates a recorded process, preserves its worktree, releases capacity, and derives a bounded partial result only from observable repository evidence. |
| Structured worker outputs | Adopt | `contracts.py` validates role-specific envelopes and provides deterministic repair plus at most one bounded provider repair where a hard budget can be enforced. |
| Hostile context scanning | Adopt | `context_scanner.py` assigns deterministic findings and fixed authority classes; `evidence.py` persists the resulting trust class without granting content authority. |
| External skill safety | Adapt | `skill_discovery.py` and `skills_registry.py` keep fetched skills inert, persist structured inspection risk, and gate lifecycle transitions. |
| Credential filtering | Adopt | `env_policy.py` computes an allowlisted environment and logs stripped key names only; launchers apply filtering only when its mode is active. |
| Orchestration overhead | Adapt | `overhead.py` reports orchestration-to-execution token amplification, cost share, and latency share for accepted goals. |
| Fast path and harness depth | Adopt | `harness_depth.py` assigns deterministic levels from existing task evidence; only promoted active levels can skip the named optional routing stages, never hard gates. |
| Stale work and critical-path steering | Adapt | `stale.py`, `steering_policy.py`, and `critical_path.py` turn observed severity and priority into continue, steer, or cancellation proposals; shadow remains observational. |
| Interrupted-worker evidence reuse | Adapt | `worker_control.py` and `evidence.py` preserve bounded `worker_partial` facts and route them only when fresh and relevant. |
| Messaging, cron, approval modes, containers, SSRF controls, and pairing | Skip | None was added by this program; the repository's scheduler, access model, guardrails, and human review remain authoritative. |

## Resolved uncertainties

- **Cross-invocation cache reuse:** provider usage already showed cache reads within Claude runs and resumed Codex
  threads, but cross-run reuse could not be attributed without a stable identity. The merged implementation records
  `prefix_sha`, prefix characters, suffix characters, and packet sections after rendering; it does not infer reuse
  for older rows that lack those fields.
- **Steering a running Claude worker:** the non-interactive Claude process has no live stdin steering channel.
  Steering therefore records the message durably, interrupts the process, and resumes its stored session. Codex
  follows the corresponding recorded-thread resume path. The original spec, acceptance, and scope are unchanged.
- **FTS5 availability:** the local SQLite used by earlier recall data supported FTS5, but portability was not
  assumed. `memory_store.py` feature-detects FTS5 and falls back to parameterized LIKE search with a warning.

## P36 anti-goals

The program deliberately did not replace the scheduler, scorecards, dependency DAG, worktree isolation, hooks,
`tests-green` gate, serial merge queue, or human merge review. It did not add a general chat or messaging system,
cron service, provider command-approval framework, container runtime, SSRF subsystem, or direct-message pairing.
It also did not make shadow decisions worker-visible: shadow paths write decision-log or run rows only. Promotion
remains evidence-based and operator-controlled; evaluation and scorecard commands report evidence but do not
rewrite configuration.

## Document index

- [01 — Memory store](01-memory-store.md): structured WARM/COLD records, migration, rebuild, search, and provenance.
- [02 — Cache telemetry](02-cache-telemetry.md): normalized cache buckets, effective cost, prefix identity, and reporting.
- [03 — Worker registry](03-worker-registry.md): durable worker snapshots, events, reconciliation, and inspection.
- [04 — Context scanner](04-context-scanner.md): deterministic injection findings, authority classes, and evidence trust.
- [05 — Overhead](05-overhead.md): accepted-goal orchestration amplification, cost share, and latency share.
- [06 — Credential filtering](06-credential-filtering.md): allowlisted worker environments and names-only shadow telemetry.
- [07 — HOT memory](07-hot-memory.md): bounded compaction, packet retrieval, shadow-first activation, and rollback.
- [08 — Memory scorecard](08-memory-scorecard.md): retrieval precision, usefulness, outcomes, and isolated memory evaluation.
- [09 — Stable prefixes](09-stable-prefixes.md): prompt ordering, prefix measurements, and unavoidable launch differences.
- [10 — Cache-aware disclosure](10-cache-aware-disclosure.md): shadow context adjustment and stable tool/skill catalogs.
- [11 — Worker control](11-worker-control.md): explicit cancellation, observable partial results, and interrupt/resume steering.
- [12 — Steering policy](12-steering-policy.md): stale, stuck, scope, failure, and critical-path steering decisions.
- [13 — Output contracts](13-output-contracts.md): role schemas, validation, bounded repair, telemetry, and promotion evidence.
- [14 — Skill hardening](14-skill-hardening.md): inert external-skill inspection and risk-gated lifecycle transitions.
- [15 — Harness depth](15-harness-depth.md): deterministic depth levels, the small-task fast path, and safety boundaries.
- [16 — Evidence reuse](16-evidence-reuse.md): preservation and relevance routing for interrupted-worker facts.
- [17 — Memory, skills, and strategy](17-memory-skills-strategy.md): learned-skill candidates and merged-lineage strategy records.
- [18 — Handoff economics](18-handoff-economics.md): cache retention, reconstruction, duplication, latency, and effective handoff cost.
- [19 — Promotion and evaluations](19-promotion-and-evals.md): feature gates, deterministic Hermes evaluation, and the unified scorecard.

## Operator run-order

Run these commands from the repository root, in order:

1. `uv run orchestrator memory migrate` builds or updates the derived memory index from the Markdown archive.
2. `uv run orchestrator memory compact` regenerates the bounded HOT view and restores HOT tier marks.
3. `uv run orchestrator hermes-eval --json` runs the isolated deterministic Hermes cases and writes the evaluation report.
4. `uv run orchestrator scorecard --hermes` reads the program's combined outcome, memory, cache, worker, security, and overhead evidence.
5. `uv run orchestrator promotion` prints recommendations for operator review; change modes separately only after reviewing the evidence and the relevant document above.
