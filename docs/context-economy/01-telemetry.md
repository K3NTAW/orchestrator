# Context economy telemetry

Every measured model dispatch records `packet_meta` and a derived `context` object in the run log. Packet text is
measured deterministically: estimated tokens are `characters // 4`; `presented_tokens` is the complete packet cost,
and every `_preamble` or named `## section` records characters, estimated tokens, and a full SHA-256 digest.

`candidate_tokens` describes the material available before a builder's size reduction: the execute body before its
4,800-character cap, or the review diff before `bounded_diff`. `candidate_known` is false for builders that cannot
measure that source. `instruction_tokens` is the rendered prompt estimate minus the packet estimate, so wrappers are
visible without changing any prompt bytes.

The context scorecard groups runs by role and parent goal. Per-goal amplification is total section tokens divided by
tokens for distinct section SHA-256 values; the same calculation is also reported by section. A section is repeated
when one digest occurs in at least two roles working the same goal. Lineage root is reported separately and does not
define the group. Runs without valid packet identity remain in the report as unmeasured rows.

When explicit packet metadata is absent, executor fallback measurement begins at a line starting `packet v` and ends
immediately before the first `\n\nRepair delta:\n`, or at the end of the text. Only headings inside that span become
sections. Text without a packet header, including a bare resume delta, is unmeasured. Explicit task metadata always
wins over fallback parsing.
