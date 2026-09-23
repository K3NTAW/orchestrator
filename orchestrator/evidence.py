"""Canonical, content-addressed evidence for deterministic context selection.

Evidence content is raw in memory.  :class:`EvidencePool` always applies
``jev.redact`` before persistence so secrets are never written to its JSONL file.
``LOCAL_USER`` covers locally produced artifacts, including user memory and
observed worker outputs; it is not limited to user-authored memory.
"""
from dataclasses import asdict, dataclass, field, fields, replace
import hashlib
import json
import re
import time
from pathlib import Path

from . import STATE, jev, context_scanner


SOURCE_TYPES = (
    "source_chunk", "test_result", "scout_finding", "memory_entry",
    "graph_finding", "previous_result", "review_finding",
    "architecture_note", "decision", "external_doc", "worker_partial",
)
PROVENANCE = ("repo", "bus", "memory", "scout", "external", "worker_partial")


@dataclass(frozen=True)
class Evidence:
    id: str
    source_type: str
    location: str
    commit: str
    content_hash: str
    content: str
    summary_short: str
    summary_long: str
    relevance: dict
    provenance: str
    observed_at: float
    trust: str
    trust_class: str = ""
    scan: dict | None = None
    scope: list = field(default_factory=list)


def _trust_class(provenance):
    return {"repo": "TRUSTED_REPO", "memory": "LOCAL_USER",
            "worker_partial": "LOCAL_USER"}.get(provenance, "EXTERNAL")


def _scope(task):
    return (task or {}).get("scope") or (task or {}).get("write_scope") or []


def _location_path(location):
    return str(location).split(":", 1)[0]


def relevance_for(ev, task):
    """Compute relevance for *task*; stored relevance is only a per-task cache."""
    title = str((task or {}).get("title") or (task or {}).get("objective") or "")
    content = ev.content if isinstance(ev, Evidence) else str(ev)
    location = ev.location if isinstance(ev, Evidence) else ""
    lower = content.lower()
    title_terms = {word.lower() for word in re.findall(r"[A-Za-z0-9_]+", title) if len(word) >= 4}
    scope = [str(path) for path in _scope(task)]
    stems = {Path(path).stem.lower() for path in scope if Path(path).stem}
    return {
        "scope_match": _location_path(location) in scope,
        "title_terms": sum(term in lower for term in title_terms),
        "path_terms": sum(stem in lower for stem in stems),
        "recent_task": (task or {}).get("id") or None,
    }


def _collapse(value):
    return " ".join(str(value).split())


def make(source_type, location, content, *, commit="", provenance, task=None,
         scope=(), summary_long=None, observed_at=None, section=None):
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"invalid source_type: {source_type}")
    if provenance not in PROVENANCE:
        raise ValueError(f"invalid provenance: {provenance}")
    content = str(content)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    evidence_id = hashlib.sha256(
        f"{source_type}|{location}|{content_hash}".encode()
    ).hexdigest()[:12]
    meaningful = next((line.strip() for line in content.splitlines() if line.strip()), "")
    short = _collapse(meaningful)[:160]
    long = _collapse(content if summary_long is None else summary_long)[:600]
    relevance_task = task
    if relevance_task is None and scope:
        relevance_task = {"scope": list(scope)}
    result = context_scanner.scan(content, source_kind="evidence")
    trust_class = "UNTRUSTED" if result["verdict"] == "blocked" else _trust_class(provenance)
    scan_summary = dict(verdict=result["verdict"],
                        patterns=sorted({f["pattern"] for f in result["findings"]}))
    provisional = Evidence(
        evidence_id, source_type, str(location), str(commit), content_hash,
        content, short, long, {}, provenance,
        time.time() if observed_at is None else float(observed_at),
        "trusted" if provenance in ("repo", "memory") else "untrusted",
        trust_class, scan_summary, list(scope),
    )
    relevance = relevance_for(provisional, relevance_task)
    if section is not None:
        relevance["section"] = section
    return replace(provisional, relevance=relevance)


def fresh(ev, head_sha):
    return ev.source_type not in ("source_chunk", "test_result", "worker_partial") or ev.commit == head_sha


def cache_key(ev, task_class, role, level):
    value = f"{ev.content_hash}|{task_class}|{role}|{level}"
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class EvidencePool:
    """Goal-bound JSONL store; content is redacted with ``jev.redact`` on disk."""

    def __init__(self, goal):
        self.path = STATE / "evidence" / f"{goal}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    row.setdefault("trust_class", _trust_class(row.get("provenance")))
                    row.setdefault("scan", None)
                    row.setdefault("scope", [])
                    row = {key: value for key, value in row.items()
                           if key in {field.name for field in fields(Evidence)}}
                    self._items[row["id"]] = Evidence(**row)

    def add(self, ev):
        if ev.id in self._items:
            return self._items[ev.id]
        persisted = replace(ev, content=jev.redact(ev.content),
                            summary_short=jev.redact(ev.summary_short),
                            summary_long=jev.redact(ev.summary_long))
        self._items[ev.id] = persisted
        with self.path.open("a") as handle:
            handle.write(json.dumps(asdict(persisted), sort_keys=True) + "\n")
        return persisted

    def get(self, evidence_id):
        return self._items.get(evidence_id)

    def by_type(self, source_type):
        return [ev for ev in self._items.values() if ev.source_type == source_type]

    @staticmethod
    def fresh(ev, head_sha):
        return fresh(ev, head_sha)

    def stale_ids(self, head_sha):
        return [ev.id for ev in self._items.values() if not fresh(ev, head_sha)]
