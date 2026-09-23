# HOT memory

The HOT view is a generated, bounded projection of the structured memory store. Run `orchestrator memory hot`
to print the view and its token, pinned, dropped, and over-budget report. Use `--json` for structured output.
`orchestrator memory compact` rebuilds `.orchestrator/memory/HOT.md` and reports pinned and dropped counts.

The `[memory]` settings in `.orchestrator/pool.toml` are the source of truth for the token budget, recent window,
and never-compact classes. Records tied to queued, held, or running tasks are also pinned. Pinned records are never
dropped, so the report can be over budget. Rebuilding the memory index restores warm/cold tiers; always run memory
compact afterwards to restore hot tier marks.

Repository migration and compaction on 2026-09-23 produced: tokens=2999, pinned=4, dropped=233.
