# Tool disclosure (P10/P11/P22)

Workers still receive the existing per-role Claude Code allowlists. This feature
measures their estimated schema cost and computes a smaller set in shadow; it
does not change `--allowedTools`.

## Level-0 catalog

`orchestrator.tool_catalog.CATALOG` contains a one-line purpose, category, and
estimated schema tokens for every offered worker tool and all 11 Planner MCP
tools. Claude built-ins use fixed estimates: Read 350, Grep 400, Glob 200, Edit
300, Write 200, and each Bash pattern group 500. Repository MCP costs are their
signature plus docstring characters divided by four. Codex is represented as a
zero-token gap because its CLI built-ins are not controlled here.

## Categories by task and role

| Role | Default categories |
| --- | --- |
| review | read, search, git, bus |
| scout | read, search, git, bus |
| triage | read, search, bus |
| challenge/spec review | read, search, git, bus |
| execute (all routing classes) | read, search, edit, shell, test, git, bus |

Task classification uses canonical `attribution.task_class`: explicit constraint
or inferred security/architectural/debugging/mechanical/unfamiliar. These classes
describe risk and difficulty, not command needs, so execution conservatively
retains shell and test categories for all classes, including explicit overrides.

Read and `bus_post_result` are mandatory for every Claude role. Git is mandatory
for review/scout, and the tests-green gate is mandatory for execute. A docs-only
scope drops optional test and shell tools; mandatory tools are never dropped.

## Shadow records and recovery

In `shadow` (and currently `active`) each dispatch records a `tool_disclosure`
decision: task class, role, offered/kept/dropped IDs, mandatory IDs, and disclosed
versus minimal tokens. Run context copies both token counts, and the context
scorecard reports per-role averages and minimal/disclosed ratio. `active` emits a
one-time warning and remains shadow.

`recovery_events()` scans existing run rows for permission-denial evidence. No
structured hidden-tool request signal is recorded today, so it normally returns
an empty event list with that limitation in its note.

P11 escalation is not implemented. An active worker needing a hidden optional
tool would post a bus event; the Planner would then re-spawn it with the wider
set. Revert by setting tool disclosure mode to `off`; allowlists remain intact.
