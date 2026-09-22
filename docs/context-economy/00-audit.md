# Context economy: P0 audit

**Scope and provenance.** This is the P0 synthesis of scouts C-A, C-B, and C-C, using run records from 2026-09-20 through 2026-09-22. All source anchors below were checked in this worktree on 2026-09-22. The synthesis's older `spawn.py:195-366` execute anchor remains correct; its review cap was described as `:426-505`, which remains correct, but the current effective diff-budget calculation is specifically `spawn.py:497-501` (rather than a flat 8,000-character diff).

## 1. Token entry points by role

Approximate packet tokens use characters / 4; they describe prompt material, not provider-reported usage.

| Role | Packet sections and approximate tokens | Assembling code |
| --- | --- | --- |
| execute | bounded packet body, at most 4,800 chars / ~1,200 tokens: objective, acceptance, base, write/read scope, constraints, tests, symbols, gotchas, decisions, verification, evidence | `orchestrator/spawn.py:195-402`; dispatched at `:787` |
| review | spec, acceptance, scope, bounded diff, changed tests, gate, and conditional role/security/fix context; total target is 8,000 chars / ~2,000 tokens | `orchestrator/spawn.py:426-505`; effective diff budget `:497-501` |
| spec_review | uncapped spec, acceptance, scope, dependencies, existing tests, complexity, tier | `orchestrator/spawn.py:508-524` |
| scout | question, expected output, scope tree (60 entries), memory titles, result contract | `orchestrator/spawn.py:527-546` |
| headless planner | `planner.md` system instructions (4,307 bytes / ~1,077 tokens in the P0 snapshot) plus a decision packet capped at 6,000 chars / ~1,500 tokens | `orchestrator/goals.py:342-345`; `orchestrator/planner_packet.py:194-250` |

## 2. Repeated context

The task contract is reconstructed independently for each role. Execute places acceptance and scope in `_packet_body` (`spawn.py:298-315`); review re-pulls spec, acceptance, and scope from the reviewed task (`spawn.py:449-451`); spec review does the same (`spawn.py:520-523`). There is no shared section-object or content-addressed reuse between those builders.

Execute has a second inclusion path: the packet contains the task contract, and the execute prompt contains literal `Spec`, `Acceptance`, and `Scope` material. This is the first deterministic removal candidate after baseline instrumentation.

Memory is also recalled per packet: execute calls `memory_recall` with notes and bus at `spawn.py:276-280`, while scout makes a separate notes-and-bus call at `spawn.py:537-539`. Review and spec-review packet builders do not make the corresponding recall.

## 3. Duplicated retrieval

`memory_recall` runs at packet construction, not once per task lineage: execute at `spawn.py:276-277` and scout at `spawn.py:537-538`. Its query is title plus scope, so equivalent context is rediscovered separately.

`orchestrator/scout_evidence.py:29-39` deliberately duplicates spawn's lazy recall loader. `reuse_check` expands its request to `notes`, `bus`, and `graph` at `scout_evidence.py:74-80`, then applies 14-day freshness and base/moved-path checks (`:53-106`). It runs once when a scout is spawned (`orchestrator/mcp.py:76-96`), not for execute/review packets. The graph layer therefore reaches only this cross-task scout-reuse path.

The repository map is outside packet assembly: it is built by the CLI at `orchestrator/cli.py:281-283` and refreshed during merge at `orchestrator/merge.py:25-92`.

## 4. Tool and MCP exposure

Workers use an explicit, strict role configuration: `.mcp.<role>.json` wins, otherwise `.mcp.worker.json` is used (`spawn.py:585-604`). The P0 role map is: execute gets the bus-only worker surface; scout and review add read-only GitHub; planner adds GitHub and the orchestrator MCP. Strict configuration prevents implicit project/user MCP loading.

All non-execute workers receive `--disallowedTools Edit,Write,NotebookEdit` at `spawn.py:601-606`. This is an exposure control, not merely prompt guidance.

## 5. Instructions always loaded

The headless planner alone supplies instructions as a system prompt via `--append-system-prompt` (`goals.py:342-345`). Execute, review, spec-review, and scout instructions ride in their submitted `-p` prompt; role packet construction happens in `spawn.run_worker` (`spawn.py:748-793`). Thus instruction text is loaded on every role invocation, even where task-contract sections are identical.

## 6. Handoff paths

| Path | What is sent | Verified implementation |
| --- | --- | --- |
| Compatible resume | `codex exec resume`, prior thread, and repair delta—the cheapest handoff | `orchestrator/executor.py:185-197`, `:396-417` |
| Fresh fix round | new worker execution and full task packet plus repair delta; bounded at five rounds | `orchestrator/executor.py:17`, `:401-428` |
| Opus to Fable escalation | filtered Opus decision, unresolved items, relevant state, optional conflict/scout/memory data; capped at 6,000 chars | `orchestrator/planner_packet.py:253-305`; cap machinery `:194-222` |
| Handover snapshot | `plan.md` handover representation, at most 120 section lines, normally every 15 minutes | `orchestrator/handover.py:12-23`, `:256-331` |

