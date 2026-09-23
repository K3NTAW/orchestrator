# P36 skill evaluation

Run `uv run orchestrator skill-eval` before considering active promotion.
The CLI prints a group/check table, exits 1 on any failure, and persists the
last result to `STATE/skill_eval.json`, including UTC run time and suite status.
A missing, failed, future-dated, or older-than-seven-days result blocks promotion.

The deterministic fixtures exercise production functions with temporary data:

| Group | Checks |
| --- | --- |
| Routing | Irrelevant excluded, obvious selected, fake Jev ambiguity, minimal set, unavailable tools |
| Lifecycle | Quarantine storage/exclusion, testing isolation, shadow exposure/packet bytes, evidence gates, demotion, rollback |
| Security | Hidden instructions, script override, draft redaction, allowed domains |
| Compaction | P6 headings, tokens versus prior revision, output contract |
| Specialists | Composition, unrelated exclusion, conflicts, required tools/context |
| Scorecard | Usage attribution, model/workflow grouping, marginal value, insufficient evidence |
| Learned | Repeated behavior, one-off rejection, quarantine, evaluation before activation |

No model or remote discovery calls are allowed. Jev decisions are supplied by a
fake, and external imports use local fixture repositories. Fixture state is
isolated; it never switches the process's `ORCH_ROOT` or modifies builtin skills.
The compaction check reads the prior Git revision; a checkout must retain it.
Failures are persisted as failures, never replaced with a passing placeholder.

`scorecard --skills` and `scorecard --economy` include skill reuse, tokens per
accepted lineage, overhead ratio, utility, and recovery rate. Reuse means all
selected skills were used, among spawns with a nonempty selection. Utility is
marginal first-pass quality delta per 1,000 skill tokens; insufficient evidence
is unknown. Missing measurements remain unknown instead of becoming zero.

To revert an applied lifecycle recommendation, use `skills rollback <id>`.
After a dependency change, use `skills revalidate <id>` and rerun this suite.
A passing suite alone never supplies promotion samples or efficiency evidence.
