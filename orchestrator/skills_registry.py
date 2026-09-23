"""Canonical inventory and lifecycle state for repository skills."""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import STATE, attribution


REPO = Path(__file__).resolve().parents[1]
STATES = ("discovered", "quarantined", "testing", "shadow", "active", "demoted", "disabled")
ALLOWED_TRANSITIONS = {
    "discovered": {"quarantined", "testing", "disabled"},
    "quarantined": {"testing", "disabled"},
    "testing": {"shadow", "disabled", "quarantined"},
    "shadow": {"active", "testing", "disabled"},
    "active": {"demoted", "disabled", "shadow"},
    "demoted": {"shadow", "testing", "disabled"},
    "disabled": {"discovered", "testing"},
}
BASELINE_REASON = "builtin baseline 2026-09-22"


@dataclass
class SkillRecord:
    id: str
    version: str
    content_hash: str
    name: str
    description: str
    provenance: str
    trust: str
    state: str
    roles: list[str]
    task_classes: list[str]
    triggers: list[str]
    tools: list[str]
    context_requirements: list[str]
    output_contract: str
    est_tokens_l0: int
    est_tokens_l1: int
    est_tokens_l2: int
    security_class: str
    repo_scope: str
    validation: dict[str, Any]
    promotion_state: str
    created_at: str
    updated_at: str
    source: str
    provenance_info: dict[str, Any] | None = None
    conflicts_with: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    dependency_hashes: dict[str, str] = field(default_factory=dict)
    stale: bool = False
    stale_since: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(root: Path) -> tuple[Path, Path]:
    directory = Path(root) / "skills"
    return directory / "registry.json", directory / "state.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value: Any) -> None:
    if path.name == "discovered.json":
        _redact_drafts(value, path.parent / "quarantine")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("["):
        try:
            parsed = ast.literal_eval(value)
            return parsed if isinstance(parsed, list) else value
        except (ValueError, SyntaxError):
            return [part.strip().strip("'\"") for part in value[1:-1].split(",") if part.strip()]
    return value.strip("'\"")


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    data: dict[str, Any] = {}
    current = None
    for line in text[4:end].splitlines():
        item = re.match(r"^\s*-\s+(.+)$", line)
        if item and current:
            if not isinstance(data[current], list):
                data[current] = []
            data[current].append(_scalar(item.group(1)))
            continue
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if match:
            current = match.group(1)
            data[current] = _scalar(match.group(2))
    return data, text[end + 5:]


def _list(value: Any, default: list[str] | None = None) -> list[str]:
    if value in (None, ""):
        return list(default or [])
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _content_hash(skill_dir: Path, skill_file: Path) -> str:
    digest = hashlib.sha256()
    digest.update(skill_file.read_bytes())
    for folder in ("scripts", "references"):
        base = skill_dir / folder
        if base.exists():
            for path in sorted(item for item in base.rglob("*") if item.is_file()):
                digest.update(path.relative_to(skill_dir).as_posix().encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _triggers(description: str) -> list[str]:
    match = re.search(r"\bUse (?:when|for)\s+(.+?)(?:[.;]|$)", description, re.I)
    if not match:
        return []
    words = re.findall(r"[a-z0-9][a-z0-9_-]+", match.group(1).lower())
    return list(dict.fromkeys(word for word in words if len(word) >= 4))


def _output_contract(meta: dict[str, Any], body: str) -> str:
    if meta.get("output"):
        value = meta["output"]
        return (", ".join(value) if isinstance(value, list) else str(value))[:300]
    match = re.search(r"(?:Finish with|\bOutput\b)\s*:?[ \t]*(.+)", body, re.I)
    return re.sub(r"\s+", " ", match.group(1)).strip()[:300] if match else ""


def _token_estimate(text: str) -> int:
    """Estimate the disclosed procedural payload; metadata sections are indexed separately."""
    sections = _sections(text)
    if sections:
        text = "\n".join(sections[name].split("\n", 1)[-1]
                         for name in ("Procedure", "Output contract"))
    return len(text) // 4


def _git_dates(path: Path) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--format=%aI", "--", str(path)], cwd=REPO,
            capture_output=True, text=True, check=False,
        )
        dates = [line for line in result.stdout.splitlines() if line]
        if dates:
            return dates[-1], dates[0]
    except OSError:
        pass
    stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return stamp, stamp


def _roles(role: str, explicit: Any) -> list[str]:
    if explicit not in (None, ""):
        return _list(explicit)
    return [role, "spec_review", "challenge"] if role == "review" else [role]


