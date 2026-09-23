# Memory store

The memory store is a derived SQLite index at `.orchestrator/memory/index.sqlite`. Markdown remains the authoritative COLD archive: migration never edits it, `add()` writes only SQLite, and `memory rebuild` discards the database and recreates it solely from Markdown.

## Schema and indexing

`records` stores the stable 12-hex SHA-1 identifier of kind, date, and title plus kind, title, ISO date, repository, provenance, outcome, revert path, supersession, goal, tier, body, and a SHA-256 content hash. Components, files, tags, and source tasks are JSON arrays. Kinds are decision, gotcha, architecture, model_note, retrospective, strategy, skill_outcome, and reference. Migrated records default to warm; superseded records and model notes or retrospectives older than 30 days are cold.

When SQLite supports FTS5, an external-content `records_fts` table indexes title, body, tags, components, and files. Insert, update, and delete triggers keep it synchronized. Without FTS5, search remains available through parameterized `LIKE` expressions and returns a metadata warning. List filters use exact JSON membership.

## Migration

`decisions.md`, `gotchas.md`, and `model-notes.md` are split at dated headings or dated list entries. Metadata type labels select the kind, explicit goal labels select the goal, and every task identifier is retained in encounter order. File paths, component names, selected topic tags, outcomes, revert paths, and supersession are extracted from each complete block. Unknown task identifiers and unknown type labels are tolerated.

`architecture.md` is handled as a repository map: its header date applies to each level-two basename entry, which becomes a separate architecture record. Existing `orchestrator/<basename>` files are attached to those records.

On this repository at commit `1acfc3b30612`, migration produces 235 records: 83 decisions, 103 gotchas, 1 reference, 46 architecture records, and 2 model notes. Counts are emitted by `memory migrate` and should be refreshed if the Markdown archive changes.

## CLI

- `orchestrator memory migrate`
- `orchestrator memory rebuild`
- `orchestrator memory search <query> [--kind K] [--component C] [--file F] [--tag T] [--since YYYY-MM-DD] [--tier T] [--limit N] [--json]`
- `orchestrator memory show <id>`

Text search output is one tab-separated line per hit: identifier, date, kind, and title. JSON search output is an array of complete records.
