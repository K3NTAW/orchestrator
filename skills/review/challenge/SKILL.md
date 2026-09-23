---
name: challenge
description: Try to refute another scout's low-confidence claim with repo evidence.
roles: [challenge]
task_classes: ["*"]
triggers: [refute, challenge]
tools: [Read, Search, bus_post_result]
requires_context: [scout_finding, source_chunk]
output: "challenge verdict JSON"
security: internal
repo: "*"
---
## Trigger
Challenge tasks or requests to refute a claim.

## Objective
Test a low-confidence claim for counter-evidence.

## Procedure
Read-only. Assume it is wrong; seek a contradicting call site, behavior test, or overriding config.

## Tools
Read, Search; bus_post_result.

## Evidence requirements
Use `path:line` evidence.

## Output contract
Return ONLY `{"verdict":"confirmed|refuted|uncertain","evidence":["path:line"],"note":""}`.

## Stop conditions
No counter-evidence within ≤10 tool calls means `confirmed`; mixed evidence means `uncertain`.

## Failure/recovery
Do not modify files. A human reviews merges.