def _tests_for(skill_dir: Path, tests_dir: Path) -> list[str]:
    needle = skill_dir.relative_to(skill_dir.parents[2]).as_posix()
    found = []
    if tests_dir.exists():
        for test in sorted(tests_dir.glob("test_*.py")):
            try:
                if needle in test.read_text():
                    found.append(test.relative_to(tests_dir.parent).as_posix())
            except (OSError, UnicodeDecodeError):
                pass
    return found


def _state_document(root: Path) -> dict[str, Any]:
    _, state_path = _paths(root)
    value = _read_json(state_path, {})
    return value if isinstance(value, dict) else {}


def state(skill_id: str, root: Path = STATE) -> dict[str, Any]:
    """Return explicit lifecycle state; provenance is deliberately not consulted."""
    entry = _state_document(Path(root)).get(skill_id)
    if not entry:
        raise KeyError(skill_id)
    return entry


def sync(root: Path = STATE, skills_dir: Path = REPO / "skills") -> dict[str, Any]:
    root, skills_dir = Path(root), Path(skills_dir)
    registry_path, state_path = _paths(root)
    old_registry = _read_json(registry_path, {"skills": {}})
    old_records = old_registry.get("skills", {}) if isinstance(old_registry, dict) else {}
    states = _state_document(root)
    records: dict[str, dict[str, Any]] = {}
    now = _now()

    for skill_file in sorted(skills_dir.glob("*/*/SKILL.md")):
        role, directory_name = skill_file.parent.parent.name, skill_file.parent.name
        skill_id = f"{role}/{directory_name}"
        text = skill_file.read_text()
        meta, body = _frontmatter(text)
        version = _content_hash(skill_file.parent, skill_file)
        entry = states.get(skill_id)
        if entry is None:
            entry = {"state": "active", "trust": "trusted", "since": now,
                     "reason": BASELINE_REASON, "history": []}
            states[skill_id] = entry
        previous = old_records.get(skill_id, {})
        old_version = previous.get("version")
        versions = entry.setdefault("versions", [])
        if not versions:
            versions.append({"content_hash": old_version or version, "at": entry.get("since", now)})
        if versions[-1]["content_hash"] != version:
            versions.append({"content_hash": version, "at": now})
        if old_version and old_version != version:
            history = entry.setdefault("history", [])
            history.append({"at": now, "from": entry["state"], "to": entry["state"],
                            "reason": f"content changed: {old_version}→{version}",
                            "old_version": old_version, "version": version})
            entry["since"] = now
            entry["reason"] = history[-1]["reason"]
            if entry.get("trust") != "trusted":
                entry["state"] = "testing"
        scripts = sorted(
            "script:" + path.relative_to(skill_file.parent).as_posix()
            for path in (skill_file.parent / "scripts").rglob("*")
            if path.is_file()
        ) if (skill_file.parent / "scripts").exists() else []
        tools = _list(meta.get("allowed-tools", meta.get("tools"))) + scripts
        triggers = _list(meta.get("triggers")) or _triggers(str(meta.get("description", "")))
        objective = re.sub(r"^#+\s+", "", body.strip().splitlines()[0]) if body.strip() else ""
        output = _output_contract(meta, body)
        tests = _tests_for(skill_file.parent, REPO / "tests")
        created, updated = _git_dates(skill_file)
        dependencies = _list(meta.get("depends_on"))
        hashes = dependency_hashes(skill_file, dependencies, body, skills_dir.parent)
        changed = sorted(path for path in set(previous.get("dependency_hashes", {})) | set(hashes)
                         if previous.get("dependency_hashes", {}).get(path) != hashes.get(path)) if previous.get("dependency_hashes") is not None else []
        stale = bool(previous.get("stale") or changed)
        stale_since = previous.get("stale_since") or (now if changed else None)
        if changed:
            entry.setdefault("history", []).append({"at": now, "from": entry["state"],
                "to": entry["state"], "reason": "dependencies changed: " + ", ".join(changed),
                "changed_dependencies": changed})
        validation = dict(previous.get("validation") or {"tests": tests, "status": "untested"})
        validation["tests"] = _list(meta.get("validation_tests")) or validation.get("tests") or tests
        if changed or (old_version and old_version != version):
            validation["status"] = "untested"
        record = SkillRecord(
            id=skill_id, version=version, content_hash=version,
            conflicts_with=_list(meta.get("conflicts_with")),
            name=str(meta.get("name", directory_name)), description=str(meta.get("description", "")),
            provenance="builtin", trust=entry["trust"], state=entry["state"],
            roles=_roles(role, meta.get("roles")), task_classes=_list(meta.get("task_classes"), ["*"]),
            triggers=triggers, tools=list(dict.fromkeys(tools)),
            context_requirements=_list(meta.get("requires_context")), output_contract=output,
            est_tokens_l0=len(str(meta.get("description", ""))) // 4,
            est_tokens_l1=len(" ".join(triggers + ([objective] if objective else []) + ([output] if output else []))) // 4,
            est_tokens_l2=_token_estimate(body), security_class=str(meta.get("security", "internal")),
            repo_scope=str(meta.get("repo", "orchestrator")),
            validation=validation, depends_on=dependencies, dependency_hashes=hashes,
            stale=stale, stale_since=stale_since,
            promotion_state=entry["state"], created_at=created, updated_at=updated,
            source=skill_file.relative_to(REPO).as_posix() if skill_file.is_relative_to(REPO) else str(skill_file),
        )
        records[skill_id] = asdict(record)

    for skill_id, previous in old_records.items():
        if previous.get("provenance") != "builtin":
            records[skill_id] = previous
            continue
        if skill_id in records:
            continue
        entry = states.setdefault(skill_id, {"state": previous.get("state", "discovered"),
                                             "trust": previous.get("trust", "untrusted"),
                                             "since": now, "reason": "", "history": []})
        if entry.get("state") != "disabled" or entry.get("reason") != "source removed":
            entry.setdefault("history", []).append({"at": now, "from": entry.get("state"),
                                                    "to": "disabled", "reason": "source removed"})
            entry.update({"state": "disabled", "since": now, "reason": "source removed"})
        missing = dict(previous)
        missing.update(state="disabled", promotion_state="disabled", trust=entry["trust"])
        records[skill_id] = missing

    discovered_path = root / "skills/discovered.json"
    discovered = _read_json(discovered_path, {"skills": {}})
    for skill_id, record in discovered.get("skills", {}).items():
        source = Path(record.get("source", ""))
        if not source.is_file():
            continue
        meta, body = _frontmatter(source.read_text())
        dependencies = _list(meta.get("depends_on"))
        hashes = dependency_hashes(source, dependencies, repo=skills_dir.parent)
        previous_hashes = record.get("dependency_hashes")
        changed = sorted(path for path in set(previous_hashes or {}) | set(hashes)
                         if (previous_hashes or {}).get(path) != hashes.get(path)) if previous_hashes is not None else []
        record.update(depends_on=dependencies, dependency_hashes=hashes)
        if changed:
            entry = states[skill_id]
            entry.setdefault("history", []).append({"at": now, "from": entry["state"], "to": entry["state"],
                "reason": "dependencies changed: " + ", ".join(changed), "changed_dependencies": changed})
            record.update(stale=True, stale_since=record.get("stale_since") or now)
            record.setdefault("validation", {})["status"] = "untested"
    if discovered_path.exists():
        _write_json(discovered_path, discovered)
    document = {"version": 1, "synced_at": now, "skills": records}
    _write_json(state_path, states)
    _write_json(registry_path, document)
    return document


