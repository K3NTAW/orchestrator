---
name: write-spec
description: Write one atomic execute-task spec for the Codex executor (≤5 files, one acceptance cluster, explicit scope). Use when splitting a synthesized plan into bus tasks.
---
# Write an atomic spec
A human reviews merges. The TaskCreated hook rejects any task without Acceptance and Scope.
- Title: verb + object. Spec: what and why in ≤12 lines, citing path:line from scout findings.
- Acceptance: 2–5 externally checkable statements (test names, CLI output, observable behavior).
- Scope: glob list; concurrent execute tasks must have disjoint scopes.
- Constraints: read_only=false, timeout_s, budget_turns; no new dependencies unless named here.
- Render `.orchestrator/prompts/execute.md` with these fields for the `codex` call.
Call `bus_create_task(role="execute", tier="astra", complexity=N, parent=GOAL_ID, ...)`.
