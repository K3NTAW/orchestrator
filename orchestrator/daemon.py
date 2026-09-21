"""Heartbeat and pipeline driver: requeue running tasks whose process died, notify when both accounts cool >30 min
or a budget trips, and walk every task one stage forward — dispatch -> gate -> review -> merge — so a goal advances
without the Planner in the loop. Timeouts are enforced by the spawner itself (subprocess timeout); this loop only
catches crashes. Every stage stamps `pipeline.<stage>_at` on the task json under the bus lock before it acts, so a
stage runs at most once no matter how often tick() runs."""
import fcntl, fnmatch, hashlib, inspect, json, os, re, subprocess, sys, threading, time, urllib.request
from pathlib import Path
from . import STATE, acceptance, bus, critical_path, decision, executor, handover, jev_route, merge, planner_runs, spawn
from .pool import Pool, fallback_tier
from . import failures, gitutil, interference, schedlog, notify as notifications
from .failures import (root, lineage, _valid_test_id, _test_id_candidates, _test_ids_with_rejections,
                       _test_ids, _path_in_scope, _rejecting_reviews, _normal_issue, failure_signature,
                       _failure_text, _node_id_to_unittest, _RunnerProbeTimeout, _flaky_rerun_command,
                       failure_kind, _review_verdict)
from .gitutil import _git_in, _resolve_base

notify = notifications._deliver
notifications._sink = lambda msg: notify(msg)


def changed_paths(task):
    return gitutil.changed_paths(task, git=_git_in, resolve_base=_resolve_base)


def _added_diff_lines(task):
    return gitutil._added_diff_lines(task, git=_git_in, resolve_base=_resolve_base)


SPEC_REVIEW_MIN = 6    # complexity at which a spec must be reviewed before an executor sees it
DIRECT_MERGE_MAX = 3   # complexity at or below which hooks are the whole review (CLAUDE.md step 7)
TWO_REVIEWS_FROM = 7   # complexity at which merge waits for two review approvals instead of one
SPEC_REVIEW_TIER = "sonnet"  # tier the spec review worker runs on
# Default globs, mirrored from the committed pool.toml (§review policy, 2026-09-19 decision): kept here only so
# a pool.toml missing [review] entirely (test_defaults_when_review_table_missing) still has something sane to
# fall back to; the shipped pool.toml is the actual source of truth operators edit.
DEFAULT_SECURITY_PATHS = [
    "orchestrator/*.py",
    ".claude/hooks/**", ".claude/settings.json", ".orchestrator/pool.toml", ".orchestrator/protected-paths.txt",
    ".orchestrator/prompts/**", "skills/**", ".claude/skills/**",
    ".mcp*.json", ".mcp.worker.json", "Dockerfile", "docker-compose*.yml", "pyproject.toml", "uv.lock",
]
DEFAULT_SEMANTIC_PATHS = [
    "pyproject.toml", "uv.lock", "Dockerfile*", "docker/**", "**/migrations/**",
    "orchestrator/serve.py", "orchestrator/mcp.py", "orchestrator/bus_mcp.py", "orchestrator/cli.py",
]
DEFAULT_SEMANTIC_PATTERNS = {
    "authorization": r"(?i)\b(?:bearer|token|authenticate|authorize|permission)\b",
    "database": r"(?i)\b(?:ALTER TABLE|CREATE TABLE)\b",
    "exports": r"^\s*__all__\s*=",
    "mcp_or_cli": r"@mcp\.tool|add_parser\(",
}
CODE_REVIEW = "always"              # never | security_paths | always -- "always" is the safest default when
                                     # [review] is missing entirely, matching pre-2026-09-19 D1 behaviour
SECURITY_PATHS = DEFAULT_SECURITY_PATHS
SEMANTIC_PATHS = DEFAULT_SEMANTIC_PATHS
SEMANTIC_PATTERNS = DEFAULT_SEMANTIC_PATTERNS
SECURITY_REVIEW_TIER = "sonnet"
SECURITY_CHECKLIST_COMPLEXITY = 7   # spawn.py's run_worker hardcodes the security-checklist cutoff at
                                     # complexity >= 7; a security-path review is stamped at least this
                                     # complexity so it always gets the checklist, whatever the source task's
                                     # own complexity was
# The constants above are defaults; _load_review_cfg() overwrites them from pool.toml's [review] table at the
# top of every tick() so dispatch()/gate()/merge_reviewed() (which read them as plain module globals, not
# through a Pool argument) always see the current policy without threading pool.cfg through every call.
_code_review_warned = False  # notify() the first time pool.toml carries an unrecognised code_review value, not every tick
_security_paths_empty_warned = False  # notify() the first time security_paths is empty under code_review="security_paths"
LOCK_PATH = STATE / "daemon.lock"
HANDOVER_INTERVAL_S = 15 * 60
HANDOVER_STATE = STATE / "handover_state.json"
STAGE_LEASE_S = 900
LEASED_STAGES = {"dispatched_at", "spec_review_at", "gated_at", "merged_at"}
































def _fix_round_spec(held, round_no, failed_ids, comments):
    prompt = (Path(__file__).resolve().parents[1] / ".orchestrator" / "prompts" / "fix-round.md").read_text()
    criteria = held.get("acceptance") or []
    selected = [c for c in criteria if any(i in c for i in (failed_ids or []))] or criteria
    def fence_data(value):
        chunks = []
        backticks = 0
        for char in value:
            if char == "`":
                backticks += 1
            else:
                if backticks >= 3:
                    chunks.append("[backticks omitted]")
                chunks.append("`" * (backticks % 3))
                backticks = 0
                chunks.append(char)
        if backticks >= 3:
            chunks.append("[backticks omitted]")
        chunks.append("`" * (backticks % 3))
        return "".join(chunks)

    failures = (held.get("resume_hint") or {}).get("failures") or ""
    if isinstance(failures, list):
        failures = "\n".join(str(line) for line in failures)
    failure_text = fence_data(str(failures)[:3000])
    review_lines = [f"{c.get('path', '')}:{c.get('line', '')} {c.get('issue', '')}" for _, cs in comments for c in cs]
    return prompt.format(root_id=root(held)["id"], root_title=root(held)["title"], held_id=held["id"],
                         n=round_no, failed_acceptance="\n".join(f"- {c}" for c in selected),
                         failure_text=failure_text, review_comments=fence_data("\n".join(review_lines) or "(none)"),
                         branch=held.get("branch") or f"task/{held['id']}",
                         head_sha=held.get("head_sha") or (held.get("resume_hint") or {}).get("commit", "unknown"),
                         failure_kind=(held.get("pipeline") or {}).get("failure_kind", "unknown"),
                         original_acceptance="\n".join(f"- {c}" for c in criteria))