def _testing_findings(skill_id: str, old_state: str, reason: str, root: Path) -> list[str]:
    """Apply the same inspection requirement on every entry into testing."""
    finding_ids = []
    record = load(root)["skills"][skill_id]
    if record["provenance"] != "builtin" or old_state == "quarantined":
        report = _read_json(root / "skills" / "quarantine" / skill_id / "findings.json", None)
        if not report or report.get("content_hash") != record["content_hash"]:
            raise ValueError("not inspected")
        from .skill_discovery import _hash
        directory = root / "skills" / "quarantine" / skill_id
        files = {}
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError("not inspected: quarantine contains symlink")
            if path.is_file() and path != directory / "findings.json":
                files[path.relative_to(directory).as_posix()] = path.read_bytes()
        if _hash(files) != report["content_hash"]:
            raise ValueError("not inspected: quarantine content changed")
        finding_ids = [f["id"] for f in report.get("findings", []) if f["severity"] == "block"]
        script_ids = [f["id"] for f in report.get("findings", []) if f.get("check") == "script"]
        if (finding_ids or script_ids) and not reason.startswith("override:"):
            raise ValueError("blocked findings: " + ", ".join(finding_ids or script_ids))
        finding_ids = finding_ids or script_ids
    return finding_ids


def transition(skill_id: str, new_state: str, reason: str, root: Path = STATE) -> dict[str, Any]:
    if new_state not in STATES:
        raise ValueError(f"unknown state: {new_state}")
    root = Path(root)
    states = _state_document(root)
    if skill_id not in states:
        raise KeyError(skill_id)
    entry = states[skill_id]
    old_state = entry["state"]
    if new_state not in ALLOWED_TRANSITIONS.get(old_state, set()):
        raise ValueError(f"invalid transition: {old_state}→{new_state}")
    finding_ids = _testing_findings(skill_id, old_state, reason, root) if new_state == "testing" else []
    now = _now()
    entry.setdefault("history", []).append({"at": now, "from": old_state, "to": new_state,
                                            "reason": reason})
    if finding_ids:
        entry["history"][-1].update(override=reason, finding_ids=finding_ids)
    entry.update({"state": new_state, "since": now, "reason": reason})
    _write_json(_paths(root)[1], states)
    _update_registry_state(root, skill_id, entry)
    return entry


