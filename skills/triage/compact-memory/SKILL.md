---
name: compact-memory
description: Compact .orchestrator/memory/decisions.md to ≤300 lines and dedupe gotchas.md, preserving every dated decision's outcome. Use monthly or when memory is over budget.
---
# Compact memory
A human reviews merges. Fold superseded decisions into their successor with a one-line "superseded YYYY-MM-DD". Never drop a gotcha whose fix is still current. Overwrite in place; post a 5-line diff summary via bus_post_result.
