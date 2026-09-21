"""Pure stale-work evidence collection.

This module deliberately owns no git state: callers may inject git and all
queries are made against the task worktree.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
from pathlib import Path

from . import gitutil, interference


ROOT = Path(__file__).resolve().parents[1]


def _default_git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _gitutil_git(git):
    """Adapt stale's keyword-cwd git callable to gitutil's cwd-first API."""
    return lambda cwd, *args: git(*args, cwd=cwd)


def load_links(root=ROOT):
    """Load the small, stable part of graphify's output used by stale checks."""
    try:
        raw = json.loads((Path(root) / "graphify-out" / "graph.json").read_text())
        nodes = raw["nodes"]
        links = raw["links"]
        files = {node["id"]: node["source_file"] for node in nodes}
        clean_links = [
            {
                "source": link["source"],
                "target": link["target"],
                "relation": link["relation"],
            }
            for link in links
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {"files": files, "links": clean_links}


def _scope(task):
    return task.get("scope", task.get("write_scope", [])) or []


def _matches(path, patterns):
    return any(path == pattern or fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _task_paths(task, changed):
    # Literal scopes are useful graph identities; globs cannot name a file.
    literals = {p for p in _scope(task) if not any(c in p for c in "*?[")}
    return set(changed) | literals


def _task_id(record):
    return record.get("id", record.get("task"))


def _sha(record):
    return record.get("sha", record.get("commit"))


def _is_ancestor(git, older, newer, worktree):
    result = git("merge-base", "--is-ancestor", older, newer, cwd=worktree)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise gitutil.GitError(
        getattr(result, "stderr", "git merge-base --is-ancestor failed")
    )


def _unknown(task, goal_ref):
    base = task.get("base", task.get("task_ref"))
    return {
        "base": base,
        "goal_head": goal_ref,
        "moved_count": 0,
        "stale_paths": [],
        "signals": {
            "scoped_files_changed": [],
            "changed_files_moved": [],
            "tests_changed": [],
            "interface_changed": [],
            "graph_relationships": [],
            "dependency_changed": [],
            "overlapping_merge": [],
        },
        "risk": "unknown",
        "risk_reasons": ["git_failed"],
        "graph": "absent",
    }


def evidence(
    task, *, goal_ref=None, graph_links=None, graph=None, tasks=None, git=None
):
    """Return deterministic evidence describing work made stale by goal moves."""
    git = git or _default_git
    worktree = task["worktree"]
    task_ref = task.get("base", task.get("task_ref"))
    parent = task.get("parent", task.get("goal_id"))
    goal_ref = goal_ref or (f"goal/{parent}" if parent else "main")
    adapter = _gitutil_git(git)

    try:
        moved = set(
            gitutil.moved_paths(task_ref, goal_ref, cwd=worktree, git=adapter)
        )
        changed = set(gitutil.changed_paths(task, git=adapter))
        task_paths = _task_paths(task, changed)

        scoped = sorted(path for path in moved if _matches(path, _scope(task)))
        changed_moved = sorted(moved & changed)

        stems = {
            Path(path).stem
            for path in task_paths
            if path.startswith("orchestrator/") and path.endswith(".py")
        }
        relevant_tests = {f"tests/test_{stem}.py" for stem in stems}
        tests_changed = sorted(moved & relevant_tests)

        interface = []
        relationships = set()
        graph_state = "absent"
        if graph_links is not None and graph is not None:
            fresh_git = lambda _worktree, *args: git(*args, cwd=worktree)
            fresh = interference.graph_fresh_for(
                moved | task_paths, graph, git=fresh_git
            )
            if fresh is not True:
                graph_state = "stale"
            else:
                files = graph_links.get("files", {})
                visible = set(files.values())
                if not ((moved | task_paths) & visible):
                    graph_state = "skipped"
                else:
                    graph_state = "used"
                    seen_interfaces = set()
                    for link in graph_links.get("links", []):
                        source_file = files.get(link.get("source"))
                        target_file = files.get(link.get("target"))
                        pairs = ((source_file, target_file, link.get("source")),
                                 (target_file, source_file, link.get("target")))
                        for moved_file, task_file, symbol in pairs:
                            if moved_file in moved and task_file in task_paths:
                                relationships.add((moved_file, task_file))
                                item = (symbol, moved_file, task_file)
                                if item not in seen_interfaces:
                                    seen_interfaces.add(item)
                                    interface.append({
                                        "symbol": symbol,
                                        "file": moved_file,
                                        "consumer": task_file,
                                    })

        task_records = list(tasks or [])
        by_id = {_task_id(item): item for item in task_records}
        dependencies = []
        for dep_id in task.get("depends_on", []) or []:
            record = by_id.get(dep_id)
            sha = _sha(record or {})
            if sha and not _is_ancestor(git, sha, task_ref, worktree):
                dependencies.append(dep_id)

        overlap = []
        comparison_paths = task_paths
        for record in task_records:
            record_id = _task_id(record)
            if (
                record_id == _task_id(task)
                or record.get("kind", record.get("task_class", "execute")) != "execute"
                or record.get("merged_into") != goal_ref
            ):
                continue
            candidates = set(record.get("changed_files", []) or [])
            candidates.update(
                p for p in _scope(record) if not any(c in p for c in "*?[")
            )
            intersects = bool(candidates & comparison_paths)
            if not intersects:
                intersects = any(
                    _matches(path, _scope(record)) for path in comparison_paths
                )
            sha = _sha(record)
            if intersects and sha and not _is_ancestor(git, sha, task_ref, worktree):
                overlap.append(record_id)

        signals = {
            "scoped_files_changed": scoped,
            "changed_files_moved": changed_moved,
            "tests_changed": tests_changed,
            "interface_changed": interface,
            "graph_relationships": [
                {"source_file": source, "target_file": target}
                for source, target in sorted(relationships)
            ],
            "dependency_changed": dependencies,
            "overlapping_merge": overlap,
        }
        reason_order = [
            "scoped_files_changed",
            "changed_files_moved",
            "tests_changed",
            "interface_changed",
            "graph_relationships",
            "dependency_changed",
            "overlapping_merge",
        ]
        reasons = [name for name in reason_order if signals[name]]
        if any(signals[name] for name in
               ("changed_files_moved", "interface_changed", "dependency_changed")):
            risk = "high"
        elif any(signals[name] for name in
                 ("tests_changed", "graph_relationships", "overlapping_merge")):
            risk = "medium"
        elif scoped:
            risk = "low"
        else:
            risk = "none"
        return {
            "base": task_ref,
            "goal_head": goal_ref,
            "moved_count": len(moved),
            "stale_paths": sorted(moved),
            "signals": signals,
            "risk": risk,
            "risk_reasons": reasons,
            "graph": graph_state,
        }
    except (gitutil.GitError, OSError, subprocess.SubprocessError):
        return _unknown(task, goal_ref)


def row(task, evidence, action, conflict=None):
    """Shape evidence for ``schedlog.append('stale', ...)``."""
    conflict_paths = conflict or []
    if isinstance(conflict, dict):
        conflict_paths = conflict.get("paths", conflict.get("files", []))
    return {
        "task": _task_id(task),
        "goal_id": task.get("goal_id", task.get("parent")),
        "base": evidence["base"],
        "goal_head": evidence["goal_head"],
        "changed_relevant_paths": evidence.get("stale_paths", []),
        "risk": evidence["risk"],
        "risk_reasons": evidence["risk_reasons"],
        "action": action,
        "conflict": conflict_paths,
    }