def auto_fix_round(pool):
    cap = pool.cfg.get("daemon", {}).get("auto_fix_rounds", 2)
    for held in bus.read(status="held", role="execute"):
        if stale(held):
            continue
        held_at = planner_runs._held_at(held)
        if held_at is None:
            continue
        key = str(held_at)
        chain = lineage(held)
        if any(t["id"] != held["id"] and t.get("status") != "failed" and
               (t.get("constraints") or {}).get("fix_round_for") == held["id"] for t in chain):
            continue
        reason = held.get("hold_reason", "")
        kind = failure_kind(held, held.get("worktree"),
                            rerun_max=pool.cfg.get("daemon", {}).get("flaky_rerun_max", 1),
                            rerun_timeout=pool.cfg.get("daemon", {}).get("flaky_rerun_timeout_s", 600))
        with bus.locked():
            current = bus.get(held["id"])
            pipeline = dict(current.get("pipeline") or {})
            if pipeline.get("failure_kind") != kind:
                pipeline["failure_kind"] = kind
                bus.update(held["id"], pipeline=pipeline)
            held = bus.get(held["id"])
        signature = failure_signature(held)
        ids, comments, routine = None, [], False
        if kind == "code_defect" and reason == "gate_red":
            ids = _test_ids((held.get("resume_hint") or {}).get("failures"))
            routine = ids is not None
        elif kind == "code_defect" and reason.startswith("review request_changes"):
            comments = _rejecting_reviews(held)
            routine = bool(comments) and all(_path_in_scope(c.get("path"), held.get("scope") or [])
                                             for _, cs in comments for c in cs)
        rounds = sum(1 for t in chain if (t.get("constraints") or {}).get("auto_round") is not None)
        # A fix task's own constraint records the failure it was created to repair.
        # Matching it means the immediately preceding round did not change the failure.
        repeated = any((t.get("constraints") or {}).get("failure_signature") == signature for t in chain)
        if pool.cfg.get("planner", {}).get("autonomous") and decision.routes_enabled(pool.cfg.get("planner", {})):
            point = {"goal_id": held.get("parent"), "kind": "held", "task_id": held["id"],
                     "payload_key": planner_runs._held_key(held)}
            if point["goal_id"]:
                ctx = planner_runs.build_ctx(point, pool)
                routine = planner_runs.decision.route(point, ctx).name == "routine"
                if routine:
                    ids = ctx["failing_ids"] or None
                    comments = _rejecting_reviews(held)
        if routine and rounds < cap and not repeated:
            with bus.locked():
                current = bus.get(held["id"])
                pipeline = dict(current.get("pipeline") or {})
                if pipeline.get("auto_fix_hold_key") == key:
                    continue
                n = rounds + 1
                bus.create_task(
                    f"fix round {n}: {root(current)['title']}", _fix_round_spec(current, n, ids, comments),
                    current["acceptance"], current["scope"], role="execute", parent=current.get("parent"),
                    tier=current["tier"], complexity=current["complexity"],
                    inputs=[current["id"]] + [r["id"] for r, _ in comments],
                    constraints={**(current.get("constraints") or {}), "fix_round_for": current["id"],
                                 "auto_round": n, "failure_signature": signature})
                pipeline["auto_fix_hold_key"] = key
                bus.update(current["id"], pipeline=pipeline)
            continue
        with bus.locked():
            current = bus.get(held["id"])
            pipeline = dict(current.get("pipeline") or {})
            skipped = dict(pipeline.get("auto_fix_skipped") or {})
            if kind == "quota":
                continue
            if key not in skipped:
                skipped[key] = True
                pipeline["auto_fix_skipped"] = skipped
                bus.update(held["id"], pipeline=pipeline)
                detail = "unchanged failure repeated" if repeated else kind
                hint = "; rebase-task hint (T-0258)" if kind == "conflict" else ""
                notify(f"{held['id']}: automatic fix round escalated ({kind}): {detail}{hint}")


def _load_review_cfg(pool):
    """Refresh the review-policy thresholds from pool.cfg's [review] table. Missing table or keys fall back to
    the defaults set on the module above -- so a pool.toml without [review] behaves exactly as if it had one
    with these values (§review policy, 2026-09-18: reviews were costing as much as execution)."""
    global SPEC_REVIEW_MIN, DIRECT_MERGE_MAX, TWO_REVIEWS_FROM, SPEC_REVIEW_TIER, REVIEW_COMPLEMENTARY
    global CODE_REVIEW, SECURITY_PATHS, SEMANTIC_PATHS, SEMANTIC_PATTERNS, SECURITY_REVIEW_TIER, _code_review_warned, STAGE_LEASE_S
    review = pool.cfg.get("review", {})
    REVIEW_COMPLEMENTARY = review.get("complementary", False) is True
    SPEC_REVIEW_MIN = review.get("spec_review_min", 6)
    DIRECT_MERGE_MAX = review.get("direct_merge_max", 3)
    TWO_REVIEWS_FROM = review.get("two_reviews_from", 7)
    SPEC_REVIEW_TIER = review.get("spec_review_tier", "sonnet")

    code_review = review.get("code_review", "always")
    if code_review not in ("never", "security_paths", "always"):
        if not _code_review_warned:
            notify(f"pool.toml [review].code_review={code_review!r} is not one of "
                   f"never|security_paths|always; falling back to always")
            _code_review_warned = True
        code_review = "always"
    CODE_REVIEW = code_review

    security_paths = review.get("security_paths", DEFAULT_SECURITY_PATHS)
    if not isinstance(security_paths, list) or not all(isinstance(p, str) for p in security_paths):
        security_paths = DEFAULT_SECURITY_PATHS
    SECURITY_PATHS = security_paths

    semantic_paths = review.get("semantic_paths", DEFAULT_SEMANTIC_PATHS)
    if not isinstance(semantic_paths, list) or not all(isinstance(p, str) for p in semantic_paths):
        semantic_paths = DEFAULT_SEMANTIC_PATHS
    SEMANTIC_PATHS = semantic_paths
    semantic_patterns = review.get("semantic_patterns", DEFAULT_SEMANTIC_PATTERNS)
    if isinstance(semantic_patterns, list):
        semantic_patterns = {str(i): p for i, p in enumerate(semantic_patterns)}
    if not isinstance(semantic_patterns, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                          for k, v in semantic_patterns.items()):
        semantic_patterns = DEFAULT_SEMANTIC_PATTERNS
    SEMANTIC_PATTERNS = semantic_patterns

    SECURITY_REVIEW_TIER = review.get("security_review_tier", "sonnet")
    STAGE_LEASE_S = pool.cfg.get("daemon", {}).get("stage_lease_s", 900)




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
        now = time.time()
        pipeline[stage] = now
        if stage in LEASED_STAGES and fields.get("status") != "held":
            pipeline[f"{stage}_lease"] = now + STAGE_LEASE_S
        if pipeline_fields:
            pipeline.update(pipeline_fields)
        bus.update(tid, pipeline=pipeline, **fields)
    return True


def complete(tid, stage, **fields):
    """Record that a stamped side effect was issued, under the same bus lock as its pipeline update."""
    with bus.locked():
        t = bus.get(tid)
        pipeline = dict(t.get("pipeline") or {})
        pipeline[f"{stage}_done"] = time.time()
        bus.update(tid, pipeline=pipeline, **fields)


def clear_stage(tid, stage, pipeline_fields=None, clear_pipeline_keys=(), **fields):
    """Clear a claim and every marker that belongs to it, atomically, for a safe retry."""
    with bus.locked():
        t = bus.get(tid)
        pipeline = dict(t.get("pipeline") or {})
        for key in (stage, f"{stage}_lease", f"{stage}_done"):
            pipeline.pop(key, None)
        for key in clear_pipeline_keys:
            pipeline.pop(key, None)
        if pipeline_fields:
            pipeline.update(pipeline_fields)
        bus.update(tid, pipeline=pipeline, **fields)








def _requeue(tid, pipeline):
    """Put a task back in the queue for dispatch() to retry. Clearing pipeline.dispatched_at is what actually
    makes that retry happen: dispatch()'s stamp() no-ops when the stage is already stamped, so a requeue that
    left dispatched_at in place would leave the task queued forever without a live worker."""
    clear_stage(tid, "dispatched_at", clear_pipeline_keys=("respawned_at",),
                status="queued", pid=None, reason="process died; requeued")
    return "requeued"


