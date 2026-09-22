# Skill registry

The canonical registry is generated from `skills/<role>/<name>/SKILL.md` plus files under each skill's
`scripts/` and `references/` directories. It inventories skills without changing worker exposure or behavior.

## Record schema

Each record has `id`, `name`, `description`, `source`; 12-hex `version` and `content_hash`; `provenance`,
`trust`, `state`, and matching `promotion_state`; `roles`, `task_classes`, `triggers`, `tools`, and
`context_requirements`; `output_contract`; `est_tokens_l0`, `est_tokens_l1`, and `est_tokens_l2`;
`security_class`, `repo_scope`, `validation`; and `created_at`/`updated_at`.

The generated `.orchestrator/skills/registry.json` has `version`, `synced_at`, and a `skills` object keyed by
skill ID. Lifecycle overrides are separate in `.orchestrator/skills/state.json`, including reason, since, and
history. Builtins receive an explicit trusted/active baseline entry; provenance never implies lifecycle state.

## Lifecycle

`discovered → quarantined | testing | disabled`; `quarantined → testing | disabled`;
`testing → shadow | disabled | quarantined`; `shadow → active | testing | disabled`;
`active → demoted | disabled | shadow`; `demoted → shadow | testing | disabled`;
`disabled → discovered | testing`. Rollback restores the prior state and records that action.

Content changes append a version-bump history event. Trusted skills retain state; other skills return to testing.
Removed sources remain visible as disabled records.

## CLI

Use `orchestrator skills sync`, `list [--json]`, `show <id> [--json]`,
`transition <id> <state> --reason <text>`, and `rollback <id>`. Invalid transitions exit non-zero.

## Revert

Delete `.orchestrator/skills/registry.json` and `.orchestrator/skills/state.json`, then run
`orchestrator skills sync`. Nothing else changes.
