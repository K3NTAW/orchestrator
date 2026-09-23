# Evidence-driven lifecycle

`orchestrator skills promote --dry-run` prints recommendations and their evidence.
Without `--dry-run`, the CLI applies recommendations through registry transitions.
There is no daemon writer. Reasons and rollback history remain on each record.

Scores use the current content-hash version's timestamp window, inclusive at its
start and exclusive at the evaluation time. Runs without timestamps cannot count.
A content change starts a fresh window; old versions never lend their samples.
Packet headers are not parsed for versions (S10 v3).

- Testing → shadow: validation status `tested`, inspection below block, fresh.
- Shadow → active: at least 20 samples in both comparable cohorts (or the larger
  configured `promotion.min_samples`), valuable/neutral verdict, measurable
  reduction in accepted tokens, accepted cost, or fix rounds, no adverse cohort,
  no security findings, and successful validation.
- Active promotion also requires a passing `skill-eval` from the last seven days.
- Active → demoted: harmful/costly evidence, or at least 70% selected-but-unused
  among at least 20 completed selection decisions in this version window.
  A recorded specialist conflict with a more recently activated skill also demotes.
  Pending spawns and missing usage are not classified as unused.
- Demoted → shadow: a later version has sufficient favorable evidence and passes
  validation/inspection. It must still qualify separately for active promotion.

Dependency hashes use SHA-256 of whole-file bytes. `depends_on` accepts paths,
globs and tool identifiers. Builtin support scripts, references, and body module
references are included. Sync records changed paths and marks the skill stale;
`skills list` displays the flag. Missing files and new glob matches count as changes.

A stale active skill stays active for its first 24 hours unless validation fails.
Failure, or more than 24 hours without revalidation, recommends shadow.
`orchestrator skills revalidate <id>` runs the declared repository unittest files;
success clears staleness and records validation. Empty test sets cannot pass.
Validation never executes external draft scripts. Such scripts require an explicit
`override:` reason to leave quarantine, even if inspection only warns.

Use `orchestrator skills rollback <id>` to undo the last history event.
`skills transition <id> <state> --reason <reason>` remains the explicit manual
operator path; automatic recommendations never bypass their evidence gates.
No dependencies were added for this stage.
