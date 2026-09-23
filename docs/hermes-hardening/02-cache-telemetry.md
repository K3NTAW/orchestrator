# Cache telemetry

Cache telemetry uses the token buckets already normalized on each run row. `input_uncached_tokens` is provider-billed input that was not read from cache, `cache_read_tokens` is reused input, and `cache_write_tokens` is newly cached input. The hit ratio is cache reads divided by uncached input, cache reads, and cache writes; an empty denominator produces `0.0`.

Effective tokens compare providers in input-token equivalents:

`uncached + cache_read × provider_read_ratio + cache_write × provider_write_ratio + output × output_ratio`

The configured defaults are Claude read `0.1`, Claude write `1.25`, Codex read `0.25`, Codex write `1.0`, and output `5.0`.

Prefix identity is calculated only after the final prompt is rendered. The prefix is every rendered character before the task-specific packet header; the suffix starts at that header and includes the objective and all later packet sections. `prefix_sha` is the first 12 hexadecimal characters of the prefix's SHA-256 digest. The metadata also records prefix and suffix character counts and the packet's section names. Thus tasks sharing a role template, selected instruction modules, and disclosed tools can share an identity while their task packets differ.

## Seven-day observation, 2026-09-23

The report was run read-only against `/Users/k3ntaw/code/orchestrator/.orchestrator`, covering the real rows from 2026-09-17 through 2026-09-23. It found 1,089 rows, 8,466,379 uncached input tokens, 353,914,866 cache-read tokens, 13,127,192 cache-write tokens, a `0.9425` aggregate hit ratio, and 137,500,067.8 effective tokens.

By provider, Claude had 484 rows, a `0.9396` hit ratio, and 54,411,817.8 effective tokens; Codex had 134 rows, a `0.9465` hit ratio, and 51,540,900.0 effective tokens. Another 471 older or auxiliary rows had no provider identity.

All 1,089 rows predate prefix telemetry and therefore have an unknown prefix. The unknown-prefix share is 100%; distinct known prefixes are zero and reuse rate is not measurable until newly annotated rows arrive. No reuse baseline is inferred from these historical rows.

Use `orchestrator scorecard --cache --days 7`; add `--json` for machine-readable output and `--root` to select a repository or state directory.
