"""Read-only git evidence shared by pipeline and Planner routing."""
import subprocess
from pathlib import Path

def _git_in(worktree, *args):
    return subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True)

def _resolve_base(worktree, parent):
    """The trunk a worktree's HEAD should be compared against: the first of goal/<parent>, origin/main or main
    that resolves via merge-base, in that order -- a parentless task, or the first execute task of a goal that
    hasn't cut its goal branch yet, falls through to whichever trunk the worktree was actually cut from. None
    when none of the three exist (or worktree isn't a git repo at all). Shared by reconcile_dead (orphaned-work
    detection) and changed_paths() (security-path review routing) so both use the same fallback order."""
    candidates = ([f"goal/{parent}"] if parent else []) + ["origin/main", "main"]
    for candidate in candidates:
        r = _git_in(worktree, "merge-base", "HEAD", candidate)
        if r.returncode == 0:
            return r.stdout.strip()
    return None

def changed_paths(t, *, git=None, resolve_base=None):
    """git diff --no-renames --name-only -z <base>..HEAD in the task's worktree, repo-relative paths, base
    picked by _resolve_base(). --no-renames lists a rename as a plain delete-of-old-path + add-of-new-path
    pair instead of collapsing it into one "R100 old\\tnew" entry, so a rename of a security-sensitive path
    (e.g. a guarded .claude/hooks/x.sh moved somewhere outside the glob) still shows the old path and still
    matches. The -z / NUL split (rather than newline splitting on plain --name-only output) keeps a quoted or
    non-ASCII path intact so it still matches the security globs. None on any failure -- a non-git worktree, no
    base to diff against, or a git error -- so gate()'s security_paths policy can fail closed: a diff it cannot
    inspect is treated as a security match (review_reason "diff_unavailable"), never as "nothing changed"."""
    git = git or _git_in
    resolve_base = resolve_base or _resolve_base
    worktree = t.get("worktree")
    if not worktree or not Path(worktree).is_dir():
        return None
    try:
        base = resolve_base(worktree, t.get("parent"))
        if base is None:
            return None
        r = git(worktree, "diff", "--no-renames", "--name-only", "-z", f"{base}..HEAD")
        if r.returncode != 0:
            return None
        return [p for p in r.stdout.split("\0") if p]
    except Exception:
        return None

def _added_diff_lines(t, *, git=None, resolve_base=None):
    git = git or _git_in
    resolve_base = resolve_base or _resolve_base
    worktree = t.get("worktree")
    if not worktree or not Path(worktree).is_dir():
        return None
    base = resolve_base(worktree, t.get("parent"))
    if base is None:
        return None
    r = git(worktree, "diff", "--no-renames", "--unified=0", f"{base}..HEAD")
    if r.returncode:
        return None
    return [line[1:] for line in r.stdout.splitlines() if line.startswith("+") and not line.startswith("+++")]

