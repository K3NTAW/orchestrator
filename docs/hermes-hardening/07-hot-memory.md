# HOT memory

The HOT view is a generated, bounded projection of the structured memory store. Run `orchestrator memory hot`
to print the view and its token, pinned, dropped, and over-budget report. Use `--json` for structured output.
`orchestrator memory compact` rebuilds `.orchestrator/memory/HOT.md` and reports pinned and dropped counts.

The `[memory]` settings in `.orchestrator/pool.toml` are the source of truth for the token budget, recent window,
and never-compact classes. Records tied to queued, held, or running tasks are also pinned. Pinned records are never
dropped, so the report can be over budget. Rebuilding the memory index restores warm/cold tiers; always run memory
compact afterwards to restore hot tier marks.

Repository migration and compaction on 2026-09-23 produced: tokens=2999, pinned=4, dropped=233.

## Packet wiring and promotion

Added 2026-09-23. `[memory].mode` defaults to `shadow`; `off` presents only the legacy
notes/bus gotchas and worktree decisions. Shadow presents those same sections unchanged
and records a `decision_log` row of kind `retrieval`. Active substitutes tiered memory in
those existing sections. Revert by setting mode back to off.

Packet builders read the existing canonical HOT.md without rebuilding it. `hot_fresh` on
both the retrieval row and packet metadata compares HOT.md and index.sqlite mtimes:
HOT newer than or equal to the index is fresh; a missing file is stale. Before dispatch,
the daemon rebuilds stale or missing HOT at most once per tick. `memory_hot.build` holds
a module-level threading lock. SQLite connections use WAL and a 5,000 ms busy timeout.

The packet query uses title words and scope basenames without extensions, quoted as FTS
terms joined with OR. Exactly three WARM searches run, one each for gotcha, decision and
architecture, with limit 8 and no exact-match file filter. HOT records come first, followed
by WARM hits in search order, deduplicated by record id. Candidate scores are 1 for HOT
and reciprocal result rank within each WARM kind. Both sources use the HOT one-line format;
architecture and decision lines go in decisions, gotchas in gotchas. The shared
`packet_hot_tokens` allowance defaults to 1,200 (rounded-up chars/4, including line endings).
Whole lines that do not fit are skipped. The existing 4,800-character packet trim still
applies afterward; there is no additional memory cap.

Retrieval rows carry candidates (id, tier, score), selected ids, subject task id and mode.
Their `extra` mapping contains legacy_ids, tokens_legacy, tokens_tiered and hot_fresh.
Token counts describe memory section lines after packet trimming, excluding empty-section
markers and headings. `packet_meta.memory_ids` identifies memory actually presented, and
`memory_mode` records the configured mode. Retrieval/logging failures warn without aborting
packet construction. Shadow comparison logging never changes the legacy body.

The promotion feature `memory_tiers` targets `memory.mode`, defaults to shadow, and uses
`retrieval` evidence. It follows the global CRITERIA, including min_samples (20 by default),
quality thresholds and measured token improvement. The collector sums both token counts
and counts retrieval rows. Fix-round delta is the mean count of bus tasks whose
`constraints.fix_round_for` points to each active task, minus the corresponding shadow mean.
Repeated retrievals count a task once in its latest observed mode. The delta remains None
until both cohorts contain at least five distinct tasks; promotion stays put while it is
unmeasured. There is no separate per-feature sample threshold or seven-day window.

### Repository shadow comparison

Measured 2026-09-23 against the current repository archive and real bus task contracts,
with memory in shadow, the 1,200-token allowance, and context routing disabled to isolate
memory selection. The SQLite snapshot was opened immutable/read-only and decision writes
were intercepted. These are replay measurements, not worker outcome evidence.

| Task | tokens_legacy | tokens_tiered |
| --- | ---: | ---: |
| T-0648 (CLI scheduling/strategy/promotion wiring) | 502 | 236 |
| T-0553 (empty-path graph freshness fix) | 158 | 112 |
| T-0329 (repomap harness import fix round) | 409 | 236 |

Total: 1,069 versus 584 presented memory tokens (45.4% reduction). HOT was fresh in all
three replays. Larger contracts T-1100, T-1110 and T-1120 each measured 0 versus 0 because
the existing body trim removed both memory sections. No fix-round improvement is inferred
from these replays; the five-tasks-per-side outcome requirement still applies.
