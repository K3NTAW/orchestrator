# Active skill routing

`[skills] mode = "active"` presents compact trusted builtin skills directly in
execute and review packets. The skills section is the first packet-body section.
Selected skills normally use the registry's Level 2 rendering; weak ambiguous
matches appear as Level 0 entries under “available on request”.

The Level 2 payload is capped at 2,400 characters and counts toward the normal
4,800-character execute-packet budget. If necessary, the lowest-ranked
non-mandatory skill is demoted to Level 1 and then dropped. The decision row
records demotions and the packet/run context records presented Level 2 tokens.

Claude active launches use `--disable-slash-commands`, preventing static skill
descriptions from loading. Skill scripts remain callable through the existing
allowlist. Codex receives the same execute-packet section; its mandatory
implement-spec skill remains available through repository instructions too.

## Safety refusal

Active mode falls back to shadow for a role when either condition holds:

- fewer than 30 shadow or active selections exist for that role in the last
  seven days;
- the role's seven-day recovery rate exceeds `[skills] max_recovery` (default
  `0.10`).

The recovery rate is recent skill-selection outcomes with a non-empty
`skill_recovery` divided by recent selection rows. A recovery means the worker
read an unpresented skill file or invoked one of that skill's scripts. The
refusal emits one notification describing the evidence count and rate.

## Revert

Set `[skills] mode = "shadow"`. The next Claude spawn omits
`--disable-slash-commands`, restores static exposure, and records selections
without changing packet contents.
