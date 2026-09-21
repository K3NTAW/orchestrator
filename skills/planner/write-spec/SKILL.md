---
name: write-spec
description: Write one atomic execute-task spec for the Codex executor (one acceptance cluster and explicit scope). Use when splitting a synthesized plan into bus tasks.
---
# Write an atomic spec
A human reviews merges. The TaskCreated hook rejects any task without Acceptance and Scope.
- Title: verb + object. Spec: what and why in ≤12 lines, citing path:line from scout findings.
- Acceptance: 2–5 externally checkable statements (test names, CLI output, observable behavior).
- Name tests as `tests/test_<module>.py::test_name` (the gate parses only this form; bare names are not verified), and use `unittest.TestCase` methods unless pytest is a project dependency.
- Scope: glob list; concurrent execute tasks must have disjoint scopes.
- read_scope: paths the worker may inspect; list them so the execution packet can include them.
- interface_contract: functions, signatures, and file formats that other tasks rely on; state it explicitly for parallel or dependent work.
- task_class: `mechanical`, `unfamiliar`, `debugging`, `architectural`, or `security`; this feeds executor routing.
- route: `straightforward` or `full`; select the route appropriate to the goal's uncertainty and risk.
- Constraints: read_only=false, timeout_s, budget_turns; no new dependencies unless named here.
- depends_on: ids that must be merged first; complexity ≥5 gets a spec review before dispatch; test file = tests/test_<module>.py.
- Warn above five files; split only by independently verifiable behaviour and dependency boundaries, never to satisfy the count.
- Render `.orchestrator/prompts/execute.md` with these fields for the `codex(task_id, prompt)` call.
Call `bus_create_task(role="execute", tier="astra", complexity=N, parent=GOAL_ID, ...)`.
