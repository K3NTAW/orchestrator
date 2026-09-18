"""Heartbeat and pipeline driver: requeue running tasks whose process died, notify when both accounts cool >30 min
or a budget trips, and walk every task one stage forward — dispatch -> gate -> review -> merge — so a goal advances
without the Planner in the loop. Timeouts are enforced by the spawner itself (subprocess timeout); this loop only
catches crashes. Every stage stamps `pipeline.<stage>_at` on the task json under the bus lock before it acts, so a
stage runs at most once no matter how often tick() runs."""
import fcntl, json, os, subprocess, sys, threading, time, urllib.request
from pathlib import Path
from . import STATE, bus, executor, handover, merge, planner_runs, spawn
from .pool import Pool, fallback_tier

SPEC_REVIEW_MIN = 6    # complexity at which a spec must be reviewed before an executor sees it
DIRECT_MERGE_MAX = 3   # complexity at or below which hooks are the whole review (CLAUDE.md step 7)
TWO_REVIEWS_FROM = 7   # complexity at which merge waits for two review approvals instead of one
SPEC_REVIEW_TIER = "sonnet"  # tier the spec review worker runs on
# The four constants above are defaults; _load_review_cfg() overwrites them from pool.toml's [review] table
# at the top of every tick() so dispatch()/gate()/merge_reviewed() (which read them as plain module globals,
# not through a Pool argument) always see the current policy without threading pool.cfg through every call.
LOCK_PATH = STATE / "daemon.lock"
HANDOVER_INTERVAL_S = 15 * 60
HANDOVER_STATE = STATE / "handover_state.json"


def _load_review_cfg(pool):
    """Refresh the review-policy thresholds from pool.cfg's [review] table. Missing table or keys fall back to
    the defaults set on the module above -- so a pool.toml without [review] behaves exactly as if it had one
    with these values (§review policy, 2026-09-18: reviews were costing as much as execution)."""
    global SPEC_REVIEW_MIN, DIRECT_MERGE_MAX, TWO_REVIEWS_FROM, SPEC_REVIEW_TIER
    review = pool.cfg.get("review", {})
    SPEC_REVIEW_MIN = review.get("spec_review_min", 6)
    DIRECT_MERGE_MAX = review.get("direct_merge_max", 3)
    TWO_REVIEWS_FROM = review.get("two_reviews_from", 7)
    SPEC_REVIEW_TIER = review.get("spec_review_tier", "sonnet")


def notify(msg):
    print(f"[notify] {msg}", file=sys.stderr)
    url = os.environ.get("ORCH_NOTIFY_URL")
    if url:
        try:
            # msg is untrusted (merge stderr, task titles): capped the same as the osascript arm below so an
            # unbounded blob of git output is never shipped whole to an external webhook.
            req = urllib.request.Request(url, data=msg[:200].encode(), method="POST",
                                          headers={"Content-Type": "text/plain"})
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:
            print(f"[notify] webhook failed: {e}", file=sys.stderr)
    if sys.platform == "darwin" and os.environ.get("ORCH_NOTIFY_DESKTOP") != "0":
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


def stamp(tid, stage, pipeline_fields=None, **fields):
    """Claim one pipeline stage for one task. Returns False when another tick already claimed it. The read of the
    existing stamp and the write of the new one happen under the same bus lock, so two ticks cannot both win.
    pipeline_fields merges extra keys into the same pipeline dict as the stage timestamp (e.g. reviews_expected
    at gate time) so they land atomically with the stamp instead of racing a second bus.update."""
    with bus.locked():
        t = bus.get(tid)
        pipeline = dict(t.get("pipeline") or {})
        if pipeline.get(stage):
            return False
        pipeline[stage] = time.time()
        if pipeline_fields:
            pipeline.update(pipeline_fields)
        bus.update(tid, pipeline=pipeline, **fields)
    return True


def _git_in(worktree, *args):
    return subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True)