def reconcile_dead(t, pool=None):
    """A running task whose worker died >60s ago: the `claude -p` child (spawn.py Popen) can outlive the daemon
    thread that would have posted its result, finish and commit in its worktree, and leave the task stuck
    "running" with a dead pid. Requeuing unconditionally would redo that finished work on top of the executor's
    own commit (gotchas.md 2026-09-18: T-0115, commit abc3f56 sat in wt/T-0115 while the task went back to
    queued). Only an execute task with a worktree gets the extra check: a clean worktree with commits ahead of
    its base is posted as a done result so gate() re-gates it normally; a dirty one is held for the Planner.
    Everything else (scout/review/etc, or an execute task with no worktree or no commits ahead) requeues as
    before. Returns "requeued" | "regated" | "held" so this is unit-testable without a live pid."""
    tid, worktree = t["id"], t.get("worktree")
    (pool or Pool()).release(tid, t.get("result") or {})
    if t.get("role") != "execute" or not worktree or not Path(worktree).is_dir():
        return _requeue(tid, t.get("pipeline"))

    base = _resolve_base(worktree, t.get("parent"))
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
    """Recognize landed task commits by ancestry, excluding an unchanged target head and
    branches with no commits beyond trunk. A merge-base with the target itself cannot
    prove work landed: it equals the task head for every ancestor, including real merges.
    Missing refs or git errors leave the task available for normal gating."""
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
        head = spawn.git("rev-parse", branch, check=False)
        target_head = spawn.git("rev-parse", target, check=False)
        if head.returncode or target_head.returncode or head.stdout.strip() == target_head.stdout.strip():
            return False
        trunk = next((c for c in ("origin/main", "main")
                      if spawn.git("rev-parse", "--verify", c, check=False).returncode == 0), None)
        if trunk:
            mb = spawn.git("merge-base", branch, trunk, check=False)
            if mb.returncode == 0 and head.returncode == 0 and head.stdout.strip() == mb.stdout.strip():
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


def running_claude_workers(pool):
    """Count claimed Claude workers, including fallback executes marked on executor."""
    return sum(1 for task in bus.read(status="running")
               if any((task.get(field) or "").startswith("claude:")
                      for field in ("assigned_to", "executor")))


def inflight_claude_dispatches():
    """Count execute dispatches whose worker has not claimed its task yet."""
    return sum(1 for task in bus.read(status="queued", role="execute")
               if (task.get("pipeline") or {}).get("dispatched_at")
               and not (task.get("pipeline") or {}).get("gated_at"))


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
    max_workers = pool.cfg.get("limits", {}).get("max_parallel_claude_workers", 4)
    return max(0, max_workers - running_claude_workers(pool) - inflight_claude_dispatches())


