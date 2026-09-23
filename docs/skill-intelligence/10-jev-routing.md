# Jev routing for ambiguous skills

Jev is consulted only for weak, ambiguous skill matches. Mandatory skills and
firm trigger matches remain deterministic and are never included in its input.

For each ambiguous skill, one batched request asks five typed probability
questions: relevance, reduced rework, duplication, worth relative to cost, and
whether specialist use is required. A skill qualifies when relevance is at
least 0.6, worth-cost is at least 0.5, and duplication is below 0.5. An
unavailable or invalid response selects no extra skill.

Each request contains one redacted task digest (title, the first 800 spec
characters, scope paths, task class, and role) and at most 400 characters of
the Level 1 rendering for each candidate. It never contains packet sections or
file contents. The `skills` boundary permits exactly `title`, `spec`, `scope`,
`task_class`, `role`, and `skills`, caps state at 6000 characters, and disallows
raw source.

At most `skills.jev_max_batch` candidates (default 8) are sent, ordered by the
router's strength/id ranking, in one request per spawn. Remaining candidates
are logged as `not_sent`.

Results are cached in `STATE/runs/jev/skills_cache.json`. The 16-character
SHA-256 key covers task id, title/spec/scope content, sorted skill versions, and
repository HEAD. `jev.skills.cache_ttl_s` defaults to 86400 and the newest 500
entries are retained.

`skills.jev_mode = "shadow"` records the verdict, source, latency, usage, and
batch size on the top-level `jev` field of `skill_selection` without changing
selection. `active` may add qualifying candidates within selection and
presentation caps. Set `skills.jev_mode = "off"` to revert immediately.
