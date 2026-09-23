"""Retrieve external skills as inert quarantine data; never execute their content."""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import re
import subprocess
import tomllib
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

from . import STATE, context_scanner
from . import skills_registry as registry

MAX_FILES = 50
MAX_BYTES = 200 * 1024
SEARCH_ROOTS = ("skills", ".claude/skills", ".codex/skills")


def _config(root):
    try:
        return tomllib.loads((Path(root) / "pool.toml").read_text()).get("skills", {})
    except (OSError, ValueError):
        return {}


def _git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                            text=True, timeout=30, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _repo_name(remote):
    match = re.search(r"(?:github\.com[:/])([^/]+/[^/]+?)(?:\.git)?/?$", remote)
    return match.group(1) if match else remote.removesuffix(".git").strip("/")


def _gh_api(endpoint):
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True,
                            timeout=30, check=False)
    if result.returncode:
        raise ValueError("GitHub retrieval failed: " + result.stderr.strip()[:300])
    return json.loads(result.stdout)


def _safe_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in ("..", ".") for p in path.parts):
        raise ValueError("unsafe source path")
    return path


def _hash(files):
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()[:12]


def _bounded(files, name, data):
    _safe_path(name)
    if name == "findings.json":
        raise ValueError("reserved quarantine report filename")
    if len(files) >= MAX_FILES or sum(map(len, files.values())) + len(data) > MAX_BYTES:
        raise ValueError("skill exceeds 50 files or 200 KB cap")
    files[name] = data


def _local_files(directory):
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlinks are refused")
        if path.is_file():
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("skill exceeds 200 KB cap")
            _bounded(files, path.relative_to(directory).as_posix(), path.read_bytes())
    return files


def _remote_files(repo, commit, directory):
    files = {}
    directories = 0

    def walk(path):
        nonlocal directories
        directories += 1
        if directories > MAX_FILES:
            raise ValueError("skill exceeds directory cap")
        entries = _gh_api(f"repos/{repo}/contents/{quote(path, safe='/')}?ref={quote(commit, safe='')}")
        if not isinstance(entries, list):
            raise ValueError("expected GitHub directory")
        for item in sorted(entries, key=lambda row: row["path"]):
            name = str(_safe_path(item["path"]).relative_to(directory))
            if item["path"] != str(PurePosixPath(path) / item["name"]):
                raise ValueError("unexpected GitHub path")
            if item["type"] == "dir":
                walk(item["path"])
            elif item["type"] == "file":
                if len(files) >= MAX_FILES or item.get("size", 0) + sum(map(len, files.values())) > MAX_BYTES:
                    raise ValueError("skill exceeds 50 files or 200 KB cap")
                content = _gh_api(f"repos/{repo}/contents/{quote(item['path'], safe='/')}?ref={quote(commit, safe='')}")
                if content.get("type") != "file" or content.get("encoding") != "base64":
                    raise ValueError("unsupported GitHub file")
                _bounded(files, name, base64.b64decode(content["content"]))
            else:
                raise ValueError("symlinks and submodules are refused")
    walk(directory)
    return files


