# Skill routing

Stage 3 computes a minimal skill set for every worker spawn while leaving the
actual, static skill exposure unchanged.

## Rules

Active registry skills are filtered by role and task class. Declared trigger
matches rank by the total number of words in all matched phrases, then by skill
ID. A one-word match is ambiguous and is logged but not selected. Mandatory
skills are selected first and do not count against `skills.max_selected`; the
cap applies only to additional firm trigger matches. History is reserved for
Stage 4 scorecard evidence.

| Worker role | Mandatory skills |
| --- | --- |
| execute / Codex execute | `executor/implement-spec` |
| review, spec_review | `reviewer/adversarial-review` |
| challenge | `reviewer/challenge` |
| planner | `planner/memory`, `planner/orchestrate`, `planner/resume`, `planner/write-spec` |
| scout | Triggered `repo-map`, `test-gap`, or `trace-callers` only |
| triage | Triggered `classify` or `compact-memory` only |

## Shadow evidence

`skills.mode = "shadow"` records candidates, matched triggers, task class,
mandatory skills, selected and rejected sets, ambiguity, and L0/L2 token
estimates in `skill_selection`. Run context carries the selected set and token
totals. Completion records any used-but-unselected skill as recovery. The skill
scorecard reports average set size, reduction, and recovery rate by role.

`active` currently behaves like shadow and emits one warning. Stage 5 will make
it control exposure. To revert, set `skills.mode = "off"`.
