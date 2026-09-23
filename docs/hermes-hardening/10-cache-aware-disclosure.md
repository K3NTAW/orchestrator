# Cache-aware disclosure

## 1. Context routing shadow

Every routed evidence item records its cacheability, effective cache-write cost, and cache-adjusted
level. Cacheability is evaluated in order: worker-partial provenance is dynamic; test results are
dynamic; source chunks pinned to the routing head are stable; everything else is semi. Effective
cost is the presented text's character count divided by four, multiplied by one plus the provider's
cache write ratio. Configured ratios override the telemetry defaults; a missing provider produces no
cost.

The only adjustment is `LONG` to `SHORT` when an item is dynamic, its effective cost exceeds
`cache_downgrade_tokens` (default 600), and its routing reason is the generic fallback:
`ambiguous`. This is the sole fallback reason assigned by `_choice` after all named rules have been checked.
All named-rule reasons are exempt: `in_scope_file`, `security_path`, `failing_output`, `acceptance`,
`skill_required_context`, `read_scope`, `section_items`, `prior_worker_overlap`, `unrelated`,
`unrelated_memory`, `stale`, and `dependency`. `HIDE` never changes. Shadow mode records the proposed
level without changing presented context. Active cache mode changes presentation only while the
effective context-router mode is also active.

Reproduce the aggregate with:

    orchestrator scorecard --context --cache-shadow

It reports the number and date range of `context_selection` rows and the share of evidence items whose
cache-adjusted level differs from the explicit pre-adjustment `presented_level` map. This also counts
adjustments applied in active mode. Older rows without that map are excluded from the share; when no
comparable items exist the share is `unknown`. Both reports use the same root. With no matching rows it prints
`no context_selection rows`.


## 2. Tool disclosure cache view (P9)

Added 2026-09-23. Both `spawn._shadow_tool_disclosure` (Claude roles) and
`bus.log_run` (Codex events) measure the same pure `tool_catalog.cache_view`.
The existing row's `deterministic` mapping gains `stable_catalog_chars`,
`dynamic_chars`, and `changed_since_previous`. The stable text is `level0` over
all static role IDs, including `CODEX_TOOLS` for Codex. It contains only names and
one-line purposes, so MCP signature measurements and task selection cannot change it.
The dynamic text is `level2` over the kept IDs.

Repository interface decision: `disclosed(role)` on this branch returns IDs, not
schema text. It remains unchanged. `level2` renders repository MCP signatures and
docstrings; provider-owned built-in schemas are unavailable here and are represented
by purpose lines explicitly identifying the worker harness as their schema source.
Character measurements describe that exact text, not an estimate of hidden provider
schemas. They do not establish provider cache savings.

`decision_log.last_row` searches from the end with a shared 200-line budget and a
2 MiB per-file byte bound. It considers only today and yesterday in Europe/Zurich,
matching `deterministic.role`, a different subject, and the presence of
`deterministic.kept`. The merged schedlog stores `runs/sched/decisions.jsonl`, so
its bounded tail is filtered by row timestamp; dated `decisions-YYYY-MM-DD.jsonl`
files are also supported within the same line budget. No process history is cached.
Older rows with kept IDs qualify. Escalations with reason `hidden_tool_requested`
are excluded. The changed flag compares sets, ignoring order. No qualifying row
in that bounded two-day window produces `None`, not an unchanged result.

Both new cache keys use `context_router.cache_mode(cfg, section)`. Missing means
`off`; invalid values fall back to off and mark the corresponding decision row
`invalid_config`. Section mode off disables the path. The pool starts with
`[tool_disclosure].cache_mode = "shadow"`. Revert by setting it to `off`.
Shadow adds measurements only. Active adds a `tools` section in
`spawn._packet_body`, with stable catalog lines before selected definitions.
The normal tool permission and escalation mechanisms remain authoritative.

Reproduce the measurement with:

    orchestrator scorecard --economy --disclosure-cache

Use `--root PATH`, `--days N` (default seven), or `--json` as needed. For each role,
the report takes the latest tool-disclosure row per task before computing `rows`,
`measured_rows`, `date_range` (ISO dates in Europe/Zurich),
`mean_stable_catalog_chars`, `mean_dynamic_chars`, `changed_share`, and `none_count`.
Only rows with both character fields contribute to the means. Changed share is
True / (True + False); missing/None flags are counted separately. An unmeasured
mean or share is null in JSON. Escalation rows never contribute.
When there are no qualifying rows it prints `no tool_disclosure rows in range`.
No production measurements are claimed here; the synthetic acceptance fixture
has three deduplicated rows, stable/dynamic means 20/30, changed share 0.5, and
one None flag.

## 3. Skill catalog cache block (P10)

`skill_router.catalog_block(role, registry)` renders every active skill eligible
for the role as sorted IDs and sorted triggers. Execute/Codex and review/security
role aliases share catalogs. Selection and task contents cannot change the block.

`spawn._prepare_skills` measures `catalog_chars` and `selected_chars` on every
routed selection, including shadow. Selected chars count the selected Level-2
skill bodies before presentation caps; existing presented-token telemetry still
reports the actual capped presentation. `_skill_routing` records both counts in
the existing `skill_selection` row's deterministic mapping.
Only `[skills].cache_mode = "active"` prepends the catalog inside the skills
section. Shadow preserves the existing section verbatim. Skill routing's existing
safety gate still governs selected-body presentation; cache activation can expose
the catalog while skill selection remains shadow. With skills mode off neither
selection nor catalog preparation runs. Invalid cache settings are logged and
fall back to off. The dated pool default is shadow; revert with cache_mode off.