def _fetch(source, root):
    """Return candidates plus per-source errors, without invoking source code."""
    candidates, errors = [], []
    local = Path(source).expanduser()
    try:
        if local.exists():
            local = local.resolve()
            builtin = (registry.REPO / "skills").resolve()
            if local == builtin or local.is_relative_to(builtin):
                raise ValueError("builtin path refused: already registered")
            repo = _repo_name(_git(local, "remote", "get-url", "origin"))
            source_repo = repo or str(local)
            commit = _git(local, "rev-parse", "HEAD") or None
            trusted = repo in _config(root).get("trusted_repos", []) if repo else False
            provenance = "repository" if trusted else "external"
            bases = [local / name for name in SEARCH_ROOTS]
            if local.name == "skills" or (local / "SKILL.md").is_file():
                bases = [local]
            license_path = local / "LICENSE"
            license_text = license_path.read_text()[:MAX_BYTES] if license_path.is_file() and not license_path.is_symlink() else None
            for skill in sorted({p for base in bases for p in base.rglob("SKILL.md")}):
                try:
                    if any(p.is_symlink() for p in (skill, *skill.parents) if p.is_relative_to(local)):
                        raise ValueError("symlinks are refused")
                    if skill.resolve().is_relative_to(builtin):
                        raise ValueError("builtin path refused: already registered")
                    files = _local_files(skill.parent)
                    candidates.append((source_repo, skill.relative_to(local).as_posix(), commit,
                                       provenance, files, license_text, str(local), None,
                                       [p.relative_to(skill.parent).as_posix() for p in skill.parent.rglob("*")
                                        if p.is_file() and p.stat().st_mode & 0o111]))
                except (OSError, ValueError) as error:
                    errors.append(str(error))
        else:
            match = re.fullmatch(r"([\w.-]+/[\w.-]+)(?:@(.+))?", source)
            if not match:
                raise ValueError("expected local path or owner/repo[@ref]")
            repo, ref = match.groups()
            ref = ref or _gh_api(f"repos/{repo}")["default_branch"]
            commit = _gh_api(f"repos/{repo}/commits/{quote(ref, safe='')}")["sha"]
            license_text = None
            # Repository root listing avoids treating a missing license as a retrieval failure.
            listing = _gh_api(f"repos/{repo}/contents?ref={quote(commit, safe='')}")
            for item in listing:
                if item["name"] == "LICENSE" and item.get("type") == "file" and item.get("size", 0) <= MAX_BYTES:
                    value = _gh_api(f"repos/{repo}/contents/LICENSE?ref={quote(commit, safe='')}")
                    license_text = base64.b64decode(value["content"]).decode("utf-8", errors="replace")
            visited = 0

            def search(path):
                nonlocal visited
                visited += 1
                if visited > 500:
                    raise ValueError("repository discovery directory cap exceeded")
                entries = _gh_api(f"repos/{repo}/contents/{quote(path, safe='/')}?ref={quote(commit, safe='')}")
                if any(i["name"] == "SKILL.md" and i["type"] == "file" for i in entries):
                    try:
                        files = _remote_files(repo, commit, path)
                        candidates.append((repo, path + "/SKILL.md", commit, "external", files,
                                           license_text, repo, ref, []))
                    except (OSError, ValueError, KeyError) as error:
                        errors.append(str(error))
                    return
                for item in entries:
                    if item["type"] == "dir":
                        expected = str(PurePosixPath(path) / item["name"])
                        if str(_safe_path(item["path"])) != expected:
                            raise ValueError("unexpected GitHub path")
                        search(expected)
            for base in SEARCH_ROOTS:
                try:
                    search(base)
                except (OSError, ValueError, KeyError) as error:
                    errors.append(f"{base}: {error}")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        errors.append(str(error))
    return candidates, errors