def spawn_async(fn, *args, **kwargs):
    """Fire a side-effecting call (executor.start, spawn.run_worker) in a background thread so tick() never
    blocks on a slow subprocess."""
    if kwargs:
        try:
            parameters = inspect.signature(fn).parameters.values()
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters)
            accepted = {p.name for p in parameters
                        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
            kwargs = kwargs if accepts_kwargs else {k: v for k, v in kwargs.items() if k in accepted}
        except (TypeError, ValueError):
            pass
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


def hold_failed(tid, error_key, stage_label, exc):
    """A stage's side effect raised: hold the task with a short reason and an error stamp instead of leaving it
    wedged at a stamped-but-never-acted-on stage."""
    with bus.locked():
        t = bus.get(tid)
        pipeline = dict(t.get("pipeline") or {})
        pipeline[error_key] = str(exc)[:300]
        bus.update(tid, status="held", hold_reason=f"{stage_label} failed: {type(exc).__name__}", pipeline=pipeline)
    notify(f"{tid}: {stage_label} failed: {exc}")


def _dispatch_worker(task_id, prompt, executor_id=None, packet_meta=None):
    routing = None
    try:
        task = bus.get(task_id)
        routing = jev_route.shadow_context(task, Pool())
    except Exception:
        routing = None
    try:
        try:
            parameters = inspect.signature(executor.start).parameters.values()
            accepts_executor = any(p.kind == inspect.Parameter.VAR_KEYWORD or
                                   (p.name == "executor_id" and p.kind != inspect.Parameter.POSITIONAL_ONLY)
                                   for p in parameters)
            accepts_meta = any(p.kind == inspect.Parameter.VAR_KEYWORD or
                               (p.name == "packet_meta" and p.kind != inspect.Parameter.POSITIONAL_ONLY)
                               for p in parameters)
        except (TypeError, ValueError):
            accepts_executor = True
            accepts_meta = True
        kwargs = {"executor_id": executor_id} if executor_id is not None and accepts_executor else {}
        if packet_meta is not None and accepts_meta:
            kwargs["packet_meta"] = packet_meta
        r = executor.start(task_id, prompt, **kwargs)
        if routing is not None:
            jev_route.record_shadow(task_id, routing)
        if r["status"] == "done":
            bus.post_result(task_id, spawn.fit_result({
                "summary": r["message"][:3000],
                "executed_by": "codex:" + bus.get(task_id)["executor"],
                "thread": r["thread"],
                "usage": r.get("usage"),
            }), "done")
        elif r["status"] == "failed":
            bus.post_result(task_id, spawn.fit_result({"reason": r["reason"][:3000]}), "failed")
    except Exception as e:
        with bus.locked():
            t = bus.get(task_id)
            pipeline = dict(t.get("pipeline") or {})
            pipeline["dispatch_error"] = str(e)[:300]
            bus.update(task_id, pipeline=pipeline)
        bus.post_result(task_id, spawn.fit_result({"reason": f"dispatch error: {e}"[:3000]}), "failed")


def _fix_round_delta(parent, fix):
    """Only the failed state changes between fix rounds; keep the resumed prompt to that delta."""
    failures = (parent.get("resume_hint") or {}).get("failures")
    if isinstance(failures, list):
        failures = "\n".join(str(line) for line in failures)
    if not failures:
        reviews = _rejecting_reviews(parent)
        failures = "\n".join(f"{c.get('path', '')}:{c.get('line', '')} {c.get('issue', '')}"
                             for _, comments in reviews for c in comments) or "(no failure detail recorded)"
    acceptance_ids = _test_ids(failures) or []
    selected = [criterion for criterion in fix.get("acceptance") or []
                if not acceptance_ids or any(test_id in criterion for test_id in acceptance_ids)]
    return "Failures:\n" + str(failures)[:3000] + "\n\nAcceptance:\n" + "\n".join(
        f"- {criterion}" for criterion in (selected or fix.get("acceptance") or []))


def _dispatch_fresh_fix(task_id, reason):
    fix = bus.get(task_id)
    pipeline = dict(fix.get("pipeline") or {})
    pipeline["resume"] = {"mode": "fresh", "reason": reason}
    bus.update(task_id, pipeline=pipeline, worktree=None, branch=f"task/{task_id}")
    fix = bus.get(task_id)
    packet = spawn.packet(fix, spawn.ROOT)
    prompt = spawn.render("execute", packet=packet, spec=fix["spec"],
                          acceptance=fix["acceptance"], scope=fix["scope"])
    _dispatch_worker(task_id, prompt, None, spawn.packet_run_meta(packet))


def _dispatch_reply_worker(task_id, parent_id, delta, plan=None):
    try:
        r = executor.reply(parent_id, delta, fix_round_task_id=task_id, plan=plan)
        if r["status"] == "done":
            bus.post_result(task_id, spawn.fit_result({
                "summary": r["message"][:3000], "executed_by": "codex:" + bus.get(parent_id)["executor"],
                "thread": r["thread"], "usage": r.get("usage"),
            }), "done")
        elif r["status"] == "failed":
            bus.post_result(task_id, spawn.fit_result({"reason": r["reason"][:3000]}), "failed")
        elif r["status"] == "held":
            bus.post_result(task_id, spawn.fit_result({"reason": "budget"}), "held")
            clear_stage(task_id, "dispatched_at", status="held", hold_reason="budget")
        elif r["status"] == "refused":
            pipeline = dict(bus.get(task_id).get("pipeline") or {})
            pipeline["resume"] = {"mode": "fresh", "reason": "parent_merged"}
            bus.update(task_id, pipeline=pipeline)
            bus.post_result(task_id, spawn.fit_result({"reason": "parent_merged"}), "failed")
        elif r["status"] == "incompatible":
            _dispatch_fresh_fix(task_id, f"compat_changed:{r['reason']}")
        else:
            bus.post_result(task_id, spawn.fit_result({"reason": f"unexpected reply status: {r.get('status')}"}), "failed")
    except Exception as e:
        head = str(e).splitlines()[0] if str(e) else type(e).__name__
        bus.post_result(task_id, spawn.fit_result({"reason": head[:3000]}), "failed")


_scheduler_mode_warned = False


def _load_scheduler_cfg(pool):
    """Read scheduler policy on every dispatch, including direct callers."""
    global _scheduler_mode_warned
    cfg = {"mode": "shadow", "soft_conflict_policy": "defer", "max_wave": 0, "stale_rebase": False}
    cfg.update(pool.cfg.get("scheduler", {}))
    if cfg["mode"] not in ("off", "shadow", "active"):
        if not _scheduler_mode_warned:
            notify(f"pool.toml [scheduler].mode={cfg['mode']!r} is not one of "
                   "off|shadow|active; falling back to shadow")
            _scheduler_mode_warned = True
        cfg["mode"] = "shadow"
    return cfg


def eligible(pool, candidates):
    """Filter candidates in their supplied order without changing bus state.

    A dispatched task is already owned, even while queued before its worker claims it;
    it belongs to the wave's running set, never its ready candidates.
    """
    fallback = _fallback_mode(pool)
    return [t["id"] for t in candidates
            if not stale({**t, "status": "queued"}) and bus.ready(t)
            and not (t.get("pipeline") or {}).get("dispatched_at")
            and (t["complexity"] < SPEC_REVIEW_MIN or t.get("spec_review_verdict") == "approve")
            and (not fallback or fallback_tier(t["complexity"]) is not None)]


def _first_come_order(eligible_ids, slots):
    return eligible_ids[:max(0, slots)]


def _wave_order(candidate_ids, tasks):
    return critical_path.rank(candidate_ids, tasks)


def _wave_tasks(candidates, running):
    """Include dependency ancestry across goals, tolerating missing ids and cycles."""
    tasks = {t["id"]: t for t in candidates + running}
    pending = list(tasks.values())
    seen = set(tasks)
    while pending:
        for tid in pending.pop().get("depends_on") or []:
            if tid in seen:
                continue
            seen.add(tid)
            try:
                task = bus.get(tid)
            except KeyError:
                continue
            tasks[tid] = task
            pending.append(task)
    # Ranking needs unresolved execute descendants even when they are held or running;
    # merged and failed work cannot delay the goal and is deliberately excluded.
    pending = list(candidates)
    while pending:
        for task in bus.dependents(pending.pop()["id"]):
            tid = task["id"]
            if (tid in tasks or task.get("role") != "execute" or task.get("merged_into")
                    or task.get("status") == "failed"):
                continue
            tasks[tid] = task
            pending.append(task)
    return tasks


def dispatch(pool):
    """queued execute tasks whose dependencies are merged: hand to the executor, or route through spec review first."""
    scheduler = _load_scheduler_cfg(pool)
    slots = free_slots(pool)
    fallback = _fallback_mode(pool)
    row = {"ts": time.time(), "free_slots": slots, "fallback": fallback,
           "running_execute": len(bus.read(status="running", role="execute")),
           "running_claude": running_claude_workers(pool), "considered": []}
    enabled = [ex for ex in pool.executors.values() if ex.enabled and "execute" in ex.roles]
    capacity_reason = ("account_capacity" if fallback else
                       "cooldown" if enabled and all(ex.cooling() for ex in enabled) else
                       "executor_capacity")
    retry_held = [t for t in bus.read(status="held", role="execute")
                  if t.get("hold_reason") == "budget" and not (t.get("pipeline") or {}).get("dispatched_at")]
    # Each bus read is id-ordered; preserve queued work ahead of budget retries.
    candidates = bus.read(status="queued", role="execute") + retry_held
    candidate_ids = eligible(pool, candidates)
    selected = _first_come_order(candidate_ids, slots)
    deferred = {}
    wave_row = None
    dispatched_ids = []
    lost_ids = []
    if scheduler["mode"] != "off" and candidate_ids:
        running = [t for t in bus.read(role="execute")
                   if t["status"] == "running" or
                   (t["status"] in ("queued", "held") and
                    (t.get("pipeline") or {}).get("dispatched_at") and
                    not (t.get("pipeline") or {}).get("gated_at"))]
        running_ids = [t["id"] for t in running]
        tasks = _wave_tasks([t for t in candidates if t["id"] in candidate_ids], running)
        wave_limit = min(slots, scheduler["max_wave"] or slots)
        wave_order = list(_wave_order(candidate_ids, tasks))
        result = interference.select_wave(
            candidate_ids, running_ids, tasks, capacity=wave_limit,
            order=wave_order,
            rules={"soft_conflict_policy": scheduler["soft_conflict_policy"]})
        wave_row = {"ts": row["ts"], "mode": scheduler["mode"], "ready": candidate_ids,
                                 "running": running_ids, "baseline_order": selected, "wave": result["wave"],
                                 "deferred": result["deferred"],
                                 "predicted": interference.pairwise([tasks[i] for i in result["wave"]]),
                                 "priority": {tid: critical_path.explain(tid, tasks) for tid in candidate_ids},
                                 "applied": scheduler["mode"] == "active"}
        if scheduler["mode"] == "active":
            selected = result["wave"]
            deferred = {item["task"]: capacity_reason if item["reason"] == "capacity"
                        else "predicted_interference" for item in result["deferred"]}
    # Visit selected tasks in wave order; still process every other candidate for reviews and telemetry.
    ranks = {tid: index for index, tid in enumerate(selected)}
    visit = (sorted(candidates, key=lambda t: ranks.get(t["id"], len(ranks)))
             if scheduler["mode"] == "active" else candidates)
    while visit:
        t = visit.pop(0)
        entry = {"task": t["id"], "goal_id": t.get("parent"), "ready": bus.ready(t),
                 "action": "skipped", "reason": "other"}
        if t.get("executor"):
            entry["executor"] = t["executor"]
        row["considered"].append(entry)
        if stale({**t, "status": "queued"}):
            entry["reason"] = "stale"
            continue
        if not entry["ready"]:
            entry["reason"] = "dependency"
            continue
        if t.get("status") == "queued":
            with bus.locked():
                current = bus.get(t["id"])
                pipeline = dict(current.get("pipeline") or {})
                if "first_ready_at" not in pipeline:
                    pipeline["first_ready_at"] = time.time()
                    bus.update(t["id"], pipeline=pipeline)
        verdict = t.get("spec_review_verdict")
        if t["complexity"] < SPEC_REVIEW_MIN or verdict == "approve":
            if fallback and fallback_tier(t["complexity"]) is None:
                entry["reason"] = "fallback_no_tier"
                continue  # no Claude tier for this complexity (9+): wait for Codex instead of being held later
            if t["id"] not in selected or slots <= 0:
                entry["reason"] = deferred.get(t["id"], "other" if
                    (t.get("pipeline") or {}).get("dispatched_at") else capacity_reason)
                continue
            if stamp(t["id"], "dispatched_at", **({"status": "queued"} if t.get("status") == "held" else {})):
                entry["action"] = "dispatched"
                entry.pop("reason")
                if fallback:
                    entry["executor"] = "claude:" + fallback_tier(t["complexity"])
                slots -= 1
                dispatched_ids.append(t["id"])
                fix_parent_id = (t.get("constraints") or {}).get("fix_round_for")
                if fix_parent_id:
                    parent = bus.get(fix_parent_id)
                    plan = executor.resume_plan(parent, t)
                    reason = plan["reason"]
                    pipeline = dict(bus.get(t["id"]).get("pipeline") or {})
                    if plan["mode"] == "resume":
                        pipeline["resume"] = {"mode": "resume", "thread": parent["codex_thread"],
                                              "parent": parent["id"]}
                        bus.update(t["id"], pipeline=pipeline, worktree=parent.get("worktree"),
                                   branch=parent.get("branch") or f"task/{parent['id']}")
                        complete(t["id"], "dispatched_at")
                        spawn_async(_dispatch_reply_worker, t["id"], parent["id"], _fix_round_delta(parent, t), plan)
                        continue
                    pipeline["resume"] = {"mode": "fresh", "reason": reason}
                    bus.update(t["id"], pipeline=pipeline)
                packet = spawn.packet(t, t.get("worktree") or spawn.ROOT)
                prompt = spawn.render("execute", packet=packet, spec=t["spec"],
                                      acceptance=t["acceptance"], scope=t["scope"])
                spawn_async(_dispatch_worker, t["id"], prompt, None, spawn.packet_run_meta(packet))
                complete(t["id"], "dispatched_at")
            else:
                # A competing dispatcher owns this task. Its failed claim costs us no
                # slot; refill from the unvisited queue, respecting the winner's scope.
                remaining = eligible(pool, [bus.get(task["id"]) for task in visit])
                if scheduler["mode"] == "active":
                    lost_ids.append(t["id"])
                    tasks[t["id"]] = bus.get(t["id"])
                    result = interference.select_wave(
                        remaining, running_ids + dispatched_ids + lost_ids, tasks,
                        capacity=min(slots, wave_limit - len(dispatched_ids)), order=wave_order,
                        rules={"soft_conflict_policy": scheduler["soft_conflict_policy"]})
                    selected = result["wave"]
                    deferred = {item["task"]: capacity_reason if item["reason"] == "capacity"
                                else "predicted_interference" for item in result["deferred"]}
                    wave_row["deferred"] = result["deferred"]
                    ranks = {tid: index for index, tid in enumerate(selected)}
                    visit.sort(key=lambda task: ranks.get(task["id"], len(ranks)))
                else:
                    selected = _first_come_order(remaining, slots)
                continue
        elif verdict == "request_changes":
            if stamp(t["id"], "spec_review_held_at", status="held", hold_reason="spec_review request_changes"):
                entry["reason"] = "spec_review_changes"
                notify(f"{t['id']}: spec review asked for changes; re-spec it")
        elif not any(r["inputs"][:1] == [t["id"]] for r in bus.read(role="spec_review")):
            if stamp(t["id"], "spec_review_at"):
                try:
                    sr = bus.create_task(f"spec review: {t['title']}", t["spec"], t["acceptance"], t["scope"],
                                         role="spec_review", inputs=[t["id"]], parent=t.get("parent"),
                                         complexity=t["complexity"], tier=SPEC_REVIEW_TIER)
                    entry.update(action="spec_review", reason="spec_review_pending")
                    spawn_async(spawn.run_worker, sr["id"])
                    complete(t["id"], "spec_review_at")
                except Exception as e:
                    hold_failed(t["id"], "spec_review_error", "spec_review", e)
        else:
            entry["reason"] = "spec_review_pending"
    if wave_row is not None:
        if scheduler["mode"] == "active":
            wave_row["wave"] = dispatched_ids
            wave_row["predicted"] = interference.pairwise([tasks[i] for i in dispatched_ids])
        schedlog.append("waves", wave_row)
    held_ids = {t["id"] for t in retry_held}
    for entry in row["considered"]:
        if entry["task"] in held_ids and entry["action"] != "dispatched" and entry["task"] not in deferred:
            entry["reason"] = "budget"
    if row["considered"]:
        row["considered"].sort(key=lambda entry: entry["task"])
        schedlog.append("dispatch", row)
    # Reviews are normally spawned when they are created, so they are not part of the execute loop above.
    # Recover the two pre-claim failure modes: a dead worker requeued by reconcile_dead, and a spawn thread
    # that vanished before bus.claim.  The per-requeue stamp prevents every daemon tick spawning another copy.
    max_workers = pool.cfg.get("limits", {}).get("max_parallel_claude_workers", 4)
    review_slots = max(0, max_workers - running_claude_workers(pool) - inflight_claude_dispatches())
    respawn_after = pool.cfg.get("daemon", {}).get("respawn_after_s", 120)
    now = time.time()
    for task in bus.read(status="queued"):
        if review_slots <= 0:
            break
        if task.get("role") not in ("review", "spec_review"):
            continue
        if stale(task):
            continue
        pipeline = task.get("pipeline") or {}
        events = task.get("events") or []
        requeued_at = max((event.get("ts", 0) for event in events
                           if event.get("reason") == "process died; requeued"),
                          default=task.get("claimed_at") or 0)
        if pipeline.get("respawned_at", 0) > requeued_at:
            continue
        died = any(event.get("reason") == "process died; requeued" for event in events)
        created_at = task.get("created_at") or min((event.get("ts", 0) for event in events), default=0)
        if not created_at:
            try:
                created_at = (bus.TASKS / f"{task['id']}.json").stat().st_mtime
            except OSError:
                created_at = now
        if not died and now - created_at < respawn_after:
            continue
        with bus.locked():
            current = bus.get(task["id"])
            current_pipeline = dict(current.get("pipeline") or {})
            current_events = current.get("events") or []
            current_requeued_at = max((event.get("ts", 0) for event in current_events
                                       if event.get("reason") == "process died; requeued"),
                                      default=current.get("claimed_at") or 0)
            if (current.get("status") != "queued" or
                    current_pipeline.get("respawned_at", 0) > current_requeued_at):
                continue
            current_pipeline["respawned_at"] = now
            bus.update(task["id"], pipeline=current_pipeline)
            spawn_async(spawn.run_worker, task["id"])
        review_slots -= 1


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
    """How many review approvals an execute task needs before merge_reviewed() may merge it under [review]'s
    "always" policy (today's pre-2026-09-19 D1 split). An orphaned result (reconcile_dead re-gating a dead
    worker's last commit) always needs exactly one, whatever the task's complexity: the orphaned warning is
    what needs the second pair of eyes, not the model split (T-0150 review: an orphaned complexity-7 task was
    stuck waiting on a second review gate() never opens). _review_plan() is the actual source of truth gate()
    uses for how many review tasks to open under every code_review setting, not just "always"; this function
    remains as its "always" branch and as merge_reviewed()'s fallback for tasks gated before reviews_expected
    was stamped."""
    if (t.get("result") or {}).get("orphaned") or t["complexity"] < TWO_REVIEWS_FROM:
        return 1
    return 2


def _matching_security_path(paths):
    """The first configured security glob any of `paths` matches (fnmatch, so "**" matches any depth same as
    "*" -- fnmatch is not path-separator aware), or None."""
    for pattern in SECURITY_PATHS:
        for path in paths:
            if fnmatch.fnmatch(path, pattern):
                return pattern
    return None


def _matching_semantic_path(paths):
    for pattern in SEMANTIC_PATHS:
        if any(fnmatch.fnmatch(path, pattern) for path in paths):
            return pattern
    return None


_SEMANTIC_DIFF_UNAVAILABLE = object()




def _matching_semantic_pattern(t):
    lines = _added_diff_lines(t)
    if lines is None:
        return _SEMANTIC_DIFF_UNAVAILABLE
    for name, pattern in SEMANTIC_PATTERNS.items():
        try:
            if any(re.search(pattern, line) for line in lines):
                return name
        except re.error:
            continue
    return None


def _security_review_tier(t):
    """security_review_tier, unless the executor is a Claude tier that IS security_review_tier -- then the
    other Claude tier, so a security-path review is never self-reviewed by the model that executed it."""
    tier = SECURITY_REVIEW_TIER
    if (t.get("executor") or "") == f"claude:{tier}":
        return _other_tier(tier)
    return tier


def _review_plan(t):
    """How many review tasks gate() should open for a done execute task, and why -- stamped verbatim onto
    pipeline.review_reason/reviews_expected so merge_reviewed() and a later change to [review] agree on what
    this task was actually gated for. Orphaned results always need exactly one review whatever CODE_REVIEW
    says (see reviews_expected()'s docstring). Otherwise: "never" merges everything straight through;
    "security_paths" reviews only a diff that touches a security-sensitive glob, or one changed_paths()
    couldn't determine (fails closed, review_reason "diff_unavailable" -- a plumbing error must never merge
    unreviewed work); an empty or missing security_paths list also fails closed (review_reason
    "security_paths_empty" -- a blank list must never silently mean "nothing is security-sensitive"); "always"
    is the pre-2026-09-19 D1 policy (DIRECT_MERGE_MAX lets the cheapest tasks merge on hooks alone,
    reviews_expected(t) drives the complexity split above that)."""
    if (t.get("result") or {}).get("orphaned"):
        return 1, "orphaned"
    if CODE_REVIEW == "never":
        return 0, "none"
    if CODE_REVIEW == "security_paths":
        if not SECURITY_PATHS:
            global _security_paths_empty_warned
            if not _security_paths_empty_warned:
                notify("pool.toml [review].security_paths is empty; every security_paths review fails closed "
                       "to one review until it is configured")
                _security_paths_empty_warned = True
            return 1, "security_paths_empty"
        paths = changed_paths(t)
        if paths is None:
            return 1, "diff_unavailable"
        match = _matching_security_path(paths)
        if match:
            return 1, f"security_paths:{match}"
        semantic_match = _matching_semantic_path(paths)
        if semantic_match:
            return 1, f"semantic_path:{semantic_match}"
        semantic_pattern = _matching_semantic_pattern(t)
        if semantic_pattern is _SEMANTIC_DIFF_UNAVAILABLE:
            return 1, "diff_unavailable"
        return (1, f"semantic_pattern:{semantic_pattern}") if semantic_pattern else (0, "none")
    # "always"
    if t["complexity"] <= DIRECT_MERGE_MAX:
        return 0, "always"
    return reviews_expected(t), "always"


def _dirty_scope_paths(worktree, scope):
    """Tracked or untracked paths under `scope` that git status sees as changed in `worktree`, ignoring the
    orchestrator's own state dir and a venv -- neither is something an executor is expected to have committed.
    Empty (never None) on a git error, so gate() can treat "can't tell" the same as "nothing to report" rather
    than blocking merge on a status call that failed for an unrelated reason."""
    r = _git_in(worktree, "status", "--porcelain", "-z", "--untracked-files=all")
    if r.returncode != 0:
        return []
    dirty = set()
    entries = iter(r.stdout.split("\0"))
    for entry in entries:
        if not entry:
            continue
        paths = [entry[3:]]
        if "R" in entry[:2] or "C" in entry[:2]:
            paths.append(next(entries, ""))  # -z reports destination, then source
        for path in paths:
            if not path or path.split("/", 1)[0] in (".orchestrator", ".venv"):
                continue
            if any(fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("/") + "/")
                   or pattern in (".", "./") for pattern in scope):
                dirty.add(path)
    return sorted(dirty)


