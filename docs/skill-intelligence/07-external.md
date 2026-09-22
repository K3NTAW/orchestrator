# External skill quarantine

Skills are capability modules, not policy authorities.

The lifecycle is discover → quarantine → inspect → testing (Stage 5/10)
→ shadow → active. Nothing in discovery or inspection executes skill content,
installs packages, clones repositories, or calls a model reviewer.

```
orchestrator skills discover /path/to/checkout
orchestrator skills discover owner/repo@main
orchestrator skills quarantine external/owner-repo/name --reason 'review intake'
orchestrator skills inspect external/owner-repo/name
orchestrator skills check-upstream external/owner-repo/name
```

`import` aliases `discover`; it does not activate a skill. Local discovery scans
skills/, .claude/skills/, and .codex/skills/. GitHub discovery uses `gh api`
Contents requests pinned to the resolved commit. Each skill is limited to
50 files and 200 KB; directories also have traversal limits. Retrieval errors
are returned and discovery errors are recorded in skills/discovery-errors.json.

Records live in STATE/skills/discovered.json, separately from builtin sync.
Bodies live only in STATE/skills/quarantine/<id>/. Discovered, quarantined,
and testing records are excluded from routing. No router exists at this stage.
Local origins matching pool.toml `[skills] trusted_repos` owner/repo entries
receive repository provenance; all intake remains untrusted. Provenance includes
source repository, path, commit, retrieval time, license text, hash and modifications.
A checkout without a commit records null; local identity without an origin uses
its absolute path. No builtin metadata inference runs on external content.

| Check | Severity |
| --- | --- |
| Shell blocks and script files | warn |
| Network commands, URLs, sockets, package managers | warn |
| Absolute paths, home paths, .git | warn |
| Credential variable names, keychain, .env | block |
| MCP tools and server declarations | warn |
| Destructive shell, git and SQL operations | block |
| Injection phrases, comments, invisible characters, base64 blobs | block |
| Domains absent from `[skills] allowed_domains` | warn |

Checks are deterministic heuristics, not a proof of safety. Findings include IDs,
file and line references, excerpts, severity and the inspected content hash.
Quarantined → testing refuses missing or stale inspection. Block findings refuse
promotion unless a human supplies a reason beginning `override:`; history records
that reason and the block IDs. Later review stages handle warning judgments.
Builtin imports, symlinks, submodules and over-limit skills are refused.

Repeated discovery deduplicates by repository and source path. Unchanged content
updates only retrieval time; changed content adds version history and preserves
lifecycle state, invalidating inspection. `check-upstream` checks the recorded
branch/ref and creates a separate discovered candidate with added/removed line
counts; it never replaces the original local version.

To revert an intake, delete its quarantine folder, its discovered.json entry,
and its state.json entry (including any separate upstream candidate IDs).
