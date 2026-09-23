# Stable prompt prefixes

On 2026-09-23, the execute, review, scout, and spec-review role rules moved ahead of the packet boundary. Wording and rule order did not change. The packet body remains task-specific, so rearranging sections within it cannot lengthen the prefix measured before its header.

## Fixture measurement

The measurement uses `spawn.render` with the shared synthetic widget task from `tests/test_spawn.py`. Before the move, every template began at the packet boundary and therefore had a zero-character prefix. After the move:

| Role | Before | After |
| --- | ---: | ---: |
| execute | 0 | 1,574 |
| review | 0 | 710 |
| scout | 0 | 648 |
| spec-review | 0 | 686 |

These are the off-mode results and cover template text only. With active conditional instruction composition on the same fixture, execute measures 725 characters while review, scout, and spec-review remain 710, 648, and 686. The active values can change with module selection as described below.

## Remaining launch differences

| Prefix input | Per-role or per-run difference | Cache impact | Disposition |
| --- | --- | --- | --- |
| MCP configuration | A role-specific `.mcp.<role>.json` is used when present; otherwise workers share `.mcp.worker.json`. | Different disclosed MCP schemas prevent cross-role reuse. | Unavoidable when a role needs a distinct server set; the shared fallback is retained elsewhere. |
| Allowed tools | `spawn.py` supplies a different `--allowedTools` list for execute, review, scout, and spec-review. | Tool definitions differ by role and split prefixes. | Unavoidable least-privilege boundary. |
| Disallowed tools | Non-execute roles add Edit, Write, and NotebookEdit to `--disallowedTools`; execute does not. | The launch instruction surface differs from execute. | Unavoidable enforcement of read-only roles. |
| Conditional instruction modules | In active mode, selected rule modules are composed according to task and role signals. Shadow mode preserves the legacy prompt. | Different selected modules create multiple prefixes within a role. | Intentional and avoidable only by disabling active conditional loading; telemetry must expose the tradeoff. |
| Tool disclosure levels | Active disclosure can reduce the role allowlist according to selected skills; off and shadow retain the static role list. Skill presentation can also vary by selection level. | Different tool schemas or disclosed skill text create multiple prefixes within a role. | Intentional adaptive behavior; shadow is byte-identical and only records decisions. |

`scorecard --cache` now prints a focused per-role table with the existing `distinct_prefix_sha`, `runs`, and `reuse_rate` values. No run-row field or bus schema changed. Over time, repeated runs with the same role prefix raise `reuse_rate`; unavoidable role boundaries remain visible as separate rows.