def _open_reviews(t, n_reviews, review_reason):
    """Create exactly the missing review children and issue their workers."""
    t = bus.get(t["id"])
    existing = [x for x in bus.read(role="review") if x["inputs"][:1] == [t["id"]]]
    security = (review_reason in ("diff_unavailable", "security_paths_empty") or
                review_reason.startswith("security_paths:") or review_reason.startswith("semantic_"))
    reviewed_sha = None
    if t.get("worktree"):
        head = _git_in(t["worktree"], "rev-parse", "HEAD")
        if head.returncode == 0:
            reviewed_sha = head.stdout.strip()
    if reviewed_sha:
        pipeline = dict(t.get("pipeline") or {})
        pipeline["reviewed_sha"] = reviewed_sha
        bus.update(t["id"], pipeline=pipeline)
        existing = [x for x in existing if x.get("reviewed_sha") == reviewed_sha]
    while len(existing) < n_reviews:
        number = len(existing)
        spec = t["spec"]
        complexity = t["complexity"]
        if review_reason == "orphaned":
            spec = ("orphaned executor: verify the acceptance criteria are fully met, the worker may "
                    f"have died mid-task\n\n{spec}")
            tier = review_tier(t)
        elif security:
            complexity = max(complexity, SECURITY_CHECKLIST_COMPLEXITY)
            tier = _security_review_tier(t)
        elif number == 0:
            tier = review_tier(t)
        elif (t.get("executor") or "").startswith("claude:"):
            tier = review_tier(t)
        else:
            tier = _other_tier(existing[0]["tier"])
        constraints = None
        if globals().get("REVIEW_COMPLEMENTARY", False) and n_reviews == 2:
            constraints = {"reviewer_role": "acceptance" if number == 0 else "adversarial"}
        r = bus.create_task(f"review: {t['title']}", spec, t["acceptance"], t["scope"], role="review",
                            inputs=[t["id"]], parent=t.get("parent"), complexity=complexity, tier=tier,
                            constraints=constraints)
        if reviewed_sha:
            bus.update(r["id"], reviewed_sha=reviewed_sha)
        spawn_async(spawn.run_worker, r["id"])
        existing.append(r)
    return existing


