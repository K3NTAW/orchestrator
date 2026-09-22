# Skill telemetry

Stage 1 observes skill exposure and use without changing prompts, allowlists, symlinks, or routing.

Run `context` fields:

- `skills_exposed`: registry IDs visible to the worker.
- `skill_tokens_l0`: sum of exposed registry `est_tokens_l0` values.
- `skills_used`: registry IDs detected after the run.
- `skill_tokens_l2`: sum of used registry `est_tokens_l2` values.

The task `packet_meta` carries exposure and session data; the task pipeline carries `skills_used`.
The run row also stores Claude's `session_id` when the JSON CLI result supplies it. A
`skill_selection` decision records static candidates and exposure, followed by an outcome with use.

Claude exposure is every active builtin registry record, matching the shared `.claude/skills`
directory. Claude use is inferred from Jev gate rows for the task or session: reading a skill's
`SKILL.md`, or running a file below its `scripts/` directory, counts as use. Skill-tool calls are
not observable until a human adds `Skill` to the Jev-gate `PreToolUse` matcher in
`.claude/settings.json`. A future source could inspect the session transcript below
`CLAUDE_CONFIG_DIR/projects/`; transcripts are not parsed now.

Codex exposure is `executor/implement-spec`. Codex use is inferred from final-summary lines of
the form `Skill used: implement-spec` (a full registry ID is also accepted).

`orchestrator scorecard --skills` reports per-role/per-skill exposure, use rate, average token
estimates, overhead relative to known input tokens, and lineage-merged tokens per accepted task.
The economy scorecard embeds the same block.

Limits: Claude Code loads skill descriptions itself, and L0/L2 values are estimates rather than
provider token counts. Missing logs or registry state produce incomplete telemetry. Telemetry is
best-effort and never blocks a spawn. Reverting requires no state migration: remove the fields and
their reporting code; historical JSON remains harmless.
