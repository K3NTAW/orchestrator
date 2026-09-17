"""Heartbeat and pipeline driver: requeue running tasks whose process died, notify when both accounts cool >30 min
or a budget trips, and walk every task one stage forward — dispatch -> gate -> review -> merge — so a goal advances
without the Planner in the loop. Timeouts are enforced by the spawner itself (subprocess timeout); this loop only
catches crashes. Every stage stamps `pipeline.<stage>_at` on the task json under the bus lock before it acts, so a
stage runs at most once no matter how often tick() runs."""
import fcntl, os, subprocess, sys, threading, time
from pathlib import Path
from . import STATE, bus, executor, merge, spawn
from .pool import Pool

SPEC_REVIEW_MIN = 5   # complexity at which a spec must be reviewed before an executor sees it
DIRECT_MERGE_MAX = 3  # complexity at or below which hooks are the whole review (CLAUDE.md step 7)
LOCK_PATH = STATE / "daemon.lock"


def notify(msg):
    print(f"[notify] {msg}", file=sys.stderr)
    if sys.platform == "darwin":
        # msg is untrusted (merge stderr, task titles): passed as an argv item, never interpolated into the
        # AppleScript source, so a quote in it cannot break out and run arbitrary local commands.
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        'display notification (item 1 of argv) with title "orchestrator"', "-e", "end run",
                        "--", msg[:200]], check=False)


def alive(pid):
    try:
        os.kill(pid, 0); return True
    except (OSError, TypeError):
        return False


def stamp(tid, stage, **fields):
    """Claim one pipeline stage for one task. Returns False when another tick already claimed it. The read of the
    existing stamp and the write of the new one happen under the same bus lock, so two ticks cannot both win."""
    with bus.locked():
        t = bus.get(tid)
        pipeline = dict(t.get("pipeline") or {})
        if pipeline.get(stage):
            return False
        pipeline[stage] = time.time()
        bus.update(tid, pipeline=pipeline, **fields)
    return True


def stale(t):
    """True for a task the daemon must not act on: one whose parent goal task exists and is already done (the goal
    closed and dispatching or merging into it would just redo a re-merge or a notify nobody asked for), or a
    parentless task that is not itself a queued execute task (top-level goals are containers, never work items)."""
    parent = t.get("parent")
    if parent:
        try:
            return bus.get(parent).get("status") == "done"
        except KeyError:
            return False
    return not (t["role"] == "execute" and t["status"] == "queued")


def free_slots(pool):
    """How many execute dispatches this tick may make: the executor pool's idle parallelism. Bounds tick()'s work
    so a queue of forty ready tasks does not fork forty subprocesses at once."""
    return sum(max(0, ex.max_parallel - ex.running) for ex in pool.executors.values()
               if ex.enabled and "execute" in ex.roles and not ex.cooling())


