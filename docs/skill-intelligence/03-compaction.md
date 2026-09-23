# Skill compaction

Measurements use `skills_registry.sync` at `HEAD~` and in the working tree. Level 0 is the description estimate; Level 2 is the procedural payload estimate (P6 metadata sections are indexed separately). The 12 Level 2 payloads fell from 3,071 to 1,541 estimated tokens, a reduction of 1,530 (49.8%).

| Skill | Before L0 | Before L2 | After L0 | After L2 | Removed | Kept verbatim |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| executor/implement-spec | 37 | 132 | 25 | 62 | Role/policy repetition and step narration | `scripts/tests_green.sh`, `scripts/failures_only.sh`, Scope/dependency/commit rules, final fields |
| planner/memory | 85 | 866 | 16 | 425 | Layer table and repeated explanations | Every script invocation and option, store/reference paths, limits, provenance and secret guardrails |
| planner/orchestrate | 42 | 832 | 24 | 391 | Repeated role prose and expanded hygiene explanation | Bus/spawn/daemon/gate/merge/memory calls, routing thresholds, review and commit flow |
| planner/resume | 23 | 199 | 21 | 81 | Repeated role prose | `bus_read`, `status`, `codex_reply`, state recovery and review thresholds |
| planner/write-spec | 39 | 392 | 20 | 193 | Repeated human-review prose and field explanations | Test format, all task fields, hook/complexity/dependency rules, prompt and bus call |
| review/adversarial-review | 38 | 113 | 16 | 67 | Motivational framing | -U3/read limit, security checklist path, exact verdict JSON and bus delivery |
| review/challenge | 23 | 85 | 17 | 47 | Repeated role prose | Read-only/tool-call guardrails, verdict meanings, exact JSON |
| scout/repo-map | 40 | 133 | 16 | 72 | Role prose | Both script invocations, graph condition, ≤80 lines, citation and scout schema |
| scout/test-gap | 30 | 116 | 9 | 54 | Role prose | Script invocation, no-test-read rule, ranking source, block path, schema/20 limit |
| scout/trace-callers | 37 | 76 | 19 | 56 | Role prose | Script invocation, no-grep rule, dynamic-access handling, schema/20/path:line contract |
| triage/classify | 36 | 66 | 14 | 56 | Tier and role prose | Read boundary, untrusted-data guardrail, exact JSON line and bus delivery |
| triage/compact-memory | 41 | 61 | 22 | 37 | Role prose | Memory paths, 300-line limit, preservation/supersession rules, five-line bus summary |

Level 0 descriptions retain each skill's first-sentence purpose. Level 1 exposes only Trigger, Objective, and Output contract. Level 2 exposes the complete compact P6 body. This changes disclosure structure and deterministic discovery metadata, not installed skill exposure.