def _requeue(tid, pipeline):
    """Put a task back in the queue for dispatch() to retry. Clearing pipeline.dispatched_at is what actually
    makes that retry happen: dispatch()'s stamp() no-ops when the stage is already stamped, so a requeue that
    left dispatched_at in place would leave the task queued forever without a live worker."""
    clean = {k: v for k, v in (pipeline or {}).items() if k != "dispatched_at"}
    bus.update(tid, status="queued", pid=None, reason="process died; requeued", pipeline=clean)
    return "requeued"


def reconcile_dead(t):
    """A running task whose worker died >60s ago: the `claude -p` child (spawn.py Popen) can outlive the daemon
    thread that would have posted its result, finish and commit in its worktree, and leave the task stuck
    "running" with a dead pid. Requeuing unconditionally would redo that finished work on top of the executor's
    own commit (gotchas.md 2026-09-18: T-0115, commit abc3f56 sat in wt/T-0115 while the task went back to
    queued). Only an execute task with a worktree gets the extra check: a clean worktree with commits ahead of
    its base is posted as a done result so gate() re-gates it normally; a dirty one is held for the Planner.
    Everything else (scout/review/etc, or an execute task with no worktree or no commits ahead) requeues as
    before. Returns "requeued" | "regated" | "held" so this is unit-testable without a live pid."""
    tid, worktree = t["id"], t.get("worktree")
    if t.get("role") != "execute" or not worktree or not Path(worktree).is_dir():
        return _requeue(tid, t.get("pipeline"))

    base = None
    parent = t.get("parent")
    if parent:
        r = _git_in(worktree, "merge-base", "HEAD", f"goal/{parent}")
        if r.returncode == 0:
            base = r.stdout.strip()
    # No goal/<parent> branch to merge-base against (parentless task, or the first execute task of a goal that
    # hasn't cut its goal branch yet): fall back to the trunk the worktree was actually cut from.
    if base is None:
        r = _git_in(worktree, "merge-base", "HEAD", "origin/main")
        if r.returncode == 0:
            base = r.stdout.strip()
    if base is None:
        r = _git_in(worktree, "merge-base", "HEAD", "main")
        if r.returncode == 0:
            base = r.stdout.strip()
    if base is None:
        return _requeue(tid, t.get("pipeline"))

    r = _git_in(worktree, "rev-list", "--count", f"{base}..HEAD")
    ahead = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0
    if ahead == 0:
        return _requeue(tid, t.get("pipeline"))

    # --porcelain lists untracked files too, not just modified ones; they count as dirty here as well since
    # holding the task for the Planner is the safe direction when the daemon can't tell what they are.
    status = _git_in(worktree, "status", "--porcelain")
    sha = _git_in(worktree, "rev-parse", "HEAD").stdout.strip()
    if status.stdout.strip():
        bus.update(tid, status="held", hold_reason="orphaned_dirty_worktree", pid=None,
                   resume_hint={"commit": sha, "dirty": status.stdout.splitlines()[:20]})
        notify(f"{tid}: dead worker left a dirty worktree with commit {sha[:8]} ahead of {base[:8]}; held")
        return "held"

    bus.post_result(tid, {
        "summary": f"executor exited without posting a result; daemon found commit {sha} ahead of "
                   f"{base[:8]} in the worktree and re-gated it",
        "commit": sha,
        "executed_by": t.get("executor") or f"claude:{t.get('tier', 'sonnet')}",
        "orphaned": True,
        "provenance": ["repo"],
    }, "done")
    print(f"[daemon] {tid}: dead worker left commit {sha[:8]} ahead of {base[:8]} in the worktree; re-gated",
          file=sys.stderr)
    return "regated"


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


def already_merged(t):
    """True when a done execute task's work already landed in its goal branch even though merged_into is unset.
    merge.merge() only stamps merged_into on whichever task_id it is called with, so a fix-round merge leaves the
    original task done with merged_into unset while its commit is already an ancestor of goal/<parent> (the first
    smoke run created T-0057..T-0062 this way). Detected here by ancestry so the gate and review stages do not
    redo already-merged work; never raises, since a missing git binary or branch just means "not merged"."""
    if t.get("merged_into"):
        return True
    parent = t.get("parent")
    if not parent:
        return False
    branch = f"task/{t['id']}"
    target = f"goal/{parent}"
    try:
        if not spawn.branch_exists(branch):
            return False
        r = spawn.git("merge-base", "--is-ancestor", branch, target, check=False)
    except Exception:
        return False
    if r.returncode == 0:
        bus.update(t["id"], merged_into=target, merged_via="ancestor")
        return True
    return False


