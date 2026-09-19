"""Serial merge queue: one at a time, rebase onto target -> tests-green -> fast-forward the target branch.
Target defaults to goal/<parent> (or 'integration'); main only ever moves via a human-approved PR."""
import fcntl, os, subprocess, sys
from . import ROOT, bus, scorecard
from .repomap import build
from .spawn import git

TESTS_GREEN = ROOT / ".claude" / "hooks" / "tests-green.sh"
IN_TESTS_GREEN = "ORCH_TESTS_GREEN"


def _can_refresh_repomap(root):
    """Only refresh a real orchestrator checkout, never a gate's scratch project."""
    return (not os.environ.get(IN_TESTS_GREEN)
            and not os.environ.get("TESTS_GREEN_DRY")
            and (root / "orchestrator" / "repomap.py").is_file())


def merge(task_id, target=None):
    t = bus.get(task_id)
    target = target or (f"goal/{t['parent']}" if t.get("parent") else "integration")
    wt = t.get("worktree") or str(ROOT / "wt" / task_id)
    with open(ROOT / ".orchestrator" / "merge.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # ponytail: global lock; one merge at a time is the design, not a shortcut
        if git("rev-parse", "--verify", target, check=False).returncode:
            base = "origin/main" if not git("rev-parse", "--verify", "origin/main", check=False).returncode else "HEAD"
            git("branch", target, base)
        r = git("rebase", target, cwd=wt, check=False)
        if r.returncode:
            conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False).stdout.split()
            hunks = git("diff", "--", *conflicts, cwd=wt, check=False).stdout[:20000]
            git("rebase", "--abort", cwd=wt, check=False)
            bus.update(task_id, status="failed", reason="rebase_conflict", resume_hint={"conflicts": conflicts, "hunks": hunks})
            return {"status": "conflict", "files": conflicts, "hunks": hunks}
        if not os.environ.get(IN_TESTS_GREEN):
            gate_env = {**os.environ, IN_TESTS_GREEN: "1"}
            tg = subprocess.run([str(TESTS_GREEN), wt], capture_output=True, text=True, input="{}", env=gate_env)
            if tg.returncode:
                bus.update(task_id, status="failed", reason="tests_red", resume_hint={"failures": tg.stderr[-4000:]})
                return {"status": "tests_red", "failures": tg.stderr[-4000:]}
        # fast-forward target without checking it out: safe because the task branch was just rebased onto it.
        # Exception: ROOT (the main checkout) has target checked out -> `git merge --ff-only` there instead, so
        # its HEAD, index and working tree move together (update-ref alone would leave them stale, see 2026-09-18).
        sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        previous_sha = git("rev-parse", target).stdout.strip()
        checked_out = git("symbolic-ref", "-q", "HEAD", cwd=ROOT, check=False).stdout.strip() == f"refs/heads/{target}"
        result = {"status": "merged", "target": target, "sha": sha}
        if checked_out:
            mff = git("merge", "--ff-only", sha, cwd=ROOT, check=False)
            if mff.returncode == 0:
                result["checkout_synced"] = True
            else:
                ff = git("update-ref", f"refs/heads/{target}", sha, git("rev-parse", target).stdout.strip(), check=False)
                if ff.returncode:
                    return {"status": "failed", "reason": ff.stderr}
                result["checkout_synced"] = False
                result["checkout_stale"] = True
                result["checkout_stale_error"] = mff.stderr[:500]
                print(f"[merge] main checkout is stale: sync ROOT to {target} when it is clean", file=sys.stderr)
        else:
            ff = git("update-ref", f"refs/heads/{target}", sha, git("rev-parse", target).stdout.strip(), check=False)
            if ff.returncode:
                return {"status": "failed", "reason": ff.stderr}
            result["checkout_synced"] = False
        try:
            changed = git("diff", "--name-only", previous_sha, sha, "--", "orchestrator", check=False).stdout.splitlines()
            if (_can_refresh_repomap(ROOT)
                    and any(path.startswith("orchestrator/") and path.endswith(".py") for path in changed)):
                architecture = ROOT / ".orchestrator" / "memory" / "architecture.md"
                architecture.parent.mkdir(parents=True, exist_ok=True)
                architecture.write_text(build(ROOT))
        except Exception as e:
            print(f"[merge] repomap generation failed: {e}", file=sys.stderr)
        bus.update(task_id, status="done", merged_into=target, sha=sha)
        bus.commit_state()
        try:
            scorecard.write(scorecard.build())
        except Exception:
            pass
        return result
