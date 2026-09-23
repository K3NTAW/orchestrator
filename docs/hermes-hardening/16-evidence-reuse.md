# Evidence reuse from interrupted workers

Cancelled workers and dead workers with dirty worktrees can leave useful, observable work behind. The daemon preserves those facts in the goal evidence pool as `worker_partial` evidence. The content is limited to changed files and diff statistics, commit subjects, test result lines, error tails, and inspected paths. Prompts and transcripts are never included.

Each item records the source task, worktree HEAD, and the task's persisted write scope. It is content-addressed, scanned, redacted on persistence, and becomes stale when its commit differs from the goal head. Preservation stamps `pipeline.partial_preserved_at`, making repeated reconciliation idempotent.

Routing compares the preserved write scope with the replacement task's write scope and packet-time read scope. Fresh overlaps receive high relevance; stale items are hidden. Since 2026-09-23, content is capped when created by `[context_router].partial_max_tokens`, defaulting to 800 tokens.

Shadow mode leaves packets unchanged and records the would-be evidence IDs and character count in the context-routing decision. Active mode adds applicable facts under `prior_worker`, after the ordinary evidence section, so a replacement can inspect or diff the preserved commit rather than repeat discovery.