def _codex_available(pool):
    """True iff some enabled execute-role executor is not cooling right now. A row that is merely saturated
    (running == max_parallel) still counts as available -- only a cooldown takes it out of the pool -- so a
    queue of ready tasks against four busy-but-healthy rows waits for the next tick instead of spilling into
    the Claude fallback branch below."""
    return any(ex.enabled and "execute" in ex.roles and not ex.cooling() for ex in pool.executors.values())


def _fallback_mode(pool):
    """True iff no execute executor is available at all (every one cooling) and the pool is configured to
    dispatch to a Claude tier instead of holding. pool.cfg is read with .get and the current defaults so a
    pool.toml without a [codex] table can't raise here."""
    return not _codex_available(pool) and pool.cfg.get("codex", {}).get("on_exhausted", "hold") == "fallback_claude"


def free_slots(pool):
    """How many execute dispatches this tick may make: the executor pool's idle parallelism. Bounds tick()'s work
    so a queue of forty ready tasks does not fork forty subprocesses at once.

    Only when every enabled execute executor is cooling (never merely saturated) and codex.on_exhausted ==
    "fallback_claude" does executor.start()'s _exhausted() path (executor.py:110) route tasks to a Claude
    fallback tier instead of holding them -- so the real ceiling then is limits.max_parallel_claude_workers, less
    every in-flight Claude worker of any role (running, assigned_to or executor starting "claude:") and every
    execute task already dispatched this tick but not yet claimed (gotchas.md 2026-09-18: T-0070 needed a hand
    dispatch without this; a later review round found the saturated-vs-cooling conflation fixed above)."""
    codex_slots = sum(max(0, ex.max_parallel - ex.running) for ex in pool.executors.values()
                      if ex.enabled and "execute" in ex.roles and not ex.cooling())
    if not _fallback_mode(pool):
        return codex_slots
    running_claude = sum(1 for t in bus.read(status="running")
                         if (t.get("assigned_to") or "").startswith("claude:")
                         or (t.get("executor") or "").startswith("claude:"))
    inflight_dispatches = sum(1 for t in bus.read(status="queued", role="execute")
                              if (t.get("pipeline") or {}).get("dispatched_at")
                              and not (t.get("pipeline") or {}).get("gated_at"))
    max_workers = pool.cfg.get("limits", {}).get("max_parallel_claude_workers", 4)
    return max(0, max_workers - running_claude - inflight_dispatches)


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
    fallback = _fallback_mode(pool)
    for t in bus.read(status="queued", role="execute"):
        if stale(t) or not bus.ready(t):
            continue
        verdict = t.get("spec_review_verdict")
        if t["complexity"] < SPEC_REVIEW_MIN or verdict == "approve":
            if fallback and fallback_tier(t["complexity"]) is None:
                continue  # no Claude tier for this complexity (9+): wait for Codex instead of being held later
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
                                         complexity=t["complexity"], tier=SPEC_REVIEW_TIER)
                    spawn_async(spawn.run_worker, sr["id"])
                except Exception as e:
                    hold_failed(t["id"], "spec_review_error", "spec_review", e)


def review_tier(t):
    """Never let a model review its own output (CLAUDE.md rule): a task the daemon fell back to a Claude tier for
    (executor.py's _exhausted(), executor field "claude:<tier>") must be reviewed by the other Claude tier, not
    the reviewer's usual sonnet default (gotchas.md 2026-09-18: T-0071 was sonnet-executed and sonnet-reviewed).
    Codex-executed tasks keep the default tier."""
    ex = t.get("executor") or ""
    if ex.startswith("claude:sonnet"):
        return "opus"
    if ex.startswith("claude:opus"):
        return "sonnet"
    return "sonnet"