def spawn_async(fn, *args):
    """Fire a side-effecting call (executor.start, spawn.run_worker) in a background thread so tick() never
    blocks on a slow subprocess."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def hold_failed(tid, error_key, stage_label, exc):
    """A stage's side effect raised: hold the task with a short reason and an error stamp instead of leaving it
    wedged at a stamped-but-never-acted-on stage."""
    with bus.locked():
        t = bus.get(tid)
        pipeline = dict(t.get("pipeline") or {})
        pipeline[error_key] = str(exc)[:300]
        bus.update(tid, status="held", hold_reason=f"{stage_label} failed: {type(exc).__name__}", pipeline=pipeline)
    notify(f"{tid}: {stage_label} failed: {exc}")


def _dispatch_worker(task_id, prompt):
    try:
        executor.start(task_id, prompt)
    except Exception as e:
        with bus.locked():
            t = bus.get(task_id)
            pipeline = dict(t.get("pipeline") or {})
            pipeline["dispatch_error"] = str(e)[:300]
            bus.update(task_id, pipeline=pipeline)


def dispatch(pool):
    """queued execute tasks whose dependencies are merged: hand to the executor, or route through spec review first."""
    slots = free_slots(pool)
    for t in bus.read(status="queued", role="execute"):
        if stale(t) or not bus.ready(t):
            continue
        verdict = t.get("spec_review_verdict")
        if t["complexity"] < SPEC_REVIEW_MIN or verdict == "approve":
            if slots <= 0:
                break
            if stamp(t["id"], "dispatched_at"):
                slots -= 1
                prompt = spawn.render("execute", spec=t["spec"], acceptance=t["acceptance"], scope=t["scope"])
                spawn_async(_dispatch_worker, t["id"], prompt)
        elif verdict == "request_changes":
            if stamp(t["id"], "spec_review_held_at", status="held", hold_reason="spec_review request_changes"):
                notify(f"{t['id']}: spec review asked for changes; re-spec it")
        elif not any(r["inputs"][:1] == [t["id"]] for r in bus.read(role="spec_review")):
            if stamp(t["id"], "spec_review_at"):
                try:
                    sr = bus.create_task(f"spec review: {t['title']}", t["spec"], t["acceptance"], t["scope"],
                                         role="spec_review", inputs=[t["id"]], parent=t.get("parent"),
                                         complexity=t["complexity"])
                    spawn_async(spawn.run_worker, sr["id"])
                except Exception as e:
                    hold_failed(t["id"], "spec_review_error", "spec_review", e)


def gate(pool):
    """done execute tasks that have not been gated: run tests-green on the worktree, then merge (cheap tasks) or
    open a review task (everything else)."""
    for t in bus.read(status="done", role="execute"):
        if stale(t) or t.get("merged_into") or (t.get("pipeline") or {}).get("gated_at") or not t.get("worktree"):
            continue
        if not Path(t["worktree"]).exists():
            if stamp(t["id"], "gated_at", status="held", hold_reason="worktree missing"):
                notify(f"{t['id']}: worktree missing; held")
            continue
        tg = subprocess.run([str(merge.TESTS_GREEN), t["worktree"]], capture_output=True, text=True, input="{}")
        if tg.returncode:
            if stamp(t["id"], "gated_at", status="held", hold_reason="gate_red",
                     resume_hint={"failures": tg.stderr[-4000:]}):
                notify(f"{t['id']}: tests red at the gate; held")
            continue
        if not stamp(t["id"], "gated_at"):
            continue
        try:
            if t["complexity"] <= DIRECT_MERGE_MAX:
                report_merge(t["id"], merge.merge(t["id"]))
            else:
                r = bus.create_task(f"review: {t['title']}", t["spec"], t["acceptance"], t["scope"], role="review",
                                    inputs=[t["id"]], parent=t.get("parent"), complexity=t["complexity"])
                spawn_async(spawn.run_worker, r["id"])
        except Exception as e:
            hold_failed(t["id"], "gated_error", "gate", e)


def report_merge(task_id, r):
    if r.get("status") == "merged":
        notify(f"{task_id} merged into {r['target']} ({r['sha'][:8]})")
    else:                                  # merge.merge already set the task failed with a resume_hint
        notify(f"{task_id} merge failed: {r.get('status')} {r.get('reason', '')}".strip())
    return r


def merge_reviewed(pool):
    """done review tasks: approve -> serial merge of the reviewed task; request_changes -> hold it for the Planner,
    which writes the fix-round spec (a daemon must not invent a spec)."""
    for r in bus.read(status="done", role="review"):
        if stale(r) or not (r.get("inputs") and isinstance(r["inputs"][0], str)):
            continue
        try:
            src = bus.get(r["inputs"][0])
        except KeyError:
            continue
        if src.get("merged_into"):
            continue
        verdict = src.get("review_verdict") or r.get("review_verdict")
        if verdict == "approve":
            # stamped before the merge, not after: a conflict leaves merged_into unset, and retrying it every tick
            # would just rebuild the same conflict
            if stamp(r["id"], "merged_at"):
                try:
                    report_merge(src["id"], merge.merge(src["id"]))
                except Exception as e:
                    hold_failed(src["id"], "merged_error", "merge", e)
        elif verdict == "request_changes":
            if stamp(src["id"], "review_held_at", status="held", hold_reason="review request_changes"):
                notify(f"{src['id']}: review asked for changes; Planner writes the fix round")


def tick(pool=None):
    pool = pool or Pool()
    for t in bus.read(status="running"):
        if t.get("pid") and not alive(t["pid"]) and time.time() - t.get("claimed_at", 0) > 60:
            bus.update(t["id"], status="queued", pid=None, reason="process died; requeued")
    for stage in (dispatch, gate, merge_reviewed):
        try:
            stage(pool)
        except Exception as e:
            print(f"[daemon] {stage.__name__} failed: {e}", file=sys.stderr)
    m = pool.both_cooling_minutes()
    if m > 30:
        notify(f"both Claude accounts cooling for {m:.0f} more min")
    for a in pool.accounts:
        if a.daily_budget and a.day_tokens >= a.daily_budget:
            notify(f"account {a.id} hit its daily budget; tasks held")
    if not pool.codex_available() and pool.codex.cooling():
        notify("Executor (Codex) cooling; execute tasks held, refill the pipeline")


def acquire_lock():
    """Non-blocking single-instance lock on STATE/daemon.lock. Returns the open file handle (keep it referenced
    for the daemon's lifetime; closing it or letting it get garbage-collected releases the flock), or None when
    another daemon already holds it."""
    STATE.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _loop(interval, stop_event):
    """A fresh Pool() per tick: cooldowns and running counts are written by the spawned workers, so a long-lived
    Pool would dispatch against minutes-old state. stop_event.wait as the sleep so a caller can interrupt it
    instead of blocking for a full interval."""
    while True:
        try:
            tick(Pool())
        except Exception as e:
            print(f"[daemon] tick failed: {e}", file=sys.stderr)
        if stop_event.wait(interval):
            return


def start_background(cfg, env=os.environ):
    """Called once from the orchestrator MCP server. None (no thread started) when [daemon].autostart is false,
    ORCH_DAEMON=0 overrides it, or another daemon (CLI or a previous autostart) already holds the lock; otherwise
    a live daemon Thread that keeps the lock until stop_background() (tests) or process exit."""
    if not (cfg.get("daemon") or {}).get("autostart", False):
        return None
    if env.get("ORCH_DAEMON") == "0":
        return None
    lock = acquire_lock()
    if lock is None:
        return None
    interval = (cfg.get("daemon") or {}).get("interval_s", 30)
    stop_event = threading.Event()

    def run():
        try:
            _loop(interval, stop_event)
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                lock.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.stop_event = stop_event
    thread.start()
    print(f"[daemon] autostarted (pid {os.getpid()})", file=sys.stderr)
    return thread


def stop_background(thread, timeout=5):
    """Test helper: signal a thread started by start_background() to stop and wait for it, which releases the
    lock so a later start_background() call in the same process can take it again."""
    if thread is None:
        return
    thread.stop_event.set()
    thread.join(timeout)


def main(interval=30, once=False):
    if once:
        tick(Pool())
        return
    lock = acquire_lock()
    if lock is None:
        print("[daemon] another instance already holds the lock; exiting", file=sys.stderr)
        sys.exit(1)
    try:
        _loop(interval, threading.Event())
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
