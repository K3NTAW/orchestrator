---
name: repo-map
description: Produce or refresh the repo map (tree + symbols) for memory/architecture.md. Use when a scout task asks for architecture, module layout, or where something lives.
---
# Repo-map scout
Read-only; you work for the Planner; a human reviews merges. Do not read files wholesale.
1. `bash skills/scout/repo-map/scripts/repo_map.sh` prints the tree and top-level symbols.
2. Summarize into ≤80 lines: entry points, layers, conventions, test layout. Cite path:line.
3. Return the scout JSON schema (findings with evidence, confidence, provenance ["repo"]).