## 7. Baseline from run logs

These figures are the C-C snapshot over 2026-09-20..22. The run-log fields used are **`role`, `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `usd`, and `task`**. `bus.log_run` is at `orchestrator/bus.py:278-335`; provider normalization reads input/cache/output variants at `:224-242`.

| Role | n | Mean input / cache-read input | Output | USD | Notes |
| --- | ---: | --- | ---: | ---: | --- |
| execute | 155 | 964k / 909k | 7.5k | 6.91 where logged | p90 input 1.78M |
| review | 127 | cache read 658k | 14.3k | 0.46 | input mean not retained in C-C summary |
| spec_review | 60 | cache read 859k | 15.4k | 0.57 | same qualification |
| scout | 9 | cache read 1.29M | 13.5k | 0.81 | small sample |

For 77 accepted execute roots, manually walked lineages estimated 3.73M mean tokens per accepted task, 2.44M median, and 32.3M maximum (confidence 0.55). Fix rounds consumed 19.8% of tokens, excluding review re-passes. Memory, `jev_route`, and scout-decision rows have no token fields, so they are unmeasured rather than free. Jev input totals were 750k, 956k, and 385k for 2026-09-19, -20, and -21. Planner counts cannot be verified where `runs/planner_runs.json` is absent; its reader is `orchestrator/scorecard.py:434-439`.

## 8. Jev call-site inventory

| Call site | Bounded payload and redaction | Questions / mode / fail-open |
| --- | --- | --- |
| `jev_gate.py:202-347` | title, spec `[:1500]`, acceptance, scope, 20 redacted recent calls, proposed input `[:300]`; recent inputs redact at `:111-118` | needed/redundant/destructive; log/sample/block; unavailable answer allows |
| `jev_route.py:97-136` | redacted spec `[:1500]`, acceptance, scope, complexity/class, memory titles | seven routing signals; off/shadow/active; `None` is neutral |
| `jev_planner.py:126-203` | redacted bounded state: subject/goal titles, spec excerpt, scope count, signals | five planner signals; shadow/active gate; unavailable records `jev_unavailable` |
| `jev_sched.py:152-257` | pair titles, scopes, redacted spec excerpts `[:400]`, up to eight pairs | pairwise scheduling questions; off/shadow/active; every error fails open |
| `jev_points.py:76-174` | redacted title `[:300]`, spec `[:600]`, scope entries and content-stripped evidence | per-point suggestion; off/shadow/active; deterministic policy remains authoritative |
| `jev_rank.py:41-61` | `goal_text` is not redacted by rank itself; caller owns that boundary; item batches | relevance noul question, default batches of 40; missing response retains candidates |

`jev_gate` also has session-scoped repeat tracking at `jev_gate.py:150-199`; this is the Jev-side repeat dedup identified by C-B.

## 9. Decisions and promotion surface

Today `decision_log.KINDS` has 13 decision kinds: `routing`, `wave`, `capacity`, `concurrency`, `merge_pressure`, `stale`, `jev_sched`, `allocation`, `strategy`, `speculation`, `scout`, `review_plan`, and `planner_route` (`orchestrator/decision_log.py:12-26`). Outcomes are linked separately by `decision_log.outcome` (`:131-139`); P0 found allocation to be the only current outcome call site.

P26 adds `context_selection`, `evidence_reuse`, `retrieval`, `tool_disclosure`, `instruction_loading`, `action_gate` (renaming gate), and `workflow_strategy` (mapping to existing strategy).

`promotion.FEATURES` supports `jev_routing`, `scheduler`, `jev_sched`, `allocation`, `strategy`, `speculation`, and `planner_routing` (`orchestrator/promotion.py:10-26`). P30 candidates are the shadow-first context features implied by P26: section/context selection, evidence reuse, retrieval, tool disclosure, instruction loading, and action gating. They should inherit the existing off/shadow/active evidence discipline.

## 10. Deterministic rules before Jev

Deterministic code should win where it can prove a safe, cheap result: packet caps and trimming (`spawn.py:333-352`), task/scope validation, read-only tool restrictions, duplicate execute-prompt removal, reuse freshness/base checks, and scheduler/router hard constraints. It remains authoritative at experimental Jev points (`jev_points.py:1-4`).

Jev belongs before costly work only for ambiguity: whether a proposed tool call is needed before executing it (`jev_gate.py:202-347`), suitability routing before worker allocation (`jev_route.py:97-120`), ambiguous scheduling pairs before competing work starts (`jev_sched.py:194-218`), planner uncertainty before escalation (`jev_planner.py:126-151`), and relevance ranking before expanding a large candidate set (`jev_rank.py:41-61`). Each must keep its neutral/fail-open route until shadow evidence meets promotion criteria.

