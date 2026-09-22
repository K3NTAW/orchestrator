---
name: trace-callers
description: Find every caller or reader of a symbol, config key, or table and rank by risk.
roles: [scout]
task_classes: ["*"]
triggers: ["who calls", callers, readers, "what breaks", impact]
tools: [Bash, bus_post_result, "script:scripts/trace.sh"]
requires_context: [source_chunk, graph_finding]
output: "scout JSON, at most 20 path:line findings"
security: internal
repo: "*"
---
## Trigger
Who calls, callers, readers, what breaks, or impact requests.

## Objective
Find every caller or reader and rank risk.

## Procedure
Run `bash skills/scout/trace-callers/scripts/trace.sh <symbol>`; never grep the repo by hand. Group hits by module; mark dynamic access (reflection, string keys) low-confidence.

## Tools
Bash script; bus_post_result.

## Evidence requirements
Each finding has `path:line`.

## Output contract
Return the scout JSON schema; max 20 findings.

## Stop conditions
Stop after all script hits are grouped and ranked.

## Failure/recovery
Read-only; report incomplete/dynamic evidence as low-confidence. A human reviews merges.
