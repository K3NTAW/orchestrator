# Conditional instructions

Role prompts remain the legacy source of truth. The base is the legacy prompt with
known module text removed; selected modules are appended beneath one conditional
instructions heading. Fundamental scope, clean-worktree, commit-message, and gate
reporting rules remain in the base.

| Module | Selection rule |
| --- | --- |
| `python-unittest` | Execute work with a Python path |
| `tool-budget` | Execute role |
| `review-security` | Security-review role or a review packet security signal |
| `docs-task` | Every scope path is Markdown |
| `migrations` | A scope path contains `migrations` or `alembic` |

Rules run in table order and return selected modules, mandatory modules, and
reasons. Security review is mandatory for the security-review role. Semantic-only
security triggers are available at review packet build time, not in task fields,
so other callers must pass the already-computed signal.

In shadow mode rendering returns the legacy bytes unchanged. It also records an
`instruction_loading` decision with candidates, mandatory and selected modules,
reasons, legacy and modular token estimates, and their delta. Run context copies
the modular estimate so the scorecard reports per-role legacy average, modular
average, and modular-to-legacy ratio.

To revert, set the `conditional_instructions` promotion mode to `off`. The legacy
prompt files are untouched.
