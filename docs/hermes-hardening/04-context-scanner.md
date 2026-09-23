# Deterministic context scanner

`orchestrator.context_scanner.scan(text, source_kind=...)` is a pure function:
no I/O, network, model, or Jev calls. It returns `verdict`, integer `score`, and
`findings` containing a pattern name, a one-based line, and an excerpt of at most
120 characters. One hit is one distinct (pattern, line) pair. Matching is case
insensitive; results are ordered by line and pattern name.

| Pattern | Weight | Signals |
| --- | --- | --- |
| override_instructions | 3 | Ignore/disregard prior instructions, role reassignment, new system prompt |
| authority_claim | 2 | Claimed system, operator, developer, or higher authority |
| credential_read | 2 | Read/print/cat dotenv, SSH/cloud credential directories, tokens, API keys, passwords |
| exfiltration | 2 | Network command with URL and variable, token or file; base64 piped to network |
| permission_change | 2 | World-writable chmod, sudoers modification, disabled safeguards, hooks or permissions |
| hidden_execution | 3 | Shell commands in HTML comments; JavaScript/data links or images |
| invisible_unicode | 3 | Zero-width characters, bidi controls, Unicode tag characters |
| hidden_html | 2 | Instruction verbs in HTML comments or display:none blocks |

For `readme`, `claude_md`, `agents_md`, `prompt`, `evidence`, and `other`, every
hit inside a backtick/tilde code fence or on a list/quote line loses two weight
points, with a minimum of zero. `skill` and `mcp_description` receive no reduction.
The score is the sum of effective weights. A retained weight-three hit blocks.
Credential-read and exfiltration hits with positive effective weights also block
when on the same or adjacent lines. Zero-weight documentation examples do not
activate that joint rule. Otherwise, score six blocks (four for skill/MCP),
score two or more is suspicious, and lower scores are safe. Thus a single fenced
weight-three example scores one; two such examples score two. A plain credential
read plus adjacent exfiltration scores four and blocks; separated by another line
it scores four and is suspicious for `other`. Repeated examples can accumulate
past the blocking threshold; context reduction is not an unconditional exemption.

`TRUST_CLASSES` has fixed authority: SYSTEM = 4, TRUSTED_REPO = 3, LOCAL_USER = 2,
EXTERNAL = 1, UNTRUSTED = 0. `may_override(lower, higher)` is false whenever the
first class has lower authority; equal or greater authority returns true. Unknown
classes and source kinds raise ValueError. Text claiming authority never changes
this ordering.

Evidence derives `trust_class` from provenance: repo → TRUSTED_REPO, memory →
LOCAL_USER, everything else → EXTERNAL. Blocked evidence is demoted to UNTRUSTED.
The legacy `trust` field is unchanged. Persisted `scan` contains only the verdict
and distinct pattern names under `patterns`, without excerpts. Old rows load with
provenance-derived trust and `scan: null`; unknown fields are ignored.

Quarantine inspection scans each UTF-8 text file once, SKILL.md first, excluding
its generated findings report and binary files. The report's `scan` maps file
names to full scanner results and `scan_overall` holds the worst verdict. Findings
map blocked to block and suspicious to warn; safe adds no findings. Existing
network/credential findings suppress duplicate scanner findings of the same
family on the same file/line. Inspection leaves lifecycle state unchanged.

Use `orchestrator scan PATH --kind agents_md` for a read-only file report; add
`--json` for structured output. The default kind is `other`.

This is a heuristic, not a semantic security boundary. It can miss paraphrases,
encoded payloads, unusual HTML/CSS and network syntax, and can flag benign prose.
Unicode format characters conservatively count even without a recognized verb;
invisible characters are removed for other pattern matching. Excerpts are source
text, so callers should treat them as untrusted data. Synthetic tests describe
attacks using words and reserved example domains, without credential values.