def _store(candidate, root, *, new_version=False):
    repo, source_path, commit, provenance, files, license_text, source, ref, executable_files = candidate
    path = Path(root) / "skills" / "discovered.json"
    document = registry._read_json(path, {"version": 1, "skills": {}})
    records = document["skills"]
    now, content_hash = registry._now(), _hash(files)
    previous = next((r for r in records.values()
                     if (r.get("provenance_info") or {}).get("source_repo") == repo
                     and (r.get("provenance_info") or {}).get("source_path") == source_path), None)
    if previous and not new_version and previous["content_hash"] == content_hash:
        previous["provenance_info"]["retrieved_at"] = now
        registry._write_json(path, document)
        return previous
    name = PurePosixPath(source_path).parent.name
    repo_id = repo.replace("/", "-") if not Path(repo).is_absolute() else "local-" + hashlib.sha256(repo.encode()).hexdigest()[:12]
    skill_id = f"{provenance}/{repo_id}/{name}"
    if previous and not new_version:
        skill_id = previous["id"]
    elif new_version:
        skill_id += "@" + content_hash
    elif skill_id in registry.load(root)["skills"]:
        skill_id += "-" + hashlib.sha256(source_path.encode()).hexdigest()[:8]
    _safe_path(skill_id)
    if new_version and skill_id in records:
        return records[skill_id]
    directory = Path(root) / "skills" / "quarantine" / skill_id
    # All retained files are plain data, written without executable permissions.
    directory.mkdir(parents=True, exist_ok=True)
    for existing in directory.rglob("*"):
        if existing.is_file():
            existing.unlink()
    for filename, data in files.items():
        target = directory / str(_safe_path(filename))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o600)
    text = files["SKILL.md"].decode("utf-8", errors="replace")
    description = re.search(r"(?m)^description:\s*(.*)$", text)
    heading = re.search(r"(?m)^#+\s+(.+)$", text)
    description = description.group(1).strip('"\'') if description else heading.group(1) if heading else ""
    info = dict(source_repo=repo, source_path=source_path, source_commit=commit, retrieved_at=now,
                license=license_text, content_hash=content_hash, modifications=[])
    record = asdict(registry.SkillRecord(
        id=skill_id, version=content_hash, content_hash=content_hash, name=name, description=description,
        provenance=provenance, trust="untrusted", state="discovered", roles=[], task_classes=[], triggers=[],
        tools=[], context_requirements=[], output_contract="", est_tokens_l0=0, est_tokens_l1=0,
        est_tokens_l2=len(text)//4, security_class="unknown", repo_scope="", validation={},
        promotion_state="discovered", created_at=now, updated_at=now,
        source=str(directory / "SKILL.md"), provenance_info=info))
    record.update(upstream_source=source, upstream_ref=ref, executable_files=executable_files)
    states = registry._state_document(root)
    if previous and not new_version:
        record.update(state=previous["state"], promotion_state=previous["state"], created_at=previous["created_at"])
        info["modifications"] = previous["provenance_info"]["modifications"]
    entry = states.setdefault(skill_id, dict(state="discovered", trust="untrusted", since=now,
                                             reason="static discovery", history=[]))
    entry.setdefault("versions", []).append(dict(content_hash=content_hash, retrieved_at=now))
    record["versions"] = entry["versions"]
    records[skill_id] = record
    registry._write_json(path, document)
    registry._write_json(registry._paths(Path(root))[1], states)
    return record


def discover(source, root=STATE):
    candidates, errors = _fetch(str(source), Path(root))
    records = [_store(candidate, Path(root)) for candidate in candidates]
    report = dict(candidates=records, errors=errors)
    registry._write_json(Path(root) / "skills" / "discovery-errors.json",
                         dict(retrieved_at=registry._now(), source=str(source), errors=errors))
    return report


CHECKS = (
    ("shell", "warn", r"```(?:sh|bash|shell|zsh)|\b(?:sudo|chmod|bash|sh|echo|export|mkdir|cp|mv|touch|sed|tee)\s"),
    ("network", "warn", r"https?://|\b(?:curl|wget|socket|pip|npm|apt|brew|yarn)\b"),
    ("outside-worktree", "warn", r"(?<![\w/])/(?:[\w.-]+/)*[\w.-]+|~/|\.git\b"),
    ("credentials", "block", r"\b[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*\b|keychain|1password|\.env\b"),
    ("mcp", "warn", r"mcp__\w+|\bmcp\b|\bserver(?:s|_name)?\s*[:=]"),
    ("destructive", "block", r"\brm\s+-[\w]*r[\w]*f|\brm\s+-[\w]*f[\w]*r|git\s+push\s+.*--force|git\s+reset\s+--hard|\bDROP\b|\btruncate\b"),
)

SCRIPT_EXTENSIONS = {".bash", ".js", ".mjs", ".pl", ".ps1", ".py", ".rb", ".sh", ".zsh"}
CREDENTIAL_NAME = re.compile(r"\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Z0-9_]*\b")