def stale_check(task):
    """Return deterministic stale-work evidence without changing pipeline state."""
    worktree = task.get("worktree")
    task_ref = task.get("branch") or f"task/{task['id']}"
    goal_ref = f"goal/{task['parent']}" if task.get("parent") else "main"
    head = _git_in(worktree, "rev-parse", goal_ref)
    goal_head = head.stdout.strip() if head.returncode == 0 else goal_ref
    result = {"base": task_ref, "goal_head": goal_head, "moved_count": 0,
              "stale_paths": [], "risk": "unknown"}
    try:
        moved = gitutil.moved_paths(task_ref, goal_ref, cwd=worktree)
    except gitutil.GitError:
        return result
    changed = changed_paths(task) or []
    surface = list(task.get("scope") or []) + changed
    stale_paths = sorted(path for path in moved
                         if any(path == pattern or fnmatch.fnmatch(path, pattern) for pattern in surface))
    risk = "none" if not stale_paths else ("high" if any(path in changed for path in stale_paths) else "low")
    return {"base": task_ref, "goal_head": goal_head, "moved_count": len(moved),
            "stale_paths": stale_paths, "risk": risk}


def _record_stale_check(task):
    """Record one stale check per task head and return the stamped evidence."""
    worktree = task["worktree"]
    head_result = _git_in(worktree, "rev-parse", "HEAD")
    head = head_result.stdout.strip() if head_result.returncode == 0 else "unknown"
    current = bus.get(task["id"])
    prior = (current.get("pipeline") or {}).get("stale_check") or {}
    if prior.get("head") == head:
        return prior, False
    try:
        evidence = stale_check(current)
    except Exception:
        task_ref = current.get("branch") or f"task/{current['id']}"
        goal_ref = f"goal/{current['parent']}" if current.get("parent") else "main"
        goal = _git_in(worktree, "rev-parse", goal_ref)
        evidence = {"base": task_ref, "goal_head": goal.stdout.strip() if goal.returncode == 0 else goal_ref,
                    "moved_count": 0, "stale_paths": [], "risk": "unknown"}
    stamped = {**evidence, "checked_at": time.time(), "head": head}
    pipeline = dict(current.get("pipeline") or {})
    pipeline["stale_check"] = stamped
    bus.update(task["id"], pipeline=pipeline)
    schedlog.append("stale", {"task": task["id"], "goal_id": task.get("parent"), **evidence,
                              "action": "recorded"})
    return stamped, True


