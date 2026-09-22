# P0 skill inventory

This inventory records the twelve repository skills before dynamic routing. Eleven skills are symlinked into
`.claude/skills`, exposing their descriptions to every Claude worker. Codex executors load `implement-spec`
through `AGENTS.md`. Worker prompts do not name skills. There was no registry, telemetry, or per-skill testing
apart from `tests/test_memory_skill.py`. Task classes come from `attribution.task_class` (security,
architectural, debugging, mechanical, unfamiliar, or explicit `constraints.task_class`); workflow strategies
come from `strategy.STRATEGIES`. This registry is P37 stage 1 only and changes no worker behavior.

“Always” means statically exposed to the applicable workers today; none are dynamically selected.

| id | roles | purpose | trigger | tokens l0/l2 | tools | output contract | repo specificity | always exposed? | dynamic? | tests | provenance |
|---|---|---|---|---:|---|---|---|---|---|---|---|
| executor/implement-spec | executor | Implement one atomic task | task delivered by orchestrator | 36/141 | failures_only.sh | 3-line summary, diff stat, failures | orchestrator | Codex via AGENTS | no | none | builtin |
| planner/memory | planner | Recall and record layered memory | goal start, merge, prior-art question | 50/1044 | graph, recall, record scripts | memory entries or graph summary | orchestrator | yes | no | test_memory_skill | builtin |
| planner/orchestrate | planner | Route a software goal end to end | feature, refactor, or bug goal | 39/999 | none | accepted goal and retrospective | orchestrator | yes | no | none | builtin |
| planner/resume | planner | Resume interrupted planning | restart or context reset | 22/245 | none | continued plan execution | orchestrator | yes | no | none | builtin |
| planner/write-spec | planner | Write an atomic executor spec | splitting a synthesized plan | 28/572 | none | bus execute task | orchestrator | yes | no | none | builtin |
| review/adversarial-review | review, spec_review, challenge | Review a scoped diff | orchestrator review task | 30/164 | none | verdict JSON | orchestrator | yes | no | none | builtin |
| review/challenge | review, spec_review, challenge | Refute uncertain findings | challenge task | 20/113 | none | verdict/evidence JSON | orchestrator | yes | no | none | builtin |
| scout/repo-map | scout | Map repository layout | architecture or location request | 31/192 | repo_map.sh | scout findings JSON | orchestrator | yes | no | none | builtin |
| scout/test-gap | scout | Find untested paths | coverage, missing tests, refactor risk | 27/151 | coverage_gaps.sh | scout findings JSON | orchestrator | yes | no | none | builtin |
| scout/trace-callers | scout | Trace symbol readers and callers | impact question | 30/121 | trace.sh | scout findings JSON | orchestrator | yes | no | none | builtin |
| triage/classify | triage | Classify incoming items | issue, log, or finding triage | 28/93 | none | one JSON line per item | orchestrator | yes | no | none | builtin |
| triage/compact-memory | triage | Compact durable memory | monthly or over budget | 27/77 | none | five-line diff summary | orchestrator | yes | no | none | builtin |
