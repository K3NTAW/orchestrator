"""Ship a closed goal to the target branch (main) unattended: full gate on the exact merged tree, then gh pr merge.

Selection: tick() scans the bus for goal tasks with result.goal_closed true and pipeline.ship.state not in
(shipped, held), oldest closed first, and advances one per call. It is disabled unless [ship].enabled is true.

State lives in pipeline.ship on the goal: pending -> gating -> merging -> shipped, or held. Every step first reads
the PR truth (`gh pr view`); a MERGED PR is recorded as shipped. That makes a run resumable after a crash: there is
no lease, a gating/merging goal with no ship.lock holder is simply resumed, and a gate that died is re-run.

Concurrency: one ship per repo, guarded only by an fcntl lock on .orchestrator/ship.lock held for the whole run.
The gate may take up to 2x [gate].timeout_s (run_gate retries once); the lock stays held that long by design.

Writes: only pipeline.ship, always through _ship_update(). The target branch only moves through `gh pr merge`;
nothing here force-pushes or pushes to the target directly.

Residual window: origin/<target> can move between the final fetch and gh pr merge. After the merge the merge
commit's first parent is compared with gated_target_sha; on a mismatch the full gate re-runs on the merge commit,
and a red result rolls the merge back at once and holds the goal. Between that merge and the rollback, the target
holds an ungated tree."""
import fcntl, json, os, re, subprocess, sys, threading, time
from pathlib import Path

from . import ROOT, bus, gate, merge, notify

ACTIVE = ("pending", "gating", "merging")
_THREADS = []


class _Hold(Exception):
    def __init__(self, reason, tail=""):
        super().__init__(reason)
        self.reason, self.tail = reason, tail


def settings(cfg):
    ship = cfg.get("ship") or {}
    env = ship.get("gate_env") or {}
    return {"enabled": ship.get("enabled", False) is True, "target": ship.get("target") or "main",
            "max_regates": int(ship.get("max_regates", 3)),
            "gate_env": {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}}


def _git(root, *args, check=False):
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and r.returncode:
        raise _Hold(f"git {args[0]} failed", (r.stdout + r.stderr)[-2000:])
    return r


def _gh(root, *args):
    return subprocess.run(["gh", *args], cwd=root, capture_output=True, text=True, timeout=120)


def _ship(task):
    return dict((task.get("pipeline") or {}).get("ship") or {})


def _ship_update(goal_id, **fields):
    """The only writer: re-read the goal under the bus lock and replace pipeline["ship"] alone."""
    with bus.locked():
        goal = bus.get(goal_id)
        pipeline = dict(goal.get("pipeline") or {})
        state = {**(pipeline.get("ship") or {}), **fields, "updated_at": time.time()}
        pipeline["ship"] = state
        bus.update(goal_id, pipeline=pipeline)
        return state


def _notify_once(goal_id, key, msg):
    with bus.locked():
        notified = dict(_ship(bus.get(goal_id)).get("notified") or {})
        if key in notified:
            return False
        notified[key] = time.time()
        _ship_update(goal_id, notified=notified)
    notify.notify(msg)
    return True


def _hold(goal_id, reason, tail=""):
    state = _ship_update(goal_id, state="held", last_error=reason, failure_tail=tail[-4000:])
    _notify_once(goal_id, f"held:{reason}:{state.get('retries', 0)}", f"{goal_id}: ship held: {reason}")
    return {"status": "held", "reason": reason}


