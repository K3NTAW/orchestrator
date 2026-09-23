"""Structured, rebuildable index over the orchestrator Markdown memory archive."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import ROOT, bus

KINDS = {"decision", "gotcha", "architecture", "model_note", "retrospective",
         "strategy", "skill_outcome", "reference"}
LIST_FIELDS = ("components", "files", "tags", "source_tasks")
FIELDS = ("id", "kind", "title", "date", "repo", "components", "files", "tags",
          "provenance", "outcome", "revert_path", "superseded_by", "source_tasks",
          "source_goal", "tier", "body", "sha")
PATH_RE = re.compile(r"(?<![\w/])(?:orchestrator|tests)/[\w./-]+\.py\b|(?<![\w/])\.claude/[\w./-]+|(?<![\w/])\.orchestrator/[\w./-]+")
TASK_RE = re.compile(r"\bT-\d{4}\b")
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


@dataclass
class Record:
    id: str = ""
    kind: str = "decision"
    title: str = ""
    date: str = ""
    repo: str = ""
    components: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    provenance: str = "markdown"
    outcome: str = ""
    revert_path: str = ""
    superseded_by: str = ""
    source_tasks: list[str] = field(default_factory=list)
    source_goal: str = ""
    tier: str = "warm"
    body: str = ""
    sha: str = ""


def _db_path(root: Path) -> Path:
    return Path(root) / ".orchestrator" / "memory" / "index.sqlite"


def _fts5_available(connection=None) -> bool:
    own = connection is None
    db = connection or sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(value)")
        db.execute("DROP TABLE temp.fts5_probe")
        return True
    except sqlite3.Error:
        return False
    finally:
        if own:
            db.close()


def _schema(db: sqlite3.Connection, fts: bool) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, date TEXT NOT NULL,
        repo TEXT NOT NULL, components TEXT NOT NULL, files TEXT NOT NULL, tags TEXT NOT NULL,
        provenance TEXT NOT NULL, outcome TEXT NOT NULL, revert_path TEXT NOT NULL,
        superseded_by TEXT NOT NULL, source_tasks TEXT NOT NULL, source_goal TEXT NOT NULL,
        tier TEXT NOT NULL, body TEXT NOT NULL, sha TEXT NOT NULL)""")
    if not fts:
        return
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
        title, body, tags, components, files, content='records', content_rowid='rowid')""")
    db.executescript("""
        CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
          INSERT INTO records_fts(rowid,title,body,tags,components,files)
          VALUES (new.rowid,new.title,new.body,new.tags,new.components,new.files);
        END;
        CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
          INSERT INTO records_fts(records_fts,rowid,title,body,tags,components,files)
          VALUES ('delete',old.rowid,old.title,old.body,old.tags,old.components,old.files);
        END;
        CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
          INSERT INTO records_fts(records_fts,rowid,title,body,tags,components,files)
          VALUES ('delete',old.rowid,old.title,old.body,old.tags,old.components,old.files);
          INSERT INTO records_fts(rowid,title,body,tags,components,files)
          VALUES (new.rowid,new.title,new.body,new.tags,new.components,new.files);
        END;
    """)


def _open(root: Path):
    path = _db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA busy_timeout = 5000")
    db.execute("PRAGMA journal_mode = WAL")
    db.row_factory = sqlite3.Row
    fts = _fts5_available(db)
    _schema(db, fts)
    return db, fts


def _unique(items):
    return list(dict.fromkeys(item for item in items if item))


def _record_id(kind, when, title):
    return hashlib.sha1((kind + when + title).encode()).hexdigest()[:12]


def _kind(label, default):
    normalized = (label or "").strip().lower().replace("-", "_").replace(" ", "_")
    mapped = {"model": "model_note", "model_note": "model_note"}.get(normalized, normalized)
    return mapped if mapped in KINDS else default


def _goal_from_bus(task_ids, root):
    for task_id in task_ids:
        try:
            if Path(root).resolve() == Path(ROOT).resolve():
                task = bus.get(task_id)
            else:
                task = json.loads((Path(root) / ".orchestrator" / "tasks" / f"{task_id}.json").read_text())
        except (KeyError, FileNotFoundError):
            continue
        if str(task.get("title", "")).startswith("GOAL:"):
            return task_id
    return ""


def _make_record(kind, when, title, body, root, label=None):
    title = re.split(r"(?<=[.!?])\s+", title.strip(), maxsplit=1)[0][:160].strip(" #-")
    paths = _unique(match.rstrip(".,;:)") for match in PATH_RE.findall(body))
    components = _unique(Path(path).name.removesuffix(".py") for path in paths)
    goal_match = re.search(r"\bgoal:\s*(T-\d{4})", body, re.I)
    task_body = (body[:goal_match.start()] + body[goal_match.end():]) if goal_match else body
    task_ids = _unique(TASK_RE.findall(task_body))
    goal = goal_match.group(1).upper() if goal_match else _goal_from_bus(task_ids, root)
    revert = re.search(r"revert path\s*:\s*([^\n.!?]*(?:[.!?]|$))", body, re.I)
    superseded = re.search(r"superseded by\s+(T-\d{4})", body, re.I)
    outcome = re.search(r"(?im)^outcome:\s*(.*(?:\n(?!\s*(?:##|[-*]\s*\d{4}-\d{2}-\d{2})).*)*)", body)
    tag_words = [word for word in ("security", "gate", "daemon", "jev", "skills", "memory", "cache")
                 if re.search(rf"\b{word}\b", body, re.I)]
    tags = [kind, *tag_words]
    normalized = (label or "").strip().lower().replace("-", "_").replace(" ", "_")
    if label and normalized not in KINDS and normalized != "model":
        tags.append("type:" + label.strip().lower())
    cutoff = date.today() - timedelta(days=30)
    is_old = False
    try:
        is_old = date.fromisoformat(when) < cutoff
    except ValueError:
        pass
    tier = "cold" if superseded or (is_old and kind in ("retrospective", "model_note")) else "warm"
    sha = hashlib.sha256(body.encode()).hexdigest()
    return Record(_record_id(kind, when, title), kind, title, when, Path(root).name,
                  components, paths, _unique(tags), "markdown", outcome.group(1).strip() if outcome else "",
                  revert.group(1).strip() if revert else "", superseded.group(1) if superseded else "",
                  task_ids, goal, tier, body.strip(), sha)


def _generic_records(path, default, root):
    text = path.read_text()
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines)
              if DATE_RE.search(line) and (line.lstrip().startswith(("#", "-", "*")))]
    records = []
    for pos, start in enumerate(starts):
        block = "\n".join(lines[start: starts[pos + 1] if pos + 1 < len(starts) else len(lines)]).strip()
        first = lines[start].strip().lstrip("#-* ")
        when_match = DATE_RE.search(first)
        if not when_match:
            continue
        when = when_match.group(1)
        heading = first[when_match.end():].strip(" :-—")
        label_match = re.search(r"(?im)^type:\s*([^·\n]+)", block)
        label = label_match.group(1).strip() if label_match else None
        kind = _kind(label, default)
        title = heading or next((line.strip("- #") for line in block.splitlines()[1:] if line.strip()), kind)
        records.append(_make_record(kind, when, title, block, root, label))
    return records


def _architecture_records(path, root):
    text = path.read_text()
    when_match = DATE_RE.search(text.splitlines()[0] if text else "")
    if not when_match:
        return []
    when = when_match.group(1)
    matches = list(re.finditer(r"(?m)^##\s+([^\s—]+)\s*(?:—\s*(.*))?$", text))
    result = []
    for index, match in enumerate(matches):
        basename, description = match.group(1), (match.group(2) or "").strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start():end].strip()
        sentence = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0][:160]
        record = _make_record("architecture", when, f"{basename}: {sentence}".rstrip(), body, root)
        candidate = Path(root) / "orchestrator" / basename
        record.files = [f"orchestrator/{basename}"] if candidate.exists() else []
        record.components = [Path(basename).stem]
        record.sha = hashlib.sha256(body.encode()).hexdigest()
        result.append(record)
    return result


def add(record, root=ROOT):
    if isinstance(record, dict):
        record = Record(**record)
    if record.kind not in KINDS:
        raise ValueError(f"unknown memory kind: {record.kind}")
    if not record.id:
        record.id = _record_id(record.kind, record.date, record.title)
    if not record.sha:
        record.sha = hashlib.sha256(record.body.encode()).hexdigest()
    values = asdict(record)
    for name in LIST_FIELDS:
        values[name] = json.dumps(values[name])
    db, _ = _open(Path(root))
    try:
        columns = ",".join(FIELDS)
        placeholders = ",".join("?" for _ in FIELDS)
        updates = ",".join(f"{name}=excluded.{name}" for name in FIELDS if name != "id")
        db.execute(f"INSERT INTO records ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}",
                   [values[name] for name in FIELDS])
        db.commit()
    finally:
        db.close()
    return record


def migrate(root=ROOT):
    root = Path(root)
    memory = root / ".orchestrator" / "memory"
    specs = (("decisions.md", "decision"), ("gotchas.md", "gotcha"), ("model-notes.md", "model_note"))
    records = []
    for filename, default in specs:
        path = memory / filename
        if path.exists():
            records.extend(_generic_records(path, default, root))
    architecture = memory / "architecture.md"
    if architecture.exists():
        records.extend(_architecture_records(architecture, root))
    for record in records:
        add(record, root)
    counts = {}
    for record in records:
        counts[record.kind] = counts.get(record.kind, 0) + 1
    return {"records": len(records), "counts": counts, "ids": [r.id for r in records]}


def rebuild(root=ROOT):
    path = _db_path(Path(root))
    if path.exists():
        db = sqlite3.connect(path)
        try:
            db.executescript("DROP TABLE IF EXISTS records_fts; DROP TABLE IF EXISTS records;")
            db.commit()
        finally:
            db.close()
    result = migrate(root)
    db, fts = _open(Path(root))
    try:
        if fts:
            db.execute("INSERT INTO records_fts(records_fts) VALUES ('rebuild')")
            db.commit()
    finally:
        db.close()
    return result


def _decode(row):
    result = dict(row)
    result.pop("rank", None)
    for name in LIST_FIELDS:
        result[name] = json.loads(result[name])
    return result


def search(query, *, kind=None, component=None, file=None, task_class=None, model=None,
           date_from=None, date_to=None, tag=None, tier=None, limit=20, root=ROOT):
    db, fts = _open(Path(root))
    warnings = []
    params = []
    if query and fts:
        sql = "SELECT records.*, bm25(records_fts) AS rank FROM records_fts JOIN records ON records.rowid=records_fts.rowid WHERE records_fts MATCH ?"
        params.append(query)
    else:
        sql = "SELECT records.*, 0 AS rank FROM records WHERE 1=1"
        if query:
            warnings.append("FTS5 unavailable; using LIKE search")
            sql += " AND (title LIKE ? OR body LIKE ? OR tags LIKE ? OR components LIKE ? OR files LIKE ?)"
            pattern = f"%{query}%"
            params.extend([pattern] * 5)
    scalar = (("kind", "=", kind), ("date", ">=", date_from), ("date", "<=", date_to),
              ("tier", "=", tier))
    for column, operator, value in scalar:
        if value is not None:
            sql += f" AND records.{column} {operator} ?"
            params.append(value)
    for column, value in (("components", component), ("files", file), ("tags", tag),
                          ("tags", task_class), ("tags", model)):
        if value is not None:
            sql += f" AND EXISTS (SELECT 1 FROM json_each(records.{column}) WHERE json_each.value = ?)"
            params.append(value)
    sql += " ORDER BY rank, records.date DESC LIMIT ?"
    params.append(max(0, int(limit)))
    try:
        rows = [_decode(row) for row in db.execute(sql, params)]
    finally:
        db.close()
    return {"records": rows, "metadata": {"fts5": fts, "warnings": warnings}}


def all_records(root=ROOT, tier=None):
    """Return the complete deterministic record set, optionally restricted by tier."""
    db, _ = _open(Path(root))
    try:
        if tier is None:
            rows = db.execute("SELECT * FROM records ORDER BY date DESC, id").fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM records WHERE tier = ? ORDER BY date DESC, id", (tier,)
            ).fetchall()
        return [_decode(row) for row in rows]
    finally:
        db.close()


def set_tiers(mapping, root=ROOT):
    """Set record tiers in one transaction; unknown record ids are ignored."""
    db, _ = _open(Path(root))
    try:
        db.executemany("UPDATE records SET tier = ? WHERE id = ?",
                       [(tier, record_id) for record_id, tier in mapping.items()])
        db.commit()
    finally:
        db.close()


def get(record_id, root=ROOT):
    db, _ = _open(Path(root))
    try:
        row = db.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        return _decode(row) if row else None
    finally:
        db.close()


def add_tag(record_id, tag, root=ROOT):
    """Add one tag to an existing record, preserving every other field."""
    record = get(record_id, root)
    if record is None:
        return None
    record["tags"] = _unique([*record.get("tags", []), tag])
    return add(record, root)
