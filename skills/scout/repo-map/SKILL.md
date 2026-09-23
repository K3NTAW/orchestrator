---
name: repo-map
description: Produce or refresh the repository map for memory/architecture.md.
roles: [scout]
task_classes: ["*"]
triggers: [architecture, layout, "where is"]
tools: [Bash, bus_post_result, "script:scripts/repo_map.sh", "script:../../planner/memory/scripts/graph.sh"]
requires_context: [source_chunk, graph_finding, architecture_note]
output: "scout JSON with an at-most-80-line repository map"
security: internal
repo: "*"
---
## Trigger
Architecture, layout, or where is requests.

## Objective
Map entry points, layers, conventions, and test layout.

## Procedure
Run `bash skills/scout/repo-map/scripts/repo_map.sh`. If `graphify-out/graph.json` exists, add `bash skills/planner/memory/scripts/graph.sh summary`. Do not read files wholesale.

## Tools
Bash scripts; bus_post_result.

## Evidence requirements
Cite `path:line`; graph evidence uses `src=path loc=Lnn`.

## Output contract
Return the scout JSON schema (findings with evidence, confidence, provenance ["repo"]), summarized in ≤80 lines.

## Stop conditions
Stop when the four requested map areas are covered.

## Failure/recovery
Read-only; identify missing graph data rather than inventing it. A human reviews merges.
