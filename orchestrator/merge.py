"""Serial merge queue: one at a time, rebase onto target -> tests-green -> fast-forward the target branch.
Target defaults to goal/<parent> (or 'integration'); main only ever moves via a human-approved PR."""
import fcntl, hashlib, subprocess, sys, time
from . import ROOT, bus, scorecard
from .repomap import build
from .spawn import git

TESTS_GREEN = ROOT / ".claude" / "hooks" / "tests-green.sh"


def _diff_hash(target, wt):
    """Hash the patch (including its stat) relative to the common base, so a clean rebase preserves approval."""
    stat = git("diff", "--stat", f"{target}...HEAD", cwd=wt, check=False)
    content = git("diff", "--binary", f"{target}...HEAD", cwd=wt, check=False)
    if stat.returncode or content.returncode:
        return None
    return hashlib.sha256((stat.stdout + "\0" + content.stdout).encode()).hexdigest()


def _can_refresh_repomap(root, refresh_repomap):
    """Refresh only when requested and this checkout contains the generator."""
    return refresh_repomap and (root / "orchestrator" / "repomap.py").is_file()


def merge(task_id, target=None, *, refresh_repomap=True):
    t = bus.get(task_id)
    target = target or (f"goal/{t['parent']}" if t.get("parent") else "integration")
    wt = t.get("worktree") or str(ROOT / "wt" / task_id)
    with open(ROOT / ".orchestrator" / "merge.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # ponytail: global lock; one merge at a time is the design, not a shortcut
        if git("rev-parse", "--verify", target, check=False).returncode:
            base = "origin/main" if not git("rev-parse", "--verify", "origin/main", check=False).returncode else "HEAD"
            git("branch", target, base)
        reviewed = (t.get("pipeline") or {}).get("reviewed_sha")
        before_diff = _diff_hash(target, wt) if reviewed else None
        if reviewed and before_diff is None:
            reason = "diff_unavailable: git diff --stat/--binary failed before rebase"
            bus.update(task_id, status="failed", reason=reason)
            return {"status": "failed", "reason": reason}
        r = git("rebase", target, cwd=wt, check=False)
        if r.returncode:
            conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False).stdout.split()
            hunks = git("diff", "--", *conflicts, cwd=wt, check=False).stdout[:20000]
            git("rebase", "--abort", cwd=wt, check=False)
            bus.update(task_id, status="failed", reason="rebase_conflict", resume_hint={"conflicts": conflicts, "hunks": hunks})
            return {"status": "conflict", "files": conflicts, "hunks": hunks}
        after_diff = _diff_hash(target, wt) if reviewed else None
        if reviewed and after_diff is None:
            reason = "diff_unavailable: git diff --stat/--binary failed after rebase"
            bus.update(task_id, status="failed", reason=reason)
            return {"status": "failed", "reason": reason}
        if reviewed and before_diff != after_diff:
            return {"status": "rebase_changed_diff"}
        tg = subprocess.run([str(TESTS_GREEN), wt], cwd=wt, capture_output=True,
                            text=True, input="{}")
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
            if (_can_refresh_repomap(ROOT, refresh_repomap)
                    and any(path.startswith("orchestrator/") and path.endswith(".py") for path in changed)):
                architecture = ROOT / ".orchestrator" / "memory" / "architecture.md"
                architecture.parent.mkdir(parents=True, exist_ok=True)
                architecture.write_text(build(ROOT, rev=sha))
        except Exception as e:
            result["repomap_error"] = str(e)[:200]
            print(f"[merge] repomap refresh failed: {e}", file=sys.stderr)
        changed_files = git("diff", "--name-only", f"{previous_sha}..{sha}", check=False).stdout.splitlines()[:500]
        merged_at = time.time()
        with bus.locked():
            pipeline = dict(bus.get(task_id).get("pipeline") or {})
            pipeline.setdefault("merged_at", merged_at)
            bus.update(task_id, status="done", merged_into=target, sha=sha,
                       merged_at=merged_at, changed_files=changed_files, pipeline=pipeline)
        bus.commit_state()
        try:
            scorecard.write(scorecard.build())
        except Exception:
            pass
        return result