def _stale_rebase(task, evidence):
    """Rebase high-risk work, returning True when gate processing must stop."""
    goal_ref = f"goal/{task['parent']}" if task.get("parent") else "main"
    rebased = _git_in(task["worktree"], "rebase", goal_ref)
    if rebased.returncode:
        conflicts = _git_in(task["worktree"], "diff", "--name-only", "--diff-filter=U")
        conflict_paths = sorted(path for path in conflicts.stdout.splitlines() if path)
        _git_in(task["worktree"], "rebase", "--abort")
        clear_stage(task["id"], "gated_at", status="held", hold_reason="stale_rebase_conflict",
                    resume_hint={"conflicts": conflict_paths, "goal_head": evidence["goal_head"]})
        schedlog.append("stale", {"task": task["id"], "goal_id": task.get("parent"), **evidence,
                                  "action": "rebase_conflict"})
        return True
    new_head = _git_in(task["worktree"], "rev-parse", "HEAD").stdout.strip()
    current = bus.get(task["id"])
    pipeline = dict(current.get("pipeline") or {})
    checked = dict(pipeline.get("stale_check") or {})
    checked["rebased_to"] = new_head
    pipeline["stale_check"] = checked
    bus.update(task["id"], pipeline=pipeline)
    clear_stage(task["id"], "gated_at")
    schedlog.append("stale", {"task": task["id"], "goal_id": task.get("parent"), **evidence,
                              "action": "rebased"})
    return True


def gate(pool):
    """done execute tasks that have not been gated: run tests-green on the worktree, then merge directly or open
    the number of review tasks _review_plan() says (see its docstring for the never/security_paths/always
    split). The second of two reviews (only possible under "always") must never run on the model that executed:
    when the executor is a Claude tier (executor field startswith "claude:"), both reviews run on review_tier(t)
    -- the non-executing tier (both reviews may land on the same account; only the model differs from the
    executor); when the executor is Codex, the second review runs on whichever tier the first one didn't get.
    All three fail-closed review reasons -- a security-path match ("security_paths:*"), a diff gate() couldn't
    inspect ("diff_unavailable"), and an empty/missing security_paths list ("security_paths_empty") -- take the
    same branch: exactly one review, on _security_review_tier(t), with the security checklist always forced,
    regardless of the source task's own complexity: spawn.py's run_worker hardcodes the checklist decision at
    complexity >= 7, so the review task is created with complexity bumped to at least
    SECURITY_CHECKLIST_COMPLEXITY rather than inheriting the source task's own (possibly much lower) complexity.
    The successful gate stamp also freezes
    pipeline.reviews_expected/review_reason so a later change to [review] can't change how many approvals
    merge_reviewed() waits for on a task already past this stage. Filters run cheap-first, already_merged()
    (which shells out to git) last, so a task the other checks would skip anyway never pays for a git call."""
    for t in bus.read(status="done", role="execute"):
        if stale(t) or (t.get("pipeline") or {}).get("gated_at") or t.get("merged_into"):
            continue
        with bus.locked():
            t = bus.get(t["id"])
            pipeline = dict(t.get("pipeline") or {})
            pipeline["gate_attempts"] = pipeline.get("gate_attempts", 0) + 1
            bus.update(t["id"], pipeline=pipeline)
        worktree = t.get("worktree")
        if worktree and not Path(worktree).exists():
            if stamp(t["id"], "gated_at", status="held", hold_reason="worktree missing"):
                notify(f"{t['id']}: worktree missing; held")
            continue
        if not worktree or not Path(worktree).is_dir():
            continue
        if worktree:
            dirty = _dirty_scope_paths(worktree, t.get("scope") or [])
            if dirty:
                if stamp(t["id"], "gated_at", status="held", hold_reason="executor did not commit",
                         resume_hint={"dirty": dirty}):
                    notify(f"{t['id']}: worktree has uncommitted scope changes; held")
                continue
            if already_merged(t):
                continue
            missing = acceptance.missing_tests(worktree, t.get("acceptance") or [])
            if missing:
                failures = [
                    f"FAILED {path}::{name} (missing: "
                    f"{'test not collected by unittest, define it inside a TestCase' if getattr(entry, 'reason', None) == 'not_collected' else 'test not defined'})"
                    for entry in missing for path, name in [entry]
                ]
                gate_reds = pipeline.get("gate_reds", 0) + 1
                if stamp(t["id"], "gated_at", pipeline_fields={"gate_reds": gate_reds},
                         status="held", hold_reason="gate_red",
                         resume_hint={"failures": failures, "missing_tests": missing}):
                    print(f"[daemon] {t['id']}: acceptance tests missing; held", file=sys.stderr)
                continue
        tg = subprocess.run([str(merge.TESTS_GREEN), worktree], capture_output=True, text=True, input="{}")
        if tg.returncode:
            gate_reds = pipeline.get("gate_reds", 0) + 1
            if stamp(t["id"], "gated_at", pipeline_fields={"gate_reds": gate_reds},
                     status="held", hold_reason="gate_red",
                     resume_hint={"failures": tg.stderr[-4000:]}):
                notify(f"{t['id']}: tests red at the gate; held")
            continue
        n_reviews, review_reason = _review_plan(t)
        now = time.time()
        green_fields = {"first_green_at": pipeline.get("first_green_at", now),
                        "reviews_expected": n_reviews, "review_reason": review_reason}
        with bus.locked():
            if not stamp(t["id"], "gated_at", pipeline_fields=green_fields):
                continue
            lineage_root = root(t)
            if lineage_root["id"] != t["id"]:
                root_pipeline = dict(lineage_root.get("pipeline") or {})
                root_pipeline.setdefault("first_green_at", now)
                root_pipeline["lineage_fix_rounds"] = root_pipeline.get("lineage_fix_rounds", 0) + 1
                bus.update(lineage_root["id"], pipeline=root_pipeline)
        try:
            evidence, _ = _record_stale_check(t)
            if _load_scheduler_cfg(pool)["stale_rebase"] and evidence["risk"] == "high":
                if _stale_rebase(t, evidence):
                    continue
            if n_reviews == 0:
                report_merge(t["id"], merge.merge(t["id"]))
            else:
                _open_reviews(t, n_reviews, review_reason)
            complete(t["id"], "gated_at")
        except Exception as e:
            hold_failed(t["id"], "gated_error", "gate", e)


