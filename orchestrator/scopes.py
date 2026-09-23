"""Contained repository scopes and Python import discovery."""
import ast
import fnmatch
import re
from pathlib import Path

def matches(path, entries):
    return any(entry in (".", "./") or (fnmatch.fnmatchcase(path, entry) if any(c in entry for c in "*?[")
               else path == entry.rstrip("/") or path.startswith(entry.rstrip("/") + "/"))
               for entry in entries)



def safe_scope(task, worktree=None):
    """Scope entries contained in the worktree, including resolved symlinks."""
    worktree = worktree or task.get("worktree")
    if not worktree:
        return list(task.get("write_scope", task.get("scope", [])))
    root = Path(worktree).resolve()
    entries = []
    for value in task.get("write_scope", task.get("scope", [])):
        entry = str(value)
        try:
            (root / entry).resolve().relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if Path(entry).is_absolute() or ".." in Path(entry).parts:
            continue
        entries.append(entry)
    return entries


def acceptance_paths(task):
    return sorted(set(re.findall(r"[\w./-]+\.py(?=::)", "\n".join(task.get("acceptance") or []))))


def imported_paths(task, worktree=None):
    worktree = worktree or task.get("worktree")
    if not worktree:
        return []
    wt = Path(worktree).resolve()
    paths = set()
    for entry in safe_scope(task, worktree):
        for path in wt.glob(entry):
            if path.suffix != ".py" or not path.is_file():
                continue
            try:
                path.resolve().relative_to(wt)
                tree = ast.parse(path.read_text(errors="replace"))
            except (OSError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = list(path.relative_to(wt).parent.parts)
                    if node.level:
                        prefix = prefix[:len(prefix) - node.level + 1]
                    else:
                        prefix = []
                    module = prefix + (node.module.split(".") if node.module else [])
                    names = [".".join(module)] + [".".join(module + [a.name]) for a in node.names]
                for name in filter(None, names):
                    stem = wt.joinpath(*name.split("."))
                    for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
                        try:
                            candidate.resolve().relative_to(wt)
                        except (OSError, RuntimeError, ValueError):
                            continue
                        if candidate.is_file():
                            paths.add(candidate.relative_to(wt).as_posix())
    return sorted(paths)


def read_scope(task, worktree=None):
    paths = {"tests/", *(task.get("read_scope") or [])}
    paths.update(str(Path(p).parent) + ("/" if str(Path(p).parent) != "." else "")
                 for p in safe_scope(task, worktree))
    paths.update(imported_paths(task, worktree))
    return sorted(paths)
