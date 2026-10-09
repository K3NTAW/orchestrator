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

Target sha: origin/<target> is read once, right after the fetch, and that exact sha is merged and stored as
gated_target_sha. The daemon fetches the same repo from another thread, so re-reading the ref later could name a
tree that was never gated.

Residual window: origin/<target> can move between the final fetch and gh pr merge. After the merge the merge
commit's first parent is compared with gated_target_sha; on a mismatch the full gate re-runs on the merge commit,
and a red result rolls the merge back at once and holds the goal. Between that merge and the rollback, the target
holds an ungated tree. A red post-merge gate never ships: the rollback state is kept in pipeline.ship and a resume
runs it if it never finished; an unresolved first parent holds instead of skipping the check.

Window: tick() stores ship_enabled_at in .orchestrator/ship_state.json the first time it sees [ship].enabled true
and deletes it when ship is seen disabled. Goals closed before that time are never touched.

Ownership: automatic rollback only reverts a merge ship made itself (pipeline.ship.merged_by == "ship"). A goal
merged by a human or another path is recorded merged_by="external"; a red post-merge gate on it only notifies."""
import fcntl, json, os, re, subprocess, sys, threading, time
from pathlib import Path

from . import ROOT, bus, gate, merge, notify

ACTIVE = ("pending", "gating", "merging")
MAX_ERRORS = 3
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
_THREADS = []


class _Hold(Exception):
    def __init__(self, reason, tail=""):
        super().__init__(reason)
        self.reason, self.tail = reason, tail


class _FetchError(OSError):
    """A failed git fetch is usually transient: it counts toward MAX_ERRORS instead of holding at once."""


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


def _fetch(root):
    r = _git(root, "fetch", "origin")
    if r.returncode:
        raise _FetchError(f"git fetch failed: {(r.stdout + r.stderr)[-400:].strip()}")
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


def _state_path(root):
    return Path(root) / ".orchestrator" / "ship_state.json"


def enabled_at(root=ROOT):
    """When ship was last seen switched on, or None if it is off or was never seen on."""
    try:
        return float(json.loads(_state_path(root).read_text())["enabled_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _track_enabled(root, enabled):
    """Persist ship_enabled_at on the first enabled tick; forget it when disabled so off-then-on opens a new window."""
    path = _state_path(root)
    if not enabled:
        path.unlink(missing_ok=True)
        return None
    at = enabled_at(root)
    if at is None:
        at = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"enabled_at": at}) + "\n")
        os.replace(tmp, path)
    return at


def closed_time(task):
    """pipeline.closed_at, else the time of the event that set goal_closed, else updated_at, else the last event."""
    closed = (task.get("pipeline") or {}).get("closed_at")
    if closed:
        return closed
    events = [e for e in task.get("events") or [] if isinstance(e, dict)]
    for e in events:
        if isinstance(e.get("result"), dict) and e["result"].get("goal_closed") and e.get("ts"):
            return e["ts"]
    return task.get("updated_at") or (events[-1].get("ts") if events else None) or 0


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
    """<root>/wt/ship-<name>: the same depth as task worktrees, so tests that derive the repo root from __file__
    resolve it the same way in both."""
    base = Path(root) / "wt"
    base.mkdir(parents=True, exist_ok=True)
    wt = base / f"ship-{name}"
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


def _goal_head(root, goal_id):
    """The newer of goal/<id> and origin/goal/<id>; the remote wins unless the local branch is ahead of it."""
    local, remote = _sha(root, f"goal/{goal_id}"), _sha(root, f"origin/goal/{goal_id}")
    if remote and (not local or _git(root, "merge-base", "--is-ancestor", local, remote).returncode == 0):
        return remote
    return local


def _covers(root, goal_id, pr):
    """A merged PR ships the goal only if its head contains the current goal head."""
    head, goal_head = (pr or {}).get("headRefOid"), _goal_head(root, goal_id)
    return bool(head and goal_head) and _git(root, "merge-base", "--is-ancestor", goal_head, head).returncode == 0


def _in_target(root, goal_id, target):
    """The goal head is already an ancestor of origin/<target>: nothing left to ship."""
    head, target_sha = _goal_head(root, goal_id), _sha(root, f"origin/{target}")
    return bool(head and target_sha) and _git(root, "merge-base", "--is-ancestor", head, target_sha).returncode == 0


def _already_in_target(goal_id, target):
    _ship_update(goal_id, state="shipped", shipped_reason="already in target", merged_by="external",
                 last_error=None, errors=0)
    _notify_once(goal_id, "shipped", f"{goal_id}: already in {target}; marked shipped without a push or PR")
    return {"status": "shipped", "reason": "already in target"}


def _find_pr(root, goal_id, target):
    """A covering MERGED PR, else an OPEN one, else a CLOSED one (callers hold on it), else None."""
    r = _gh(root, "pr", "list", "--head", f"goal/{goal_id}", "--base", target, "--state", "all",
            "--json", "number,state,url,mergeCommit,headRefOid")
    if r.returncode:
        raise _Hold("gh pr list failed", r.stderr[-2000:])
    prs = json.loads(r.stdout or "[]")
    merged = [p for p in prs if p.get("state") == "MERGED" and _covers(root, goal_id, p)]
    return (merged or [p for p in prs if p.get("state") == "OPEN"]
            or [p for p in prs if p.get("state") == "CLOSED"] or [None])[0]


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
    """Gate the merged tree in a local worktree; push it and open the PR only after the gate is green."""
    st = _ship_update(goal_id, state="gating", gate_ok=False)
    _fetch(root)
    target_sha = _sha(root, f"origin/{target}")
    if not target_sha:
        raise _Hold("ship target missing")
    if _in_target(root, goal_id, target):
        return _already_in_target(goal_id, target)
    has_remote = _sha(root, f"origin/goal/{goal_id}")
    base = _goal_head(root, goal_id)
    if not base:
        raise _Hold("ship goal branch missing")
    wt = _worktree(root, goal_id, base)
    try:
        r = _git(wt, "merge", "--no-edit", "-m", f"Merge {target} {target_sha[:12]} into goal/{goal_id}", target_sha)
        if r.returncode:
            _git(wt, "merge", "--abort")
            raise _Hold("ship conflict", (r.stdout + r.stderr)[-2000:])
        head = _sha(wt, "HEAD")
        if has_remote and _git(wt, "merge-base", "--is-ancestor", has_remote, "HEAD").returncode:
            raise _Hold("ship push not fast-forward")
        ok, reason, tail = _gate(root, wt, goal_id, cfg)
        if not ok:
            raise _Hold(reason, tail)
        push = _git(wt, "push", "origin", f"HEAD:refs/heads/goal/{goal_id}")
        if push.returncode:
            raise _Hold("ship push failed", push.stderr[-2000:])
        if not st.get("pr_number"):
            pr = _find_pr(root, goal_id, target)
            if pr and pr.get("state") == "CLOSED":
                raise _Hold("ship PR closed")
            pr = pr or _create_pr(root, bus.get(goal_id), target)
            _ship_update(goal_id, pr_number=pr["number"], pr_url=pr["url"])
        _ship_update(goal_id, gated_head_sha=head, gated_target_sha=target_sha, gate_ok=True)
    finally:
        _drop_worktree(root, wt)
    _ship_update(goal_id, state="merging", errors=0)
    return None


def _merging(goal_id, root, cfg, target):
    st = _ship(bus.get(goal_id))
    _fetch(root)
    if _sha(root, f"origin/{target}") != st.get("gated_target_sha"):
        attempts = st.get("attempts", 0) + 1
        if attempts > settings(cfg)["max_regates"]:
            _ship_update(goal_id, attempts=attempts)
            raise _Hold("ship target kept moving")
        _ship_update(goal_id, state="gating", attempts=attempts)
        return
    if not (st.get("gate_ok") and st.get("gated_head_sha") and st.get("gated_target_sha")) or _git(root, "merge-base", "--is-ancestor", st["gated_target_sha"],
                                            st["gated_head_sha"]).returncode:
        raise _Hold("ship gated head does not contain gated target")
    title = bus.get(goal_id).get("title", "")[:150]
    n = st["pr_number"]
    r = _gh(root, "pr", "merge", str(n), "--merge", "--match-head-commit", st["gated_head_sha"],
            "--subject", f"Merge PR {n}: {title} (goal/{goal_id}, auto-merged after green full gate)")
    if (_pr_truth(root, n) or {}).get("state") != "MERGED":
        if r.returncode:
            raise _Hold("ship merge failed", r.stderr[-2000:])
        # Exit 0 without a merge: a merge queue or auto-merge took the PR. Nothing here can wait on it.
        raise _Hold("merge pending", r.stdout[-2000:])
    # A crash before this line leaves merged_by unset: the merge then counts as external and is never rolled back.
    _ship_update(goal_id, merged_by="ship")


def _merged_rollback(root, merge_sha, target):
    """The rollback/<short> PR a previous, interrupted rollback already got merged, or None."""
    r = _gh(root, "pr", "list", "--head", f"rollback/{merge_sha[:12]}", "--base", target, "--state", "all",
            "--json", "number,state,url,mergeCommit")
    if r.returncode:
        return None
    merged = [p for p in json.loads(r.stdout or "[]") if p.get("state") == "MERGED"]
    return merged[0] if merged else None


def _roll_back_red(goal_id, root, cfg, merge_sha, tail=""):
    """post_merge_gate is red: run the rollback unless one already finished, then hold. Never ships."""
    rb = _ship(bus.get(goal_id)).get("rollback") or {}
    if rb.get("status") == "running":
        done = _merged_rollback(root, merge_sha, settings(cfg)["target"])
        if done:
            rb = {"status": "rolled_back", "merge_sha": merge_sha, "pr_url": done.get("url"),
                  "revert_sha": _merge_oid(done), "resumed": True}
            _ship_update(goal_id, rollback=rb)
    if rb.get("status") in (None, "running"):
        _ship_update(goal_id, rollback={"status": "running", "merge_sha": merge_sha})
        rb = rollback(merge_sha, root=root, cfg=cfg)
        _ship_update(goal_id, rollback=rb)
    if rb.get("status") == "rolled_back":
        return _hold(goal_id, "post-merge gate red", tail)
    return _hold(goal_id, "post-merge gate red, rollback failed", tail or rb.get("tail", ""))


def _on_merged(goal_id, root, cfg, target, merge_sha, stop_event=None, head_oid=None):
    """A post-merge gate runs unless the PR merged exactly the green gated head onto the gated target.
    Only a merge ship made itself is rolled back on red; an external merge is recorded shipped and only notifies."""
    st = _ship(bus.get(goal_id))
    ours = st.get("merged_by") == "ship"
    st = _ship_update(goal_id, merge_sha=merge_sha, merged_by="ship" if ours else "external")
    if st.get("post_merge_gate") == "red" and ours:
        return _roll_back_red(goal_id, root, cfg, merge_sha)
    fetched = _git(root, "fetch", "origin")
    first_parent = _sha(root, f"{merge_sha}^1")
    if st.get("gated_target_sha") and not first_parent:
        raise _Hold("ship merge parent unresolved", (fetched.stdout + fetched.stderr)[-2000:])
    gated = bool(st.get("gate_ok") and st.get("gated_head_sha") and head_oid == st["gated_head_sha"]
                 and first_parent == st.get("gated_target_sha"))
    if not gated and not st.get("post_merge_gate"):
        wt = _worktree(root, f"post-{goal_id}", merge_sha)
        try:
            ok, _reason, tail = _gate(root, wt, goal_id, cfg)
        finally:
            _drop_worktree(root, wt)
        _ship_update(goal_id, post_merge_gate="green" if ok else "red", failure_tail="" if ok else tail)
        if not ok and ours:
            return _roll_back_red(goal_id, root, cfg, merge_sha, tail)
    if not ours and _ship(bus.get(goal_id)).get("post_merge_gate") == "red":
        _ship_update(goal_id, state="shipped", last_error="post-merge gate red on an external merge")
        _notify_once(goal_id, "external-red",
                     f"{goal_id}: merged into {target} outside ship as {merge_sha}; post-merge gate red, "
                     f"no automatic rollback. Revert by hand if needed: orchestrator rollback {merge_sha}")
        return {"status": "shipped", "merge_sha": merge_sha, "merged_by": "external", "post_merge_gate": "red"}
    _ship_update(goal_id, state="shipped", last_error=None, errors=0)
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
            _git(root, "fetch", "origin")
            pr = _find_pr(root, goal_id, target)
            if pr and pr.get("state") == "MERGED" and _merge_oid(pr):
                _ship_update(goal_id, pr_number=pr["number"], pr_url=pr["url"])
                return _on_merged(goal_id, root, cfg, target, _merge_oid(pr), stop_event, pr.get("headRefOid"))
            if _in_target(root, goal_id, target):
                return _already_in_target(goal_id, target)
            if pr and pr.get("state") == "CLOSED":
                # A human closed it: never replace it with a new PR that would merge unattended.
                raise _Hold("ship PR closed")
            if pr:
                st = _ship_update(goal_id, pr_number=pr["number"], pr_url=pr["url"])
        while not _stopped(stop_event):
            st = _ship(bus.get(goal_id))
            if st.get("post_merge_gate") == "red" and st.get("merge_sha") and st.get("merged_by") == "ship":
                return _on_merged(goal_id, root, cfg, target, st["merge_sha"], stop_event)
            if st.get("pr_number"):
                truth = _pr_truth(root, st["pr_number"])
                if truth and truth.get("state") == "MERGED" and _merge_oid(truth):
                    if _covers(root, goal_id, truth):
                        return _on_merged(goal_id, root, cfg, target, _merge_oid(truth), stop_event,
                                          truth.get("headRefOid"))
                    # The goal moved on after this PR merged: ship the rest through a new PR.
                    _ship_update(goal_id, state="gating", pr_number=None, pr_url=None, post_merge_gate=None,
                                 merge_sha=None, merged_by=None)
                    continue
                if truth and truth.get("state") == "CLOSED":
                    raise _Hold("ship PR closed")
            if st.get("state") in ("pending", "gating"):
                done = _gating(goal_id, root, cfg, target)
                if done:
                    return done
            elif st.get("state") == "merging":
                _merging(goal_id, root, cfg, target)
            else:
                return {"status": st.get("state")}
        return {"status": "stopped"}
    except _Hold as h:
        return _hold(goal_id, h.reason, h.tail)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        errors = _ship(bus.get(goal_id)).get("errors", 0) + 1
        _ship_update(goal_id, last_error=str(e)[:500], errors=errors)
        print(f"[ship] {goal_id}: {e}", file=sys.stderr)
        if errors >= MAX_ERRORS:
            return _hold(goal_id, f"ship failed {MAX_ERRORS} times in a row", str(e))
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


def candidates(root=ROOT, since=None):
    """Closed goals not yet shipped or held, closed at or after ship_enabled_at; nothing before ship was seen on."""
    since = enabled_at(root) if since is None else since
    if since is None:
        return []
    rows = [t for t in bus.read() if (t.get("constraints") or {}).get("goal")
            and (t.get("result") or {}).get("goal_closed") and _ship(t).get("state") not in ("shipped", "held")
            and closed_time(t) >= since]
    return sorted(rows, key=lambda t: (closed_time(t), t["id"]))


def tick(pool, stop_event=None, root=ROOT, inline=True):
    """Start at most one ship. Inline (daemon --once) blocks; otherwise a non-daemon thread runs it."""
    if not settings(pool.cfg)["enabled"]:
        _track_enabled(root, False)
        return None
    since = _track_enabled(root, True)
    if _git(root, "remote", "get-url", "origin").returncode:
        return None
    if any(t.is_alive() for t in _THREADS):
        return None
    if not inline and not threading.main_thread().is_alive():
        return None  # interpreter shutdown: a new non-daemon thread would keep the process alive for hours
    rows = candidates(root=root, since=since)
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
    if st.get("merge_sha") and st.get("post_merge_gate") == "red":
        return {"status": "refused", "reason": "PR merged with a red post-merge gate; roll back by hand"}
    _ship_update(goal_id, state="pending", attempts=0, errors=0, last_error=None, failure_tail="",
                 retries=st.get("retries", 0) + 1)
    return {"status": "released", "goal": goal_id}


def rollback(merge_sha, root=ROOT, cfg=None, force=False):
    """Revert a merge commit on the target through a gated PR. --force skips the gate for emergencies.
    The gate runs on the revert merged with the target as fetched; if the target moved by merge time, the revert
    branch takes the new target and is gated once more; still moving leaves the PR open."""
    if not isinstance(merge_sha, str) or not SHA_RE.fullmatch(merge_sha):
        return {"status": "refused", "reason": f"{merge_sha!r} is not a commit sha"}
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
    base = _sha(root, f"origin/{target}")
    wt = _worktree(root, f"rollback-{short}", base)
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
        for regate in range(0 if force else 2):
            ok, reason, tail = _gate(root, wt, f"rollback-{short}", cfg)
            if not ok:
                notify.notify(f"rollback {short}: {reason}; revert PR {url} left open, not merged")
                return {"status": "gate_red", "pr_url": url, "tail": tail}
            fetched = _git(root, "fetch", "origin")
            now = _sha(root, f"origin/{target}")
            if fetched.returncode or not now:
                notify.notify(f"rollback {short}: fetch failed; revert PR {url} left open, not merged")
                return {"status": "failed", "reason": "fetch failed", "pr_url": url, "tail": fetched.stderr[-2000:]}
            if now == base:
                break
            if regate:
                notify.notify(f"rollback {short}: {target} kept moving; revert PR {url} left open, not merged")
                return {"status": "target_moved", "pr_url": url}
            r = _git(wt, "merge", "--no-edit", "-m", f"Merge {target} {now[:12]} into {branch}", now)
            if r.returncode:
                _git(wt, "merge", "--abort")
                notify.notify(f"rollback {short}: {target} moved and conflicts; revert PR {url} left open")
                return {"status": "conflict", "pr_url": url, "tail": (r.stdout + r.stderr)[-2000:]}
            head, base = _sha(wt, "HEAD"), now
            push = _git(wt, "push", "origin", f"HEAD:refs/heads/{branch}")
            if push.returncode:
                notify.notify(f"rollback {short}: push failed; revert PR {url} left open")
                return {"status": "failed", "reason": "push failed", "pr_url": url, "tail": push.stderr[-2000:]}
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
