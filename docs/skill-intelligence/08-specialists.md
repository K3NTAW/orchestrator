# Temporary specialists (S6)

A specialist exists for one spawn: the existing role plus the skill router's
selected ids and versions. Its deterministic name joins the role and sorted
short skill names. Composition never creates or persists a role (P35), changes
a model, or installs a dependency. The pool still owns executor/tier selection.

Context requirements are the union of the surviving skills' `requires_context`
source types. The context router raises those types to at least LONG; in shadow
this changes only routing telemetry. Active execute and review packet headers
carry `specialist: <name>`; scout and specification-review headers are unchanged.
Active specialist packets share goal EvidencePool facts, naming each consumed
fact by id. Review and security-review specialists resolve `inputs[0]` through
the bus to an execute task and exclude all `review_finding` locations beginning
with that task's review prefix. Missing inputs skip exclusion and are logged.
Fix-round rejecting comments remain in their separate context section.

Tools start with the catalog's minimal set, retaining every mandatory tool.
Declared tools may restore tools from the role's legacy allowlist. Bash expands
to that role's Bash patterns; Search means Grep and Glob; bus names map to bus
MCP ids; orchestrator names use the catalog's unprefixed keys. Script declarations
require `Bash(bash skills/*)`. A known but unavailable tool drops even a mandatory
worker skill with `tool_unavailable`; unknown names are logged as `unknown_tool`
and do not drop it. Planner composition retains its mandatory skills, with no
tools and reason `planner tools not catalogued`.

When both skill routing and tool disclosure are active, and the skill-routing
safety gate accepts active routing, Claude workers receive the specialist tool
set: the minimal set plus available aliased tools declared by selected skills,
with mandatory tools retained. Unknown or unavailable tools are never added.
The disclosure decision records the specialist allowlist and its added tools and
selected skill ids; run metadata identifies `specialist` as the allowlist source.
If either mode is shadow or off, or the safety gate refuses active skill routing,
the previous minimal (active disclosure) or legacy (shadow/off disclosure)
allowlist behavior is restored. Codex executors keep their CLI tools and record
`codex` as the source. A `needs_tool:` retry remains a one-shot respawn with the
full legacy Claude allowlist and the remaining budget and timeout.

## Conflicts

Skills conflict on incompatible declared final-result contracts, an explicit
`conflicts_with` id in either direction, a shared script path with differing
argument contracts, or contradictory commit rules in executor Stop conditions.
Supporting output artifacts do not claim the final result. Script contracts
come from script declarations and procedure invocations. Resolution prefers:

1. More matched trigger keywords.
2. The role's mandatory skill.
3. Lexically first skill id.

Ranked greedy selection keeps a mutually compatible set. The existing
`skill_selection` decision row records kept/dropped/rule and the conflict cause,
along with specialist name, tools added/unavailable/unknown and required context.
There is no new decision kind or permanent specialist registry.

## Examples using existing skills

- Authentication-impact scout: `scout+trace-callers`, optionally `test-gap` when
  its triggers also match; consumes source and graph evidence.
- Migration reviewer: `review+adversarial-review`; the migration spec and diff
  provide context, while earlier reviewers' verdict evidence stays excluded.
- Frontend executor: `execute+implement-spec`; scope and acceptance specialize
  the task without inventing a frontend role or changing its selected model.

Revert presentation by setting `[skills] mode = "shadow"`. Composition decisions
remain available for inspection, while specialist headers, skill procedures and
shared evidence stop being injected into worker packets.