def rollback(skill_id: str, root: Path = STATE) -> dict[str, Any]:
    root = Path(root)
    states = _state_document(root)
    if skill_id not in states:
        raise KeyError(skill_id)
    entry = states[skill_id]
    history = entry.setdefault("history", [])
    last = next((row for row in reversed(history) if row.get("from") != row.get("to")), None)
    if last is None:
        raise ValueError(f"no transition to roll back for {skill_id}")
    previous = last["from"]
    current = entry["state"]
    if previous == "testing":
        _testing_findings(skill_id, current, f"rollback: {last['reason']}", root)
    now = _now()
    history.append({"at": now, "from": current, "to": previous,
                    "reason": f"rollback: {last['reason']}"})
    entry.update({"state": previous, "since": now, "reason": history[-1]["reason"]})
    _write_json(_paths(root)[1], states)
    _update_registry_state(root, skill_id, entry)
    return entry


def _update_registry_state(root: Path, skill_id: str, entry: dict[str, Any]) -> None:
    for registry_path in (_paths(root)[0], root / "skills" / "discovered.json"):
        document = _read_json(registry_path, {})
        record = document.get("skills", {}).get(skill_id)
        if record is not None:
            record.update(state=entry["state"], promotion_state=entry["state"], trust=entry["trust"])
            _write_json(registry_path, document)


def load(root: Path = STATE) -> dict[str, Any]:
    root = Path(root)
    document = _read_json(_paths(root)[0], {"version": 1, "synced_at": None, "skills": {}})
    document["skills"].update(_read_json(root / "skills" / "discovered.json", {}).get("skills", {}))
    return document


_P6_HEADINGS = (
    "Trigger", "Objective", "Procedure", "Tools", "Evidence requirements",
    "Output contract", "Stop conditions", "Failure/recovery",
)


