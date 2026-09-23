# Memory scorecard and evaluation

`orchestrator scorecard --memory [--days N] [--json]` reads decision-log rows whose kind is `retrieval` and joins them to task outcomes. The default window is seven days. Results contain one row per task and mode plus aggregates that state their mode explicitly.

Active rows count `selected` records and `tokens_tiered`. Shadow rows count `extra.legacy_ids` and `tokens_legacy`, so precision and utility compare the memory workers actually saw. Precision is used records divided by presented records. Utility per token is used records divided by memory tokens, multiplied by 1,000. Repeated retrieval means that the same record was presented to the same goal lineage more than once; irrelevant records were presented but have no downstream-use signal.

Downstream use matches memory files or components against paths the task actually changed, or matches record identifiers and title words in the result summary. Changed paths come from `gitutil.changed_paths` while a worktree exists and its diff is available. The fallback is the bus task row's `changed_files` written by merge, never a reconstructed git-log diff. Task scope is the last fallback. Every task row reports `changed_path_source` as `worktree`, `changed_files`, or `scope`.

First pass means a merged task has no task whose `constraints.fix_round_for` points to it. The report compares the first-pass rate for merged tasks with presented memory against those without it and reports fix-round counts on each memory task row.

`orchestrator memory-eval [--json]` runs five deterministic cases in an isolated temporary repository: a current decision in HOT, an older WARM search result, superseded provenance in COLD, exclusion of an irrelevant record, and preservation of every record through compaction. The result is also written to `.orchestrator/memory_eval.json`; a failing case makes the command exit nonzero.