def report_merge(task_id, r):
    task = bus.get(task_id)
    if task.get("parent"):
        goal_id = task["parent"]
        try:
            head = gitutil._git_in(spawn.ROOT, "rev-parse", f"goal/{goal_id}")
            head_sha = head.stdout.strip() if head.returncode == 0 else None
            with bus.locked():
                goal = bus.get(goal_id)
                pipeline = dict(goal.get("pipeline") or {})
                pipeline["last_merge"] = {**r, "head_sha": head_sha, "ts": time.time()}
                bus.update(goal_id, pipeline=pipeline)
        except (KeyError, OSError):
            pass
    if r.get("status") == "merged":
        merged_at = time.time()
        with bus.locked():
            current = bus.get(task_id)
            task_pipeline = dict(current.get("pipeline") or {})
            task_pipeline.setdefault("accepted_at", merged_at)
            bus.update(task_id, pipeline=task_pipeline)
            if (current.get("constraints") or {}).get("fix_round_for"):
                fix_id = task_id
                lineage_root = root(current)
                root_pipeline = dict(lineage_root.get("pipeline") or {})
                root_pipeline.setdefault("accepted_at", merged_at)
                while (current.get("constraints") or {}).get("fix_round_for"):
                    ancestor = bus.get(current["constraints"]["fix_round_for"])
                    fields = {"status": "done", "merged_into": r["target"],
                              "merged_via": f"fix round {fix_id} {r['sha']}", "hold_reason": None}
                    if ancestor["id"] == lineage_root["id"]:
                        fields["pipeline"] = root_pipeline
                    bus.update(ancestor["id"], **fields)
                    current = ancestor
                bus.update(task_id, status="done", merged_into=r["target"],
                           merged_via=f"fix round {fix_id} {r['sha']}", hold_reason=None)
        notify(f"{task_id} merged into {r['target']} ({r['sha'][:8]})")
    else:                                  # merge.merge already set the task failed with a resume_hint
        notify(f"{task_id} merge failed: {r.get('status')} {r.get('reason', '')}".strip())
    return r




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
    if already_merged(t):
        return
    all_reviews = [r for r in bus.read(role="review") if r["inputs"][:1] == [t["id"]]]
    pipeline = dict(t.get("pipeline") or {})
    reviewed_sha = pipeline.get("reviewed_sha")
    if reviewed_sha and t.get("worktree"):
        head = _git_in(t["worktree"], "rev-parse", "HEAD")
        current_sha = head.stdout.strip() if head.returncode == 0 else None
        if current_sha and current_sha != reviewed_sha:
            pipeline.update(reviewed_sha=current_sha, reviews_expected=1)
            bus.update(t["id"], pipeline=pipeline, status="done")
            _open_reviews(bus.get(t["id"]), 1, pipeline.get("review_reason", "semantic_path:changed"))
            notify(f"{t['id']}: approval void; branch head moved, fresh review opened")
            return
    reviews = [r for r in all_reviews if not reviewed_sha or r.get("reviewed_sha") == reviewed_sha]
    if not reviews:
        return  # gate() creates them; nothing to act on yet
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
                result = report_merge(t["id"], merge.merge(t["id"]))
                complete(t["id"], "merged_at")
                if result.get("status") != "merged":
                    if result.get("status") == "tests_red":
                        clear_stage(t["id"], "merged_at")
                    bus.update(t["id"], status="held", hold_reason=f"merge {result.get('status')}")
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


def _merged_target(t):
    return f"goal/{t['parent']}" if t.get("parent") else t.get("merged_into")


def sweep_leases(pool):
    """Reconcile expired post-lease claims. Old timestamp-only claims intentionally remain untouched."""
    now = time.time()
    for run_key in pool.sweep_reservations(now):
        print(f"[daemon] dropped dead expired budget reservation {run_key}", file=sys.stderr)
    for status in ("queued", "running", "done"):
        for t in bus.read(status=status):
            pipeline = t.get("pipeline") or {}
            for stage in LEASED_STAGES:
                lease = pipeline.get(f"{stage}_lease")
                if not lease or pipeline.get(f"{stage}_done") or lease > now:
                    continue
                tid = t["id"]
                if stage == "dispatched_at":
                    if status == "queued":
                        clear_stage(tid, stage)
                elif stage == "spec_review_at":
                    children = [r for r in bus.read(role="spec_review") if r["inputs"][:1] == [tid]]
                    if children and children[0]["status"] == "queued" and not children[0].get("claimed_at"):
                        spawn_async(spawn.run_worker, children[0]["id"])
                        complete(tid, stage)
                    elif children:
                        clear_stage(tid, stage)
                    else:
                        clear_stage(tid, stage)
                elif stage == "gated_at":
                    if already_merged(t):
                        complete(tid, stage, merged_into=_merged_target(t))
                        continue
                    expected = pipeline["reviews_expected"]
                    if expected == 0:
                        report_merge(tid, merge.merge(tid))
                        complete(tid, stage)
                        continue
                    children = [r for r in bus.read(role="review") if r["inputs"][:1] == [tid]]
                    if len(children) >= expected:
                        for child in children:
                            if child["status"] == "queued" and not child.get("claimed_at"):
                                spawn_async(spawn.run_worker, child["id"])
                    else:
                        _open_reviews(t, expected, pipeline["review_reason"])
                    complete(tid, stage)
                else:  # merged_at
                    if already_merged(t):
                        complete(tid, stage, merged_into=_merged_target(t))
                        continue
                    retries = pipeline.get("merge_retries", 0) + 1
                    if retries >= 2:
                        clear_stage(tid, stage, pipeline_fields={"merge_retries": retries},
                                    status="held", hold_reason="merge lease expired twice")
                    else:
                        clear_stage(tid, stage, pipeline_fields={"merge_retries": retries})


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


def tick(pool=None, stop_event=None):
    pool = pool or Pool()
    try:
        pool.tally_planner()
    except Exception as e:
        print(f"[daemon] tally_planner failed: {e}", file=sys.stderr)
    _load_review_cfg(pool)
    try:
        sweep_leases(pool)
    except Exception as e:
        print(f"[daemon] sweep_leases failed: {e}", file=sys.stderr)
    for t in bus.read(status="running"):
        if stop_event and stop_event.is_set():
            return
        if t.get("pid") and not alive(t["pid"]) and time.time() - t.get("claimed_at", 0) > 60:
            try:
                reconcile_dead(t, pool)
            except Exception as e:
                print(f"[daemon] reconcile {t['id']} failed: {e}", file=sys.stderr)
                continue
    for stage in (dispatch, gate, merge_reviewed):
        if stop_event and stop_event.is_set():
            return
        try:
            stage(pool)
        except Exception as e:
            print(f"[daemon] {stage.__name__} failed: {e}", file=sys.stderr)
    try:
        auto_fix_round(pool)
    except Exception as e:
        print(f"[daemon] auto_fix_round failed: {e}", file=sys.stderr)
    if pool.cfg.get("planner", {}).get("autonomous", False):
        try:
            planner_runs.reconcile()
            planner_runs.tick(pool)
        except Exception as e:
            print(f"[daemon] planner_runs failed: {e}", file=sys.stderr)
    m = pool.both_cooling_minutes()
    cooling = m > 30
    if pool.notification_transition("cooling", cooling) and cooling:
        notify(f"both Claude accounts cooling for {m:.0f} more min")
    for a in pool.accounts:
        over_budget = bool(a.daily_budget and a.day_tokens + a.planner_day_tokens >= a.daily_budget)
        if pool.notification_transition(f"daily_budget:{a.id}", over_budget) and over_budget:
            notify(f"account {a.id} hit its daily budget; tasks held")
    codex_cooling = not pool.codex_available() and pool.codex.cooling()
    if pool.notification_transition("codex_cooling", codex_cooling) and codex_cooling:
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
            tick(Pool(), stop_event)
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
    executor.join_fallback_threads(timeout)


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
        executor.join_fallback_threads()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
