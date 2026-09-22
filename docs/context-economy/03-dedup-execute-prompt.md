# Execute prompt contract deduplication

Measured with a representative execute task whose packet was 3,961 chars, using
`spawn.packet_run_meta`'s chars and the P1 estimate of `chars // 4`.

| prompt | chars | est_tokens |
| --- | ---: | ---: |
| before | 5,569 | 1,392 |
| after | 5,536 | 1,384 |

The packet remains the sole task contract. Objective/spec, every acceptance
criterion, and every write-scope path remain untrimmable; only the duplicate
template fields were removed. Fresh fix rounds use the same packet-only render,
while executor reply deltas are unchanged.

Revert path: `git revert <this-commit-sha>`.