def _sections(body: str) -> dict[str, str]:
    """Return P6 sections, preserving their markdown headings."""
    matches = list(re.finditer(r"(?m)^## (.+?)\s*$", body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1)
        if name in _P6_HEADINGS:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            sections[name] = body[match.start():end].strip()
    return sections


def render(skill_id: str, level: int, root: Path = STATE) -> str:
    """Render an active registry skill at disclosure level 0, 1, or 2."""
    if level not in (0, 1, 2):
        raise ValueError(f"unknown disclosure level: {level}")
    record = load(Path(root)).get("skills", {}).get(skill_id)
    if record is None:
        raise KeyError(skill_id)
    if level == 0:
        description = str(record.get("description", "")).strip()
        first, separator, _ = description.partition(".")
        first = first.strip() + ("." if separator else "")
        return f"- {skill_id} — {first}"
    source = REPO / record["source"]
    _, body = _frontmatter(source.read_text())
    body = body.strip()
    if level == 2:
        return body
    sections = _sections(body)
    return "\n\n".join(sections[name] for name in ("Trigger", "Objective", "Output contract"))


def candidates(task: dict[str, Any], role: str, cfg: Any = None,
               root: Path = STATE) -> list[dict[str, Any]]:
    """Select active skills using only role, task class, and declared triggers."""
    del cfg  # Reserved for later routing stages; deterministic Stage 2 ignores it.
    records = load(Path(root)).get("skills", {})
    task_kind = attribution.task_class(task)
    haystack = " ".join(str(task.get(key, "")) for key in ("title", "spec")).lower()
    scope = [str(path) for path in (task.get("scope") or task.get("write_scope") or [])]
    selected = []
    for skill_id, record in records.items():
        if record.get("state") != "active" or role not in record.get("roles", []):
            continue
        classes = record.get("task_classes", [])
        if "*" not in classes and task_kind not in classes:
            continue
        triggers = record.get("triggers", [])
        matched = []
        for trigger in triggers:
            trigger = str(trigger)
            if trigger.startswith("scope:"):
                if any(fnmatch.fnmatch(path, trigger[6:]) for path in scope):
                    matched.append(trigger)
            elif trigger.lower() in haystack:
                matched.append(trigger)
        if not triggers or matched:
            selected.append({"id": skill_id, "matched": matched, "level": 1})
    return sorted(selected, key=lambda row: (-len(row["matched"]), row["id"]))


def dependency_hashes(source, dependencies, body="", repo=REPO):
    """Hash whole files, including builtin support files and referenced modules."""
    source, repo = Path(source), Path(repo)
    paths = set()
    for folder in ("scripts", "references"):
        paths.update(p for p in (source.parent / folder).rglob("*") if p.is_file())
    declarations = list(dependencies)
    declarations += re.findall(r"(?:[\w.-]+/)+[\w.*?-]+\.(?:py|sh|md|toml|json)", body)
    declarations += [m.replace(".", "/") + ".py" for m in
                     re.findall(r"\borchestrator(?:\.[a-zA-Z_]\w*)+", body)]
    for declaration in declarations:
        declaration = declaration.removeprefix("script:")
        if declaration.startswith("mcp__") or declaration in {"Read", "Write", "Edit", "Grep", "Glob", "Search", "Bash"}:
            if declaration.startswith("mcp__bus__") and (repo / "orchestrator/bus.py").is_file():
                paths.add(repo / "orchestrator/bus.py")
            declaration = "orchestrator/tool_catalog.py"
        candidate = Path(declaration)
        if candidate.is_absolute():
            if candidate.is_file():
                paths.add(candidate)
            continue
        for base in (repo, source.parent):
            paths.update(p for p in base.glob(declaration) if p.is_file())
    return {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def _update_record(skill_id, fields, root):
    for path in (_paths(root)[0], Path(root) / "skills/discovered.json"):
        document = _read_json(path, {})
        if skill_id in document.get("skills", {}):
            document["skills"][skill_id].update(fields)
            _write_json(path, document)


def revalidate(skill_id, root=STATE):
    """Run declared repository unittest files; never execute quarantine scripts."""
    root = Path(root)
    record = load(root)["skills"][skill_id]
    tests = record.get("validation", {}).get("tests") or []
    passed = bool(tests)
    for test in tests:
        path = (REPO / test).resolve()
        if not path.is_relative_to((REPO / "tests").resolve()) or not path.is_file():
            passed = False
            continue
        try:
            result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s",
                                     str(path.parent), "-p", path.name], cwd=REPO,
                                    capture_output=True, text=True, timeout=300)
            passed = passed and result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            passed = False
    now = _now()
    validation = {**record.get("validation", {}), "status": "tested" if passed else "failed",
                  "at": now, "version": record["version"],
                  "dependency_hashes": record.get("dependency_hashes", {})}
    fields = {"validation": validation}
    if passed:
        fields.update(stale=False, stale_since=None)
    states = _state_document(root)
    entry = states[skill_id]
    entry.setdefault("history", []).append({"at": now, "from": entry["state"], "to": entry["state"],
        "reason": "revalidation passed" if passed else "revalidation failed", "validation": validation})
    _write_json(_paths(root)[1], states)
    _update_record(skill_id, fields, root)
    return validation


def _redact_drafts(document, quarantine):
    """Redact secret-looking draft text at the shared discovery persistence boundary."""
    from .jev import redact
    from .skill_discovery import _hash
    for record in document.get("skills", {}).values():
        source = Path(record.get("source", ""))
        if not source.is_file() or not source.resolve().is_relative_to(quarantine.resolve()):
            continue
        body = source.read_text()
        safe = redact(body)
        if safe == body:
            continue
        source.write_text(safe)
        files = {p.relative_to(source.parent).as_posix(): p.read_bytes()
                 for p in source.parent.rglob("*") if p.is_file() and p.name != "findings.json"}
        record.update(content_hash=_hash(files), version=_hash(files))
        if record.get("provenance_info"):
            record["provenance_info"] = json.loads(redact(json.dumps(record["provenance_info"])))
