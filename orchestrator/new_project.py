"""`orchestrator new <path>`: turn a new or empty folder into a scaffolded orchestrator repo with one commit, so the
Planner never needs mkdir or git init. Local only: no remote, no push, no network, no Planner or daemon launch."""
import os, subprocess
from pathlib import Path

from .install import install

COMMIT_MESSAGE = ("orchestrator: scaffold (new project)\n\n"
                  "What: orchestrator install scaffold for a new project.\n"
                  "Why: the Planner starts new projects through `orchestrator new`.\n"
                  "Revert: delete the folder; nothing else depends on it yet.")


class NewProjectError(Exception):
    pass


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _toplevel(path: Path):
    r = _git(path, "rev-parse", "--show-toplevel")
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 else None


def create(path) -> list[str]:
    p = Path(path).expanduser()
    parent = Path(os.path.realpath(p.parent))
    if not parent.is_dir():
        raise NewProjectError(f"parent does not exist: {parent}")
    p = parent / p.name

    init = False
    if not p.exists() and not p.is_symlink():
        if _toplevel(parent) is not None:
            raise NewProjectError(f"{p} would sit inside another repo's working tree ({_toplevel(parent)})")
        existed, init = False, True
    elif p.is_dir() and not p.is_symlink():
        entries = list(p.iterdir())
        top = _toplevel(p)
        if not entries:
            if top is not None:
                raise NewProjectError(f"{p} is inside another repo's working tree ({top})")
            init = True
        elif top == os.path.realpath(p) and [e.name for e in entries] == [".git"]:
            if _git(p, "rev-parse", "--verify", "-q", "HEAD").returncode == 0:
                raise NewProjectError(f"{p} is a git repo with commits")
        else:
            raise NewProjectError(f"{p} is not empty")
        existed = True
    else:
        raise NewProjectError(f"{p} exists and is not a directory")

    report = [f"used existing {p}" if existed else f"created {p}"]
    if not existed:
        p.mkdir()
    if init:
        r = _git(p, "init", "-q", "-b", "main")
        if r.returncode != 0:
            raise NewProjectError(f"git init failed: {r.stderr.strip()}")
        report.append("git init -b main")
    try:
        report.extend(install(str(p)))
    except SystemExit:
        raise NewProjectError(f"install failed for {p}")

    r = _git(p, "add", "-A")
    if r.returncode == 0:
        r = _git(p, "commit", "-q", "-m", COMMIT_MESSAGE)
    if r.returncode != 0:
        raise NewProjectError(f"commit failed, scaffold files stay uncommitted in {p}: {r.stderr.strip()}")
    report.append(f"commit {_git(p, 'rev-parse', '--short', 'HEAD').stdout.strip()}")
    return report