def _risk_report(files, executable_files, scans):
    scripts, endpoints, tools, credential_refs, dependencies, filesystem = [], set(), set(), set(), set(), set()
    url_dependencies = False
    for name, data in sorted(files.items()):
        suffix = PurePosixPath(name).suffix.lower()
        first = data.splitlines()[0].decode("utf-8", errors="replace") if data else ""
        shebang = first if first.startswith("#!") else None
        if shebang or suffix in SCRIPT_EXTENSIONS or name in executable_files:
            item = {"path": name, "executable": name in executable_files}
            item["shebang" if shebang else "extension"] = shebang or suffix
            scripts.append(item)
        text = data.decode("utf-8", errors="replace")
        urls = re.findall(r"https?://[^\s<>\"')\]}]+", text)
        endpoints.update(urls)
        endpoints.update(host for url in urls if (host := urlparse(url).hostname))
        credential_refs.update(CREDENTIAL_NAME.findall(text))
        credential_refs.update(re.findall(r"(?i)(?:^|[/\s'\"])(\.env(?:\.[\w.-]+)?|[^/\s'\"]*credentials?(?:\.[\w.-]+)?)", text))
        if name == "SKILL.md":
            meta, _ = registry._frontmatter(text)
            for field in ("allowed-tools", "allowed_tools", "tools"):
                tools.update(registry._list(meta.get(field)))
        if re.search(r"(?i)\b(?:bash|shell|sh|zsh)\b", text):
            tools.add("Bash")
        if PurePosixPath(name).name == "requirements.txt":
            dependencies.update(line.strip() for line in text.splitlines()
                                if line.strip() and not line.lstrip().startswith("#"))
        elif PurePosixPath(name).name == "package.json":
            try:
                package = json.loads(text)
                for field in ("dependencies", "devDependencies", "optionalDependencies"):
                    dependencies.update(f"{key}@{value}" for key, value in package.get(field, {}).items())
            except (ValueError, TypeError):
                pass
        elif PurePosixPath(name).name == "pyproject.toml":
            try:
                project = tomllib.loads(text)
                dependencies.update(project.get("project", {}).get("dependencies", []))
            except (tomllib.TOMLDecodeError, TypeError):
                pass
        for match in re.finditer(r"(?im)^\s*(?:python\s+-m\s+)?(pip|npm)\s+install\s+([^\n;&|]+)", text):
            dependencies.update(part for part in re.split(r"\s+", match.group(2).strip()) if not part.startswith("-"))
        filesystem.update(re.findall(r"(?<![\w.])(?:~(?:/[^\s'\"`]+)?|/(?:[^\s'\"`]+))", text))
        filesystem.update(m.group(0).strip() for m in re.finditer(
            r"(?im)^\s*(?:sudo\s+)?(?:rm\s+[^\n]*(?:-[^\s]*r|--recursive)|chmod\s+[^\n]+)", text))
    url_dependencies = any(re.search(r"(?:https?://|git\+|github\.com[:/])", item, re.I) for item in dependencies)
    blocked = any(result.get("verdict") == "blocked" for result in scans.values())
    destructive = any(re.search(r"(?i)\b(?:rm\s+.*(?:-[^\s]*r|--recursive)|chmod)\b", item)
                      for item in filesystem)
    reasons = []
    if blocked:
        reasons.append("blocked context scan")
    if endpoints and credential_refs:
        reasons.append("endpoint and credential reference")
    if destructive:
        reasons.append("recursive delete or permission change")
    if url_dependencies:
        reasons.append("dependency installed from URL or git source")
    populated = scripts or endpoints or dependencies or credential_refs
    level = "high" if reasons else "medium" if populated else "low"
    if level == "medium":
        reasons.append("script, endpoint, dependency, or credential reference present")
    return {"scripts": scripts, "endpoints": sorted(endpoints), "tools": sorted(tools),
            "credential_refs": sorted(credential_refs), "dependencies": sorted(dependencies),
            "filesystem": sorted(filesystem), "scan": scans, "level": level, "reasons": reasons}