def _other_tier(tier):
    return "sonnet" if tier == "opus" else "opus"


def reviews_expected(t):
    """How many review approvals an execute task needs before merge_reviewed() may merge it -- the single source
    of truth gate() also uses to decide how many review tasks to open. An orphaned result (reconcile_dead
    re-gating a dead worker's last commit) always needs exactly one, whatever the task's complexity: the orphaned
    warning is what needs the second pair of eyes, not the model split (T-0150 review: an orphaned complexity-7
    task was stuck waiting on a second review gate() never opens)."""
    if (t.get("result") or {}).get("orphaned") or t["complexity"] < TWO_REVIEWS_FROM:
        return 1
    return 2


def gate(pool):
    """done execute tasks that have not been gated: run tests-green on the worktree, then merge (cheap tasks) or
    open the number of review tasks reviews_expected() says (one for complexity between DIRECT_MERGE_MAX and
    TWO_REVIEWS_FROM, or any orphaned result; two otherwise). The second review must never run on the model that
    executed: when the executor is a Claude tier (executor field startswith "claude:"), both reviews run on
    review_tier(t) -- the non-executing tier (both reviews may land on the same account; only the model differs
    from the executor); when the executor is Codex, the second review runs on whichever tier the first one
    didn't get. The successful gate stamp also freezes pipeline.reviews_expected = reviews_expected(t) so a later
    change to the [review] two_reviews_from threshold can't change how many approvals merge_reviewed() waits for
    on a task already past this stage. Filters run cheap-first, already_merged() (which shells out to git) last,
    so a task the other checks would skip anyway never pays for a git call."""
    for t in bus.read(status="done", role="execute"):
        if stale(t) or (t.get("pipeline") or {}).get("gated_at") or not t.get("worktree") or already_merged(t):
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
        if not stamp(t["id"], "gated_at", pipeline_fields={"reviews_expected": reviews_expected(t)}):
            continue
        orphaned = bool((t.get("result") or {}).get("orphaned"))
        try:
            if not orphaned and t["complexity"] <= DIRECT_MERGE_MAX:
                report_merge(t["id"], merge.merge(t["id"]))
                continue
            spec = t["spec"]
            if orphaned:
                # a result with orphaned=true came from reconcile_dead re-gating a dead worker's last commit,
                # not from an executor that actually finished: never let complexity alone route it straight
                # to merge, whatever the task's normal tier would be. One review is enough here regardless of
                # complexity -- the orphaned warning is what needs a second pair of eyes, not the model split.
                spec = ("orphaned executor: verify the acceptance criteria are fully met, the worker may "
                        f"have died mid-task\n\n{spec}")
                r = bus.create_task(f"review: {t['title']}", spec, t["acceptance"], t["scope"], role="review",
                                    inputs=[t["id"]], parent=t.get("parent"), complexity=t["complexity"],
                                    tier=review_tier(t))
                spawn_async(spawn.run_worker, r["id"])
                continue
            # Any status counts here, not just "done": a review that's still queued/running already claims the
            # one (or first of two) slot, so a re-entry must not spawn a duplicate on top of it.
            existing = [x for x in bus.read(role="review") if x["inputs"][:1] == [t["id"]]]
            if not existing:
                r1 = bus.create_task(f"review: {t['title']}", spec, t["acceptance"], t["scope"], role="review",
                                     inputs=[t["id"]], parent=t.get("parent"), complexity=t["complexity"],
                                     tier=review_tier(t))
                spawn_async(spawn.run_worker, r1["id"])
                existing = [r1]
            if reviews_expected(t) == 2 and len(existing) == 1:
                executor_field = t.get("executor") or ""
                if executor_field.startswith("claude:"):
                    tier2 = review_tier(t)
                else:
                    tier2 = _other_tier(existing[0]["tier"])
                r2 = bus.create_task(f"review: {t['title']}", spec, t["acceptance"], t["scope"], role="review",
                                     inputs=[t["id"]], parent=t.get("parent"), complexity=t["complexity"],
                                     tier=tier2)
                spawn_async(spawn.run_worker, r2["id"])
        except Exception as e:
            hold_failed(t["id"], "gated_error", "gate", e)


