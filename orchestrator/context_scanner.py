"""Deterministic, heuristic inspection of untrusted context; no I/O or model calls."""
import re


TRUST_CLASSES = ("SYSTEM", "TRUSTED_REPO", "LOCAL_USER", "EXTERNAL", "UNTRUSTED")
SOURCE_KINDS = ("skill", "agents_md", "claude_md", "readme", "prompt",
                "mcp_description", "evidence", "other")
WEIGHTS = dict(override_instructions=3, authority_claim=2, credential_read=2,
               exfiltration=2, permission_change=2, hidden_execution=3,
               invisible_unicode=3, hidden_html=2)


def authority(cls):
    """Return the fixed authority rank; reject unknown classes."""
    return 4 - TRUST_CLASSES.index(cls)


def may_override(lower, higher):
    return authority(lower) >= authority(higher)


_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\U000e0000-\U000e007f]")
_VERB = r"\b(?:ignore|disregard|execute|run|read|print|cat|send|upload|fetch|curl|wget|sudo|chmod|disable|bypass|follow|obey|delete)\b"
_PATTERNS = {
    "override_instructions": r"\b(?:ignore|disregard)\s+(?:all\s+)?(?:the\s+)?(?:previous|prior)\s+(?:(?:system|developer)\s+)?(?:instructions?|prompts?)\b|\byou are now\b|\bnew system prompt\b",
    "authority_claim": r"\b(?:I am|you are|this is|acting as|from)\s+(?:now\s+)?(?:the\s+)?(?:system|operator|developer)\b|\b(?:higher|highest|superior)\s+(?:authority|priority)\b|\b(?:system|operator|developer)\s+(?:message|instruction|override)\b",
    "credential_read": r"\b(?:read|print|cat|extract|dump)\b[^\n]*(?:\.env\b|dotenv\b|\.ssh\b|\.aws\b|\.azure\b|\.config/gcloud\b|\b(?:tokens?|api[ _-]?keys?|passwords?|credentials?)\b)",
    "exfiltration": r"\b(?:curl|wget|nc|fetch)\b[^\n]*https?://[^\n]*(?:\$|@|\b(?:token|password|file|credentials?)\b)|\b(?:curl|wget|nc|fetch)\b[^\n]*(?:\$|@|\b(?:token|password|file|credentials?)\b)[^\n]*https?://|\bbase64\b[^\n]*\|[^\n]*\b(?:curl|wget|nc|fetch)\b",
    "permission_change": r"\bchmod\s+(?:-[a-z]+\s+)*(?:[0-7]{2,3}[2367]|[ao]\+w)\b|\b(?:edit|write|modify|append)\b[^\n]*\bsudoers\b|\b(?:disable|bypass|remove)\s+(?:all\s+|the\s+)?(?:safeguards?|hooks?|guardrails?|permissions?|security)\b",
    "hidden_execution": r"!?\[[^\n]*?\]\(\s*(?:javascript|data):|(?:href|src)\s*=\s*[\"']\s*(?:javascript|data):",
}
_PATTERNS = {name: re.compile(pattern, re.I) for name, pattern in _PATTERNS.items()}
_HIDDEN = re.compile(r"<!--.*?(?:-->|\Z)|<([a-z][\w:-]*)\b[^>]*\bstyle\s*=\s*[\"'][^\"']*display\s*:\s*none[^\"']*[\"'][^>]*>.*?(?:</\1\s*>|\Z)", re.I | re.S)
_SHELL = re.compile(r"\b(?:sh|bash|zsh|curl|wget|nc|sudo|chmod|cat|eval|exec)\b|\$\(|`[^`]+`", re.I)
_QUOTED = re.compile(r"^\s*(?:>|[-+*]\s|\d+[.)]\s|[\"'])")


def scan(text, *, source_kind):
    """Return line evidence and a score, counting each family once per line.

    Documentation contexts reduce weights by two. Zero-weight credential hits
    do not participate in the joint rule. Skill and MCP text has no reduction.
    """
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"invalid source_kind: {source_kind}")
    lines = text.splitlines()
    hits = set()
    # Remove invisible format characters for matching words they may interrupt.
    for number, line in enumerate(lines, 1):
        normalized = _INVISIBLE.sub("", line)
        for name, pattern in _PATTERNS.items():
            if pattern.search(normalized):
                hits.add((name, number))
        if _INVISIBLE.search(line):
            hits.add(("invisible_unicode", number))
    for match in _HIDDEN.finditer(text):
        start = text.count("\n", 0, match.start()) + 1
        for offset, line in enumerate(match.group().splitlines()):
            if re.search(_VERB, _INVISIBLE.sub("", line), re.I):
                hits.add(("hidden_html", start + offset))
            if match.group().startswith("<!--") and _SHELL.search(line):
                hits.add(("hidden_execution", start + offset))
    reduced = set()
    fence = None
    for number, line in enumerate(lines, 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        in_fence = fence is not None
        if marker:
            run, tail = marker.groups()
            if fence is None:
                fence = run
                in_fence = True
            elif run[0] == fence[0] and len(run) >= len(fence) and not tail.strip():
                fence = None
        if in_fence or _QUOTED.match(line):
            reduced.add(number)
    strict = source_kind in ("skill", "mcp_description")
    weighted = {(name, number): max(0, WEIGHTS[name] - (2 if number in reduced and not strict else 0))
                for name, number in hits}
    score = sum(weighted.values())
    reads = [n for (p, n), w in weighted.items() if p == "credential_read" and w]
    sends = [n for (p, n), w in weighted.items() if p == "exfiltration" and w]
    joint = any(abs(read - send) <= 1 for read in reads for send in sends)
    blocked = 3 in weighted.values() or joint or score >= (4 if strict else 6)
    verdict = "blocked" if blocked else "suspicious" if score >= 2 else "safe"
    findings = [dict(pattern=name, line=number, excerpt=lines[number - 1][:120])
                for name, number in sorted(hits, key=lambda hit: (hit[1], hit[0]))]
    return dict(verdict=verdict, findings=findings, score=score)