def inspect(skill_id, root=STATE):
    root = Path(root)
    record = registry.load(root)["skills"][skill_id]
    if record["state"] != "quarantined":
        raise ValueError("inspection requires quarantined state")
    directory = root / "skills" / "quarantine" / skill_id
    if any(p.is_symlink() for p in directory.rglob("*")):
        raise ValueError("symlinks are refused")
    files = {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob("*")
             if p.is_file() and p != directory / "findings.json"}
    if _hash(files) != record["content_hash"]:
        raise ValueError("quarantine content changed; discover again before inspection")
    findings = []
    allowed = _config(root).get("allowed_domains", [])

    def add(check, severity, name, line, excerpt):
        findings.append(dict(id=f"{check}-{len(findings)+1:04d}", severity=severity, check=check,
                             line=f"{name}:{line}", excerpt=excerpt[:200]))
    scans = {}
    for name, data in sorted(files.items(), key=lambda item: (item[0] != "SKILL.md", item[0])):
        if (name.startswith("scripts/") or PurePosixPath(name).suffix.lower() in SCRIPT_EXTENSIONS
                or name in record.get("executable_files", []) or data.startswith(b"#!")):
            add("script", "warn", name, 1, "script file (never executed)")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\0" in text:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for check, severity, pattern in CHECKS:
                flags = 0 if check == "credentials" else re.I
                if re.search(pattern, line, flags):
                    add(check, severity, name, number, line)
            for url in re.findall(r"https?://[^\s<>\"')]+", line):
                if urlparse(url).hostname not in allowed:
                    add("external-endpoint", "warn", name, number, url)
        result = context_scanner.scan(text, source_kind="skill")
        scans[name] = result
        if result["verdict"] != "safe":
            severity = {"blocked": "block", "suspicious": "warn"}[result["verdict"]]
            for finding in result["findings"]:
                family = {"exfiltration": "network", "credential_read": "credentials"}.get(finding["pattern"])
                if any(f["check"] == family and f["line"] == f"{name}:{finding['line']}" for f in findings):
                    continue
                add(finding["pattern"], severity, name, finding["line"], finding["excerpt"])
    overall = max((result["verdict"] for result in scans.values()),
                  key={"safe": 0, "suspicious": 1, "blocked": 2}.get, default="safe")
    severity = max((f["severity"] for f in findings), key={"info": 0, "warn": 1, "block": 2}.get, default="info")
    risk = _risk_report(files, record.get("executable_files", []), scans)
    report = dict(inspected_at=registry._now(), content_hash=record["content_hash"], findings=findings, max_severity=severity,
                  scan=scans, scan_overall=overall, risk=risk)
    registry._write_json(directory / "findings.json", report)
    return report


def check_upstream(skill_id, root=STATE):
    root = Path(root)
    record = registry.load(root)["skills"][skill_id]
    if record["provenance"] == "builtin":
        raise ValueError("builtin import refused: already registered")
    source = record["upstream_source"]
    if record.get("upstream_ref"):
        source += "@" + record["upstream_ref"]
    candidates, errors = _fetch(source, root)
    for candidate in candidates:
        if candidate[1] != record["provenance_info"]["source_path"]:
            continue
        if _hash(candidate[4]) == record["content_hash"]:
            return dict(changed=False, errors=errors)
        old_dir = root / "skills" / "quarantine" / skill_id
        old = {p.relative_to(old_dir).as_posix(): p.read_bytes() for p in old_dir.rglob("*")
               if p.is_file() and p != old_dir / "findings.json"}
        added = removed = 0
        for name in old.keys() | candidate[4].keys():
            diff = difflib.ndiff(old.get(name, b"").decode(errors="replace").splitlines(),
                                 candidate[4].get(name, b"").decode(errors="replace").splitlines())
            for line in diff:
                added += line.startswith("+ ")
                removed += line.startswith("- ")
        version = _store(candidate, root, new_version=True)
        version["diff_summary"] = dict(lines_added=added, lines_removed=removed)
        path = root / "skills" / "discovered.json"
        document = registry._read_json(path, {})
        document["skills"][version["id"]] = version
        registry._write_json(path, document)
        return dict(changed=True, candidate=version, errors=errors)
    return dict(changed=False, errors=errors or ["upstream skill missing"])