def report_merge(task_id, r):
    if r.get("status") == "merged":
        notify(f"{task_id} merged into {r['target']} ({r['sha'][:8]})")
    else:                                  # merge.merge already set the task failed with a resume_hint
        notify(f"{task_id} merge failed: {r.get('status')} {r.get('reason', '')}".strip())
    return r


def _review_verdict(r, src, allow_src_fallback):
    """A review task's own review_verdict/result -- not src's -- is the reliable source once a task can carry two
    reviews: spawn.run_worker writes review_verdict onto both the review task and src, so with two reviews the
    second to finish clobbers src's field with its own verdict. Each review's own field is never touched by its
    sibling, so it is checked first; src is only a fallback for older data that predates this field existing on
    r, and only when allow_src_fallback is true -- callers pass that as (len(reviews) == 1), since with two or
    more reviews src's single field cannot speak for more than one of them."""
    v = r.get("review_verdict") or (r.get("result") or {}).get("verdict")
    if v:
        return v
    return src.get("review_verdict") if allow_src_fallback else None


def merge_reviewed(pool):
    """done, unmerged execute tasks with at least one review: merge once reviews_expected() of their reviews have
    approved; anything else about a done review's verdict holds the task for the Planner to write the fix-round
    spec (a daemon must not invent a spec). gated_at is not required -- a hand-gated task the Planner spawned
    reviews for directly still gets swept -- only "has at least one review" gates entry, same as gate() itself
    creating them. Walking execute tasks (not done review tasks) is what closes the escape hatch T-0159 review
    item 1 found: a review that never reaches done -- every review of the task failed or was held -- used to
    never get iterated at all, leaving the task stuck done+gated forever. Every review bucketed by verdict, not
    just "approve" vs "request_changes": done+approve is approved, done with any other verdict (including a
    missing or off-vocabulary one) is rejected, queued/running is pending, failed/held is stuck -- exhaustive
    over the statuses bus tasks can actually carry, so a done review can never fall through every bucket and
    trip an IndexError on an empty stuck list (T-0164 review item 1: a done sibling with an unrecognised verdict
    used to satisfy none of the old checks). needed comes from pipeline.reviews_expected, stamped by gate() at
    gate time, falling back to reviews_expected(t) only for tasks gated before that stamp existed -- so a later
    change to the [review] two_reviews_from threshold cannot move the goalposts on a task already past gate().
    Each task's body runs in its own try/except: a bad review record or an unexpected raise prints one stderr
    line and moves on, so one task can never stop the sweep for the others in the same tick. Filters run
    cheap-first, already_merged() (which shells out to git) last."""
    for t in bus.read(status="done", role="execute"):
        try:
            _merge_reviewed_one(t)
        except Exception as e:
            print(f"[daemon] merge_reviewed {t['id']} failed: {e}", file=sys.stderr)


