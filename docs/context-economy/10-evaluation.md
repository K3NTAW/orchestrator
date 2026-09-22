# Context economy evaluation

`orchestrator context-eval` creates isolated, deterministic repositories and compares unchanged packet text with feature modes off and in shadow. `--json` emits the measurements; `--root` retains fixture worktrees for inspection. The command writes only the final `.orchestrator/context_eval.json` to production state.

The seven fixtures are:

- `localized_fix`: at least 40% reduction; the scoped source remains FULL.
- `cross_cutting`: 10–40% reduction across several source files.
- `security`: security instructions and security-path evidence remain FULL.
- `test_failure`: failing output and named tests remain FULL.
- `documentation`: source chunks are summarized; none remains FULL.
- `review`: keeps diff, acceptance, and interfaces, without an implementation transcript.
- `repeated_fix_round`: at least 80% of prior evidence IDs are reused.

`orchestrator scorecard --economy` joins context routing, tool disclosure, handoff, and read-economy measurements by `(role, task class)`. It also shows promotion recommendations for the four shadow features and the time, commit, and pass status of the last context evaluation. Use `--json` for automation.

Promotion requires the configured minimum sample size, non-inferior first-pass/fix-round/gate/review quality, measurable accepted cost or token improvement, and a low context-recovery rate. A passing fixture suite supplies efficiency evidence only. Shadow packets are unchanged, so they cannot supply quality deltas and must remain in shadow until an active A/B measures them.

Revert paths:

- Context router: set `[context_router].mode = "off"`.
- Tool disclosure: set `[tool_disclosure].mode = "off"`.
- Conditional instructions: set `[instructions].mode = "off"`.
- Handoff routing: set `[handoff].mode = "off"`.
