---
name: trace-callers
description: Find every caller or reader of a symbol, config key or table and rank by risk. Use when a task asks who calls, who reads, or what breaks if X changes.
---
# Trace-callers scout
Read-only; a human reviews merges. Run `bash skills/scout/trace-callers/scripts/trace.sh <symbol>`; never grep the repo by hand. Group hits by module; flag dynamic access (reflection, string keys) as low-confidence. Return the scout JSON schema; max 20 findings, each with path:line.
