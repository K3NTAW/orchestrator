"""Shared test harness: one TMP orchestrator root per test process, the hook subprocess runner, and small git
helpers. ORCH_ROOT must be set before the first `from orchestrator import ...` anywhere in the process, so this
module does that at import time — every test file imports it first."""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="orch-"))
os.environ["ORCH_ROOT"] = str(TMP)
(TMP / ".orchestrator").mkdir()
for f in ("pool.toml",):
    config = (REPO / ".orchestrator" / f).read_text()
    # Tests opt into autonomous planning explicitly; never inherit the live repository setting.
    config = re.sub(r"(?m)^(\s*autonomous\s*=\s*).*$", r"\1false", config, count=1)
    (TMP / ".orchestrator" / f).write_text(config)
(TMP / ".orchestrator" / "prompts").symlink_to(REPO / ".orchestrator" / "prompts")
(TMP / ".claude").symlink_to(REPO / ".claude")
sys.path.insert(0, str(REPO))

HOOKS = REPO / ".claude" / "hooks"


def hook(name, payload, cwd=None, env=None):
    return subprocess.run([str(HOOKS / name)], input=json.dumps(payload), capture_output=True, text=True, cwd=cwd,
                          env={**os.environ, **(env or {})})


class FakeProc:
    """Stand-in for subprocess.run's CompletedProcess: executor._run only reads stdout/stderr/returncode."""
    def __init__(self, stdout, returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def codex_stream(*events):
    return "\n".join(json.dumps(e) for e in events)


def g(*a, cwd=TMP, **k):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, **k)


def scratch_repo(path):
    """Init a git repo at `path` with an initial commit (main branch, a .gitignore covering the orchestrator's
    own state dirs, user.name/email set) and return path. Safe to call more than once on the same path: git init
    and the commit are both no-ops when there is nothing new to do, so a test class can call this to make sure
    it has a usable repo without caring whether another test class already set one up."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=path, capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    gitignore = path / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".orchestrator/\nwt/\n.claude\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    return path