def _record_decision(title, fact, goal_id):
    script = ROOT / ".claude" / "skills" / "memory" / "scripts" / "record.sh"
    if not script.exists():
        return
    try:
        subprocess.run([str(script), "add", "--file", "decisions", "--type", "decision", "--title", title,
                        "--goal", goal_id, "--fact", fact, "--outcome", "recorded by orchestrator ship"],
                       capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[ship] decisions record failed: {e}", file=sys.stderr)


def _gate(root, wt, task_id, cfg):
    env = {**os.environ, **settings(cfg)["gate_env"]}
    r = gate.run_gate(wt, script=merge.TESTS_GREEN, task_id=task_id, cfg=cfg, env=env)
    tail = (r.get("stdout", "") + r.get("stderr", ""))[-4000:]
    return (not r["timed_out"] and r["returncode"] == 0), ("gate timeout" if r["timed_out"] else "gate red"), tail


def _worktree(root, name, ref):
    base = Path(root) / ".orchestrator" / "ship-wt"
    base.mkdir(parents=True, exist_ok=True)
    wt = base / name
    if wt.exists():
        _git(root, "worktree", "remove", "--force", str(wt))
    _git(root, "worktree", "prune")
    _git(root, "worktree", "add", "--detach", str(wt), ref, check=True)
    return wt


def _drop_worktree(root, wt):
    _git(root, "worktree", "remove", "--force", str(wt))
    _git(root, "worktree", "prune")


def _sha(root, ref):
    r = _git(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else None


def _pr_number(url):
    m = re.search(r"/(\d+)\s*$", url.strip())
    return int(m.group(1)) if m else None


def _find_pr(root, goal_id, target):
    r = _gh(root, "pr", "list", "--head", f"goal/{goal_id}", "--base", target, "--state", "all",
            "--json", "number,state,url,mergeCommit")
    if r.returncode:
        raise _Hold("gh pr list failed", r.stderr[-2000:])
    prs = [p for p in json.loads(r.stdout or "[]") if p.get("state") != "CLOSED"]
    merged = [p for p in prs if p.get("state") == "MERGED"]
    return (merged or [p for p in prs if p.get("state") == "OPEN"] or [None])[0]


def _create_pr(root, goal, target):
    body = (f"Goal: {goal['id']}\n\nShipped by the orchestrator after a green full gate on the merged tree.\n"
            "Rollback after merge: `orchestrator rollback <merge_sha>`.\n")
    r = _gh(root, "pr", "create", "--head", f"goal/{goal['id']}", "--base", target,
            "--title", f"{goal['id']}: {goal.get('title', '')[:150]}", "--body", body)
    if r.returncode or not r.stdout.strip():
        raise _Hold("gh pr create failed", r.stderr[-2000:])
    url = r.stdout.strip().splitlines()[-1]
    return {"number": _pr_number(url), "url": url, "state": "OPEN"}


def _pr_truth(root, number):
    r = _gh(root, "pr", "view", str(number), "--json", "state,mergeCommit,headRefOid")
    if r.returncode:
        return None
    return json.loads(r.stdout or "{}")


def _merge_oid(pr):
    return ((pr or {}).get("mergeCommit") or {}).get("oid")


def _stopped(stop_event):
    return stop_event is not None and stop_event.is_set()


def _gating(goal_id, root, cfg, target):
    st = _ship_update(goal_id, state="gating")
    _git(root, "fetch", "origin", check=True)
    local, remote = f"goal/{goal_id}", f"origin/goal/{goal_id}"
    has_local, has_remote = _sha(root, local), _sha(root, remote)
    base = remote if has_remote and (not has_local or
                                     _git(root, "merge-base", "--is-ancestor", local, remote).returncode == 0) else local
    if not (has_local or has_remote):
        raise _Hold("ship goal branch missing")
    wt = _worktree(root, f"ship-{goal_id}", base)
    try:
        r = _git(wt, "merge", "--no-edit", f"origin/{target}")
        if r.returncode:
            _git(wt, "merge", "--abort")
            raise _Hold("ship conflict", (r.stdout + r.stderr)[-2000:])
        head = _sha(wt, "HEAD")
        if has_remote and _git(wt, "merge-base", "--is-ancestor", remote, "HEAD").returncode:
            raise _Hold("ship push not fast-forward")
        push = _git(wt, "push", "origin", f"HEAD:refs/heads/goal/{goal_id}")
        if push.returncode:
            raise _Hold("ship push failed", push.stderr[-2000:])
        target_sha = _sha(wt, f"origin/{target}")
        if not st.get("pr_number"):
            pr = _find_pr(root, goal_id, target) or _create_pr(root, bus.get(goal_id), target)
            _ship_update(goal_id, pr_number=pr["number"], pr_url=pr["url"])
        _ship_update(goal_id, gated_head_sha=head, gated_target_sha=target_sha)
        ok, reason, tail = _gate(root, wt, goal_id, cfg)
        if not ok:
            raise _Hold(reason, tail)
    finally:
        _drop_worktree(root, wt)
    _ship_update(goal_id, state="merging")


def _merging(goal_id, root, cfg, target):
    st = _ship(bus.get(goal_id))
    _git(root, "fetch", "origin", check=True)
    if _sha(root, f"origin/{target}") != st.get("gated_target_sha"):
        attempts = st.get("attempts", 0) + 1
        if attempts > settings(cfg)["max_regates"]:
            _ship_update(goal_id, attempts=attempts)
            raise _Hold("ship target kept moving")
        _ship_update(goal_id, state="gating", attempts=attempts)
        return
    title = bus.get(goal_id).get("title", "")[:150]
    n = st["pr_number"]
    r = _gh(root, "pr", "merge", str(n), "--merge", "--match-head-commit", st["gated_head_sha"],
            "--subject", f"Merge PR {n}: {title} (goal/{goal_id}, auto-merged after green full gate)")
    if r.returncode and (_pr_truth(root, n) or {}).get("state") != "MERGED":
        raise _Hold("ship merge failed", r.stderr[-2000:])


def _on_merged(goal_id, root, cfg, target, merge_sha, stop_event=None):
    st = _ship_update(goal_id, merge_sha=merge_sha)
    _git(root, "fetch", "origin")
    first_parent = _sha(root, f"{merge_sha}^1")
    if st.get("gated_target_sha") and first_parent and first_parent != st["gated_target_sha"] \
            and not st.get("post_merge_gate"):
        wt = _worktree(root, f"ship-post-{goal_id}", merge_sha)
        try:
            ok, _reason, tail = _gate(root, wt, goal_id, cfg)
        finally:
            _drop_worktree(root, wt)
        _ship_update(goal_id, post_merge_gate="green" if ok else "red")
        if not ok:
            rb = rollback(merge_sha, root=root, cfg=cfg)
            _ship_update(goal_id, rollback=rb)
            return _hold(goal_id, "post-merge gate red", tail)
    _ship_update(goal_id, state="shipped", last_error=None)
    _record_decision(f"{goal_id} shipped to {target} as {merge_sha[:12]}",
                     f"revert path: orchestrator rollback {merge_sha}", goal_id)
    _notify_once(goal_id, "shipped",
                 f"{goal_id} shipped to {target} as {merge_sha}; rollback: orchestrator rollback {merge_sha}")
    return {"status": "shipped", "merge_sha": merge_sha}


def advance(goal_id, pool, root=ROOT, stop_event=None):
    """Drive one goal through the state machine until shipped, held, or stopped. Caller holds ship.lock."""
    cfg = pool.cfg
    target = settings(cfg)["target"]
    try:
        st = _ship(bus.get(goal_id))
        if st.get("state") in ("shipped", "held"):
            return {"status": st["state"]}
        if not st.get("state"):
            st = _ship_update(goal_id, state="pending", attempts=0)
        if not st.get("pr_number"):
            pr = _find_pr(root, goal_id, target)
            if pr:
                st = _ship_update(goal_id, pr_number=pr["number"], pr_url=pr["url"])
                if pr.get("state") == "MERGED" and _merge_oid(pr):
                    return _on_merged(goal_id, root, cfg, target, _merge_oid(pr), stop_event)
        while not _stopped(stop_event):
            st = _ship(bus.get(goal_id))
            if st.get("pr_number"):
                truth = _pr_truth(root, st["pr_number"])
                if truth and truth.get("state") == "MERGED" and _merge_oid(truth):
                    return _on_merged(goal_id, root, cfg, target, _merge_oid(truth), stop_event)
                if truth and truth.get("state") == "CLOSED":
                    raise _Hold("ship PR closed")
            if st.get("state") in ("pending", "gating"):
                _gating(goal_id, root, cfg, target)
            elif st.get("state") == "merging":
                _merging(goal_id, root, cfg, target)
            else:
                return {"status": st.get("state")}
        return {"status": "stopped"}
    except _Hold as h:
        return _hold(goal_id, h.reason, h.tail)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        _ship_update(goal_id, last_error=str(e)[:500])
        print(f"[ship] {goal_id}: {e}", file=sys.stderr)
        return {"status": "error", "error": str(e)[:500]}


def _lock(root):
    path = Path(root) / ".orchestrator" / "ship.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _unlock(fh):
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def candidates():
    rows = [t for t in bus.read() if (t.get("constraints") or {}).get("goal")
            and (t.get("result") or {}).get("goal_closed") and _ship(t).get("state") not in ("shipped", "held")]
    return sorted(rows, key=lambda t: ((t.get("pipeline") or {}).get("closed_at") or t.get("created_at") or 0, t["id"]))


def tick(pool, stop_event=None, root=ROOT, inline=True):
    """Start at most one ship. Inline (daemon --once) blocks; otherwise a non-daemon thread runs it."""
    if not settings(pool.cfg)["enabled"]:
        return None
    if _git(root, "remote", "get-url", "origin").returncode:
        return None
    if any(t.is_alive() for t in _THREADS):
        return None
    rows = candidates()
    if not rows:
        return None
    fh = _lock(root)
    if fh is None:
        return None
    goal_id = rows[0]["id"]

    def run():
        try:
            return advance(goal_id, pool, root=root, stop_event=stop_event)
        finally:
            _unlock(fh)

    if inline:
        return run()
    thread = threading.Thread(target=run, name=f"ship-{goal_id}", daemon=False)
    _THREADS[:] = [t for t in _THREADS if t.is_alive()] + [thread]
    thread.start()
    return {"status": "started", "goal": goal_id, "thread": thread}


def join_threads(timeout=None):
    for thread in list(_THREADS):
        thread.join(timeout)


def retry(goal_id):
    st = _ship(bus.get(goal_id))
    if st.get("state") != "held":
        return {"status": "not_held", "state": st.get("state")}
    _ship_update(goal_id, state="pending", attempts=0, last_error=None, failure_tail="",
                 retries=st.get("retries", 0) + 1)
    return {"status": "released", "goal": goal_id}


def rollback(merge_sha, root=ROOT, cfg=None, force=False):
    """Revert a merge commit on the target through a gated PR. --force skips the gate for emergencies."""
    if cfg is None:
        from .pool import config
        cfg = config()
    target = settings(cfg)["target"]
    _git(root, "fetch", "origin")
    sha = _sha(root, merge_sha)
    first_parent = _git(root, "rev-list", "--first-parent", f"origin/{target}").stdout.split()
    parents = _git(root, "rev-list", "--parents", "-n", "1", sha or merge_sha).stdout.split()
    if not sha or sha not in first_parent:
        return {"status": "refused", "reason": f"{merge_sha} is not on the first-parent chain of origin/{target}"}
    if len(parents) < 3:
        return {"status": "refused", "reason": f"{merge_sha} is not a merge commit"}
    short = sha[:12]
    subject = _git(root, "log", "-1", "--format=%s", sha).stdout.strip()
    branch = f"rollback/{short}"
    wt = _worktree(root, f"rollback-{short}", f"origin/{target}")
    try:
        r = _git(wt, "revert", "-m", "1", "--no-edit", sha)
        if r.returncode:
            _git(wt, "revert", "--abort")
            notify.notify(f"rollback {short}: revert conflict; no PR opened")
            return {"status": "conflict", "tail": (r.stdout + r.stderr)[-2000:]}
        head = _sha(wt, "HEAD")
        push = _git(wt, "push", "origin", f"HEAD:refs/heads/{branch}")
        if push.returncode:
            notify.notify(f"rollback {short}: push failed")
            return {"status": "failed", "reason": "push failed", "tail": push.stderr[-2000:]}
        body = f"Reverts merge {sha} on {target}.\n"
        if force:
            body += "\n--force: the full gate was skipped for an emergency rollback.\n"
        created = _gh(root, "pr", "create", "--head", branch, "--base", target,
                      "--title", f"Revert {subject}"[:200], "--body", body)
        if created.returncode or not created.stdout.strip():
            notify.notify(f"rollback {short}: gh pr create failed")
            return {"status": "failed", "reason": "gh pr create failed", "tail": created.stderr[-2000:]}
        url = created.stdout.strip().splitlines()[-1]
        number = _pr_number(url)
        if not force:
            ok, reason, tail = _gate(root, wt, f"rollback-{short}", cfg)
            if not ok:
                notify.notify(f"rollback {short}: {reason}; revert PR {url} left open, not merged")
                return {"status": "gate_red", "pr_url": url, "tail": tail}
    finally:
        _drop_worktree(root, wt)
    merged = _gh(root, "pr", "merge", str(number), "--merge", "--match-head-commit", head,
                 "--subject", f"Revert {subject}"[:200])
    if merged.returncode:
        notify.notify(f"rollback {short}: gh pr merge failed for {url}")
        return {"status": "failed", "reason": "gh pr merge failed", "pr_url": url, "tail": merged.stderr[-2000:]}
    truth = _pr_truth(root, number) or {}
    revert_sha = _merge_oid(truth)
    _record_decision(f"rolled back {short} on {target}",
                     f"reverted merge {sha} via {url}; revert path: orchestrator rollback {revert_sha or '<revert sha>'}",
                     "rollback")
    notify.notify(f"rolled back {short} on {target} via {url}")
    return {"status": "rolled_back", "pr_url": url, "revert_sha": revert_sha, "forced": force}