def _merge_reviewed_one(t):
    if stale(t):
        return
    reviews = [r for r in bus.read(role="review") if r["inputs"][:1] == [t["id"]]]
    if not reviews:
        return  # gate() creates them; nothing to act on yet
    if already_merged(t):
        return
    single = len(reviews) == 1
    approved, rejected, pending, stuck, unknown = [], [], [], [], []
    for r in reviews:
        status = r["status"]
        if status == "done":
            verdict = _review_verdict(r, t, single)
            (approved if verdict == "approve" else rejected).append((r, verdict))
        elif status in ("queued", "running"):
            pending.append(r)
        elif status in ("failed", "held"):
            stuck.append(r)
        else:
            unknown.append(r)  # should be impossible: every bus status is one of the above
    if rejected:
        r, verdict = rejected[0]
        reason = f"review request_changes: {r['id']} ({verdict})"
        if stamp(t["id"], "review_held_at", status="held", hold_reason=reason):
            notify(f"{t['id']}: {reason}")
        return
    needed = (t.get("pipeline") or {}).get("reviews_expected")
    if needed is None:
        needed = reviews_expected(t)
    if len(approved) >= needed:
        # stamped on the source task before the merge, not after: a conflict leaves merged_into unset, and
        # retrying it every tick would just rebuild the same conflict; stamping here means a task with two
        # reviews attempts the merge exactly once no matter which review finishes last
        if stamp(t["id"], "merged_at"):
            try:
                report_merge(t["id"], merge.merge(t["id"]))
            except Exception as e:
                hold_failed(t["id"], "merged_error", "merge", e)
        return
    if len(approved) + len(pending) >= needed:
        return  # a still-live sibling could yet supply the missing approval(s); keep waiting
    if stuck:
        if approved:
            first = stuck[0]
            reason = f"review {first['status']}: {first['id']}"
        else:
            reason = f"reviews failed: {', '.join(sorted(r['id'] for r in stuck))}"
    else:
        reason = f"review state unknown: {', '.join(sorted(r['id'] for r in unknown))}"
    if stamp(t["id"], "review_held_at", status="held", hold_reason=reason):
        notify(f"{t['id']}: {reason}")


def _handover_last_at():
    try:
        return json.loads(HANDOVER_STATE.read_text()).get("handover_last_at", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def _save_handover_last_at(now):
    """Read-modify-write .orchestrator/handover_state.json under an flock on the file itself, the same pattern
    as pool._save_planner_account -- so a concurrent tick (another thread, or another daemon process briefly
    racing the lock file) can't clobber this timestamp with a stale read."""
    HANDOVER_STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(HANDOVER_STATE, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}
            data["handover_last_at"] = now
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(data, indent=1))
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def maybe_handover(reason, now=None):
    """Write the auto-handover section at most once every HANDOVER_INTERVAL_S, so plan.md is never staler than
    that even when the Planner is gone. The throttle timestamp lives in its own handover_state.json, not
    pool_state.json, so a handover tick never races Pool.save()'s full read-modify-write of the pool's own
    state."""
    now = now if now is not None else time.time()
    if now - _handover_last_at() < HANDOVER_INTERVAL_S:
        return False
    try:
        handover.write(reason)
    except Exception as e:
        print(f"[daemon] handover failed: {e}", file=sys.stderr)
        return False
    _save_handover_last_at(now)
    return True


def tick(pool=None):
    pool = pool or Pool()
    try:
        pool.tally_planner()
    except Exception as e:
        print(f"[daemon] tally_planner failed: {e}", file=sys.stderr)
    _load_review_cfg(pool)
    for t in bus.read(status="running"):
        if t.get("pid") and not alive(t["pid"]) and time.time() - t.get("claimed_at", 0) > 60:
            try:
                reconcile_dead(t)
            except Exception as e:
                print(f"[daemon] reconcile {t['id']} failed: {e}", file=sys.stderr)
                continue
    for stage in (dispatch, gate, merge_reviewed):
        try:
            stage(pool)
        except Exception as e:
            print(f"[daemon] {stage.__name__} failed: {e}", file=sys.stderr)
    if pool.cfg.get("planner", {}).get("autonomous", False):
        try:
            planner_runs.reconcile()
            for goal_id, kind, payload_key in planner_runs.decision_points():
                planner_runs.run(goal_id, kind, payload_key)
                break  # at most one autonomous Planner launch per tick
        except Exception as e:
            print(f"[daemon] planner_runs failed: {e}", file=sys.stderr)
    m = pool.both_cooling_minutes()
    if m > 30:
        notify(f"both Claude accounts cooling for {m:.0f} more min")
    for a in pool.accounts:
        if a.daily_budget and a.day_tokens >= a.daily_budget:
            notify(f"account {a.id} hit its daily budget; tasks held")
    if not pool.codex_available() and pool.codex.cooling():
        notify("Executor (Codex) cooling; execute tasks held, refill the pipeline")
    maybe_handover("daemon tick")


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
