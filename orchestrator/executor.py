"""Executor: GPT-6 Astra via ``codex exec``. One thread per atomic task; resume drives the bounded fix loop.

Observed ``codex exec resume --help`` options (2026-09-19): ``--config``, ``--last``, ``--all``, ``--enable``,
``--disable``, ``--image``, ``--strict-config``, ``--model``, ``--dangerously-bypass-approvals-and-sandbox``,
``--dangerously-bypass-hook-trust``, ``--worktree``, ``--thread-source``, ``--skip-git-repo-check``, ``--ephemeral``,
``--ignore-user-config``, ``--ignore-rules``, ``--output-schema``, ``--json``, ``--output-last-message``, and ``--help``.
Notably, resume accepts ``--json`` and the access flags, but not ``-C``; its process cwd selects the worktree.
Usage-limit errors cool Codex down and hold the task (§4.10).
"""
import inspect, json, re, subprocess, time
from pathlib import Path
from . import ROOT, bus
import threading
from . import scorecard, allocation, critical_path, duration, jev_route, decision_log, promotion, skill_router
from .pool import Pool, fallback_tier, is_rate_limited, parse_reset_hint

MAX_ROUNDS = 5
FALLBACK_JOIN_TIMEOUT_S = 5
_fallback_threads = []
_fallback_threads_lock = threading.Lock()


def _route_skills(task, cfg, exposure, choice=None):
    mode = promotion.mode("skill_routing", cfg)
    if mode not in ("shadow", "active"):
        return {}
    if choice is None:
        from .spawn import _prepare_skills
        choice = _prepare_skills(task, "codex_execute", cfg)
    presented = choice["presented"] if choice["mode"] == "active" else choice["selected"]
    decision_log.record("skill_selection", _state_target(task), role="codex_execute", candidates=choice["candidates"],
                        hard_constraints=choice["mandatory"],
                        deterministic={"triggers": choice["triggers"], "task_class": choice["task_class"],
                                       "mandatory": choice["mandatory"]}, selected=presented,
                        rejected=choice["rejected"], reason=choice["reason"], mode=choice["mode"],
                        extra={key: choice[key] for key in ("tokens_exposed_l0", "tokens_selected_l0",
                                                            "tokens_selected_l2", "ambiguous")}
                              | {"role": "codex_execute", "demoted": choice["demoted"]})
    return {"skills_selected": presented,
            "skill_tokens_selected_l0": choice["tokens_selected_l0"],
            "skill_tokens_selected_l2": choice["tokens_selected_l2"],
            "skill_tokens_presented_l2": choice["skill_tokens_presented_l2"],
            "skill_routing_mode": choice["mode"]}


def _prune_fallback_threads():
    """Drop completed fallback workers while holding the registry lock."""
    _fallback_threads[:] = [thread for thread in _fallback_threads if thread.is_alive()]


def fallback_threads():
    """Return the currently running Claude fallback worker threads."""
    with _fallback_threads_lock:
        _prune_fallback_threads()
        return tuple(_fallback_threads)


def join_fallback_threads(timeout=FALLBACK_JOIN_TIMEOUT_S):
    """Wait a bounded time for fallback workers, returning those still alive."""
    deadline = time.monotonic() + timeout
    for thread in fallback_threads():
        thread.join(max(0, deadline - time.monotonic()))
    return fallback_threads()


def argv_for(kind, args, cwd, access):
    """Return a Codex argv without spawning it; fresh exec preserves its historical byte shape."""
    if kind == "exec":
        return ["codex", "exec", *args, "--json", "-C", str(cwd), *access]
    if kind == "resume":
        resume_access = []
        i = 0
        while i < len(access):
            flag = access[i]
            if flag == "--dangerously-bypass-approvals-and-sandbox":
                resume_access.append(flag)
            elif flag in ("-s", "--sandbox") and i + 1 < len(access):
                i += 1
                resume_access.extend(["--config", f'sandbox_mode="{access[i]}"'])
            i += 1
        return ["codex", "exec", "resume", *args, "--json", *resume_access]
    raise ValueError(f"unknown codex command kind: {kind}")


def parse_events(lines):
    """codex exec --json: newline-delimited events. Observed: thread.started{thread_id}, turn.started, error{message},
    turn.failed{error}, item.completed{item}, turn.completed{usage}. Unknown types are ignored."""
    out = {"thread_id": None, "message": "", "usage": {}, "error": None}
    for l in lines:
        l = l.strip()
        if not l.startswith("{"):
            continue
        try:
            e = json.loads(l)
        except json.JSONDecodeError:
            continue
        t = e.get("type", "")
        if t == "thread.started":
            out["thread_id"] = e.get("thread_id")
        elif t == "error":
            out["error"] = e.get("message")
        elif t == "turn.failed":
            out["error"] = (e.get("error") or {}).get("message", out["error"])
        elif t == "turn.completed":
            out["usage"] = e.get("usage", {})
        elif t == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
            out["message"] = e["item"].get("text", "")
    return out


def _tokens(u):
    """Codex reports cached_input_tokens, Claude cache_read_input_tokens. Write the Claude key so cli.cost aggregates
    both providers uniformly; keep the Codex key for one release so older runs/*.jsonl readers keep working."""
    out = {k: u.get(k, 0) for k in ("input_tokens", "output_tokens", "cached_input_tokens")}
    out["cache_read_input_tokens"] = out["cached_input_tokens"]
    out.update(bus.normalize_usage("codex", u))
    return out


def _state_target(task):
    """Return the task whose execution state belongs to this run."""
    return task.get("_run_task_id", task["id"])


def _codex_skill_meta(message=""):
    from . import skills_registry
    records = skills_registry.load().get("skills", {})
    skill_id = "executor/implement-spec"
    record = records.get(skill_id, {})
    names = re.findall(r"(?im)^Skill used:\s*([^\s]+)\s*$", message)
    used = sorted({name if "/" in name else f"executor/{name}" for name in names
                   if (name if "/" in name else f"executor/{name}") in records})
    return {"skills_exposed": [skill_id], "skill_tokens_l0": int(record.get("est_tokens_l0") or 0),
            "skills_used": used,
            "skill_tokens_l2": sum(int(records[item].get("est_tokens_l2") or 0) for item in used)}


def _run(pool, task, args, cwd, timeout, ex=None):
    cfg = pool.cfg["codex"]
    # dangerous_full_access (pool.toml): user decision 2026-09-16; otherwise workspace-write sandbox (container-safe default)
    access = ["--dangerously-bypass-approvals-and-sandbox"] if cfg.get("dangerous_full_access") else ["-s", "workspace-write"]
    kind = "resume" if args and args[0] == "resume" else "exec"
    command_args = args[1:] if kind == "resume" else args
    cmd = argv_for(kind, command_args, cwd, access)
    log_task = _state_target(task)
    log = {"executor": ex.id if ex else "codex", "complexity": task["complexity"]}
    constraints = task.get("_run_constraints", task.get("constraints") or {})
    if constraints.get("fix_round_for"):
        log["resume_mode"] = task.get("_resume_mode", "fresh")
    log["prompt_chars"] = len(args[-1])
    from .spawn import packet_run_meta
    log["packet_meta"] = task.get("packet_meta") or packet_run_meta(packet_span(args[-1]))
    t0 = time.time()
    try:
        run_kwargs = {"capture_output": True, "text": True, "timeout": timeout}
        if kind == "resume":
            run_kwargs["cwd"] = cwd
        r = subprocess.run(cmd, **run_kwargs)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": f"timeout after {timeout}s"}
    if r.returncode == 2 and ("unexpected argument" in r.stderr or "Usage:" in r.stderr):
        reason = f"codex argv error: {r.stderr[-800:]}"
        bus.log_run(task=log_task, role="execute", tier=log["executor"], account="codex",
                    duration_s=round(time.time() - t0, 1), outcome="failed", reason=reason, **log)
        bus.update(_state_target(task), resume_hint={"argv_error": reason[:300]})
        return {"status": "failed", "reason": reason}
    ev = parse_events(r.stdout.splitlines() + r.stderr.splitlines())
    if ev["thread_id"]:
        # The head is a resume boundary, not merely result metadata: later replies must not
        # expose an old conversation to unrelated worktree changes.
        fields = {"codex_thread": ev["thread_id"]}
        head = _thread_head(cwd)
        if head:
            fields["codex_thread_head"] = head
        bus.update(task["id"], **fields)
    if ev["error"] and is_rate_limited(ev["error"]):
        secs = parse_reset_hint(ev["error"], pool.cfg["limits"]["cooldown_default_s"])
        if ex:
            pool.cooldown_executor(ex.id, secs, "codex usage limit")   # a usage limit is the quota group's, not one model's
        pool.codex.cooldown_until = time.time() + secs; pool.save()
        bus.update(_state_target(task), status="held", hold_reason=f"codex usage limit; resets in {secs // 60} min",
                   resume_hint={"thread": ev["thread_id"], "diff_stat": _diff_stat(cwd)})
        bus.log_run(task=log_task, role="execute", tier=log["executor"], account="codex", outcome="usage_limit",
                    cooldown_s=secs, **log)
        return {"status": "held", "reason": ev["error"], "resets_in_s": secs}
    u = ev["usage"]
    try:
        skill_meta = _codex_skill_meta(ev["message"])
        log["packet_meta"].update(skill_meta)
        pipeline = dict(bus.get(log_task).get("pipeline") or {})
        pipeline["skills_used"] = skill_meta["skills_used"]
        bus.update(log_task, packet_meta=log["packet_meta"], pipeline=pipeline)
        decision_log.outcome(log_task, "skill_selection", skills_used=skill_meta["skills_used"],
                             skill_tokens_l2=skill_meta["skill_tokens_l2"],
                             skill_recovery=sorted(set(skill_meta["skills_used"]) -
                                                   set(log["packet_meta"].get("skills_selected") or [])))
    except Exception:
        pass
    bus.log_run(task=log_task, role="execute", tier=log["executor"], account="codex", provider="codex", duration_s=round(time.time() - t0, 1),
                outcome="error" if ev["error"] else "done", usage=u, **log, **(_tokens(u) if u else {}))
    if ev["error"] or r.returncode:
        return {"status": "failed", "reason": ev["error"] or r.stderr[-800:], "thread": ev["thread_id"]}
    return {"status": "done", "thread": ev["thread_id"], "message": ev["message"][:6000], "usage": u}


def _diff_stat(cwd):
    return subprocess.run(["git", "diff", "--stat"], cwd=cwd, capture_output=True, text=True).stdout[-1500:]


def _thread_head(cwd):
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _resume_compatible(task):
    """A resumed thread may only see a clean descendant of its prior checkout head."""
    expected = task.get("codex_thread_head")
    cwd = task.get("worktree")
    if not cwd:
        return False, "worktree unavailable"
    # Tasks created before thread heads were recorded retain their historical resume behaviour.
    if not expected:
        return True, "legacy thread has no recorded head"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True)
    if status.returncode or status.stdout.strip():
        return False, "worktree is dirty"
    head = _thread_head(cwd)
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", expected, "HEAD"], cwd=cwd,
                              capture_output=True, text=True)
    return (bool(head and ancestor.returncode == 0), "worktree head moved" if head else "worktree HEAD unavailable")


def resume_plan(parent, fix_task):
    """Return the single authoritative routing decision for a fix round."""
    if not parent.get("codex_thread"):
        return {"mode": "fresh", "reason": "no_thread"}
    if parent.get("rounds", 0) >= MAX_ROUNDS:
        return {"mode": "fresh", "reason": "rounds_exhausted"}
    ex = Pool().executors.get(parent.get("executor") or "")
    if ex is None or ex.provider != "codex":
        return {"mode": "fresh", "reason": "executor_not_codex"}
    compatible, _ = _resume_compatible(parent)
    if not compatible:
        return {"mode": "fresh", "reason": "incompatible_worktree"}
    return {"mode": "resume", "reason": None}


def _commit_from_message(message):
    """Extract an explicitly reported commit, avoiding incidental short hexadecimal text."""
    full = re.search(r"\b[0-9a-f]{40}\b", message, re.I)
    if full:
        return full.group(0)
    labelled = re.search(
        r"\b(?:commit\s+sha|committed|commit|sha|HEAD\s+is\s+now\s+at)\b(?:\s*[:=]\s*|\s+)([0-9a-f]{7,40})\b",
        message, re.I,
    )
    if labelled:
        return labelled.group(1)
    backticked = re.search(r"\bCommit\b[^\n`]*`([0-9a-f]{7,40})`", message, re.I)
    return backticked.group(1) if backticked else None


def post_tool_result(task_id, result, replace_result=False):
    """Post a Codex result; fix-loop replies replace an unmerged task's prior round."""
    task = bus.get(task_id)
    if task.get("assigned_to") != "codex":
        return False, f"task is assigned to {task.get('assigned_to')}, not codex"
    if result.get("status") != "done":
        return False, f"codex result status is {result.get('status')}, not done"
    previous = task.get("result")
    if task.get("merged_into") is not None:
        return False, f"task is merged into {task.get('merged_into')}"
    if not replace_result:
        if previous is not None:
            return False, f"result exists from thread {previous.get('thread', 'unknown')}"
        if task.get("status") != "running":
            return False, f"task status is {task.get('status')}, not running"
    message = result.get("message", "")
    commit = _commit_from_message(message) or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=task["worktree"], capture_output=True, text=True, check=True
    ).stdout.strip()
    posted = {
        "summary": message[:3000],
        "commit": commit,
        "executed_by": "codex:" + task["executor"],
        "provenance": ["repo"],
        "usage": result.get("usage"),
        "thread": result.get("thread") or task.get("codex_thread"),
        "rounds": (previous.get("rounds", 1) + 1) if replace_result and previous else 1,
    }
    if replace_result and previous:
        posted["previous_commits"] = [*previous.get("previous_commits", []), previous.get("commit")]
    with bus.locked():
        if replace_result:
            pipeline = dict(bus.get(task_id).get("pipeline") or {})
            for key in list(pipeline):
                if key.startswith("gated_at") or key in ("hold_reason", "resume_hint"):
                    pipeline.pop(key)
            pipeline["regated_by"] = "codex_reply"
            bus.update(task_id, pipeline=pipeline, hold_reason=None, resume_hint=None)
        bus.post_result(task_id, posted, "done")
    return True, "result posted"


def start(task_id, prompt, executor_id=None, packet_meta=None):
    """Fresh Codex thread for one atomic task, in its worktree, on the executor pick_executor routes the task to.
    Held (not failed) when every executor in the task's complexity band is cooling, busy or over its daily budget.
    scores() is B3's ranking input; absent, every executor scores 1.0."""
    pool = Pool()
    result = None
    handed_off = False
    try:
        t = bus.get(task_id)
        previous = t.get("result")
        if previous is not None and (t.get("status") == "done" or t.get("merged_into") is not None):
            return {"status": "refused", "reason":
                    f"task {task_id} already has a result from thread {previous.get('thread', 'unknown')}; use codex_reply for a fix round"}
        if t.get("merged_into") is not None:
            return {"status": "refused", "reason": f"task {task_id} is merged into {t['merged_into']}"}
        if t.get("status") not in ("queued", "running", "done"):
            return {"status": "refused", "reason": f"task {task_id} status is {t.get('status')}; cannot claim"}
        if executor_id in pool.executors:
            ex = pool.executors[executor_id]
        else:
            try:
                scores = scorecard.scores(scorecard.build())
            except Exception:
                scores = {}
            baseline_scores = dict(scores)
            try:
                if pool.cfg.get("jev", {}).get("routing", {}).get("mode") == "active":
                    routing = jev_route.shadow_context(t, pool)
                    scores = jev_route.active_scores(t, pool, routing["eligible"],
                                                     routing["classification"], routing["evidence"], scores)
                ex = pool.pick_executor("execute", t["complexity"], scores=scores, task=t)
                mode = pool.cfg.get("allocation", {}).get("mode", "shadow")
                if mode != "off" and ex is not None:
                    graph = {x["id"]: x for x in bus.read(role="execute", compact=False)
                             if not x.get("merged_into") and x.get("status") != "failed"}
                    graph[t["id"]] = t
                    durations = duration.durations_for(list(graph.values()))
                    ready_ids = {x["id"] for x in graph.values()
                                 if x["status"] in ("queued", "running") and bus.ready(x)}
                    ready_ids.add(t["id"])
                    priority = critical_path.explain(t["id"], graph, durations)
                    ready_priorities = [critical_path.explain(i, graph, durations)["critical_path_s"]
                                        for i in sorted(ready_ids)]
                    eligible_ids = [e.id for e in pool.eligible_executors("execute", t["complexity"], t)]
                    ev = allocation.evidence_for(eligible_ids, scorecard.task_class(t))
                    choice = allocation.choose(t, eligible_ids, baseline=ex.id, priority=priority,
                                               ready_priorities=ready_priorities, evidence=ev, cfg=pool.cfg)
                    if choice["executor"] not in eligible_ids:
                        raise ValueError("allocation returned an ineligible executor")
                    factor_key = ("critical_factor" if priority["critical_path_s"] >= max(ready_priorities)
                                  else "non_critical_factor")
                    factor = pool.cfg.get("allocation", {}).get(factor_key, allocation.DEFAULTS[factor_key])
                    for candidate in choice["candidates"]:
                        candidate[factor_key] = factor
                    allocation.record(t["id"], choice)
                    # Preserve ranking inputs and candidate costs alongside the allocation decision.
                    decision_log.outcome(t["id"], "allocation", priority=priority,
                                         ready_priorities=ready_priorities, candidates=choice["candidates"])
                    if mode == "active":
                        ex = pool.executors[choice["executor"]]
            except Exception as exc:
                ex = pool.pick_executor("execute", t["complexity"], scores=baseline_scores, task=t)
                bus.log_run(task=task_id, role="execute", outcome="routing_fallback",
                            allocation_error=str(exc), executor=ex.id if ex else None)
        try:
            mode = pool.cfg.get("handoff", {}).get("mode", "off")
            if mode in ("shadow", "active"):
                from . import handoff_scorecard, notify
                eligible = pool.eligible_executors("execute", t["complexity"], t)
                candidates = [candidate.id for candidate in eligible]
                task_class = scorecard.task_class(t)
                costs = {candidate: handoff_scorecard.expected_route_cost(
                    task_class, candidate, cfg=pool.cfg) for candidate in candidates}
                decision_log.record(
                    "handoff", task_id, candidates=candidates,
                    hard_constraints=["pool.eligible_executors(role=execute, complexity, task)"],
                    deterministic={"baseline": ex.id if ex else None,
                                   "expected_route_cost": costs, "task_class": task_class},
                    historical={"n": sum(value.get("n", 0) for value in costs.values())},
                    selected=ex.id if ex else None, reason="shadow: routing unchanged", mode=mode)
                if mode == "active":
                    notify.notify_once(task_id, "handoff_active_shadow",
                                       f"{task_id}: handoff active is observation-only; routing unchanged")
        except Exception as exc:
            try:
                from . import notify
                notify.notify(f"{task_id}: handoff shadow unavailable: {exc}")
            except Exception:
                pass
        if ex is None or ex.provider != "codex":
            result = _exhausted(pool, t)
            handed_off = result.get("status") == "fallback"
            return result
        if pool.reserve(task_id, ex.id, "execute", t) is None:
            pipeline = dict(t.get("pipeline") or {})
            pipeline["hold_note"] = "budget"
            pipeline.pop("dispatched_at", None)
            bus.update(task_id, status="queued", pipeline=pipeline)
            return {"status": "budget", "reason": "budget reservation refused"}
        from .spawn import ensure_worktree
        wt = Path(t.get("worktree") or ensure_worktree(task_id))
        from .spawn import _prepare_skills, packet, packet_run_meta
        skill_choice = _prepare_skills(t, "codex_execute", pool.cfg)
        if skill_choice and skill_choice["mode"] == "active":
            briefing = packet(t, wt, cfg=pool.cfg, skills=skill_choice)
            old_packet = packet_span(prompt)
            prompt = prompt.replace(old_packet, briefing, 1) if old_packet else briefing + "\n" + prompt
        t = {**t, "packet_meta": packet_meta if packet_meta is not None else packet_run_meta(packet_span(prompt))}
        try:
            skill_meta = _codex_skill_meta()
            t["packet_meta"].update(skills_exposed=skill_meta["skills_exposed"],
                                    skill_tokens_l0=skill_meta["skill_tokens_l0"])
            if promotion.mode("skill_routing", pool.cfg) in ("shadow", "active"):
                t["packet_meta"].update(_route_skills(t, pool.cfg, skill_meta, skill_choice))
            else:
                decision_log.record("skill_selection", task_id, role="codex_execute", candidates=skill_meta["skills_exposed"],
                                hard_constraints=["static exposure (stage 1)"],
                                deterministic={"role": "codex_execute", "task_class": scorecard.task_class(t),
                                               "exposed": skill_meta["skills_exposed"],
                                               "skill_tokens_l0": skill_meta["skill_tokens_l0"]},
                                    selected=skill_meta["skills_exposed"], reason="stage1 static", mode="shadow")
        except Exception:
            pass
        bus.update(task_id, packet_meta=t["packet_meta"])
        bus.claim(task_id, "codex", str(wt)); bus.update(task_id, rounds=0, executor=ex.id, tier=ex.id)
        ex.roll_day(); ex.day_tasks += 1
        pool.codex.day_tasks += 1; pool.save()       # legacy mirror, until B3 drops pool.codex
        result = _run(pool, t, ["-m", ex.model, prompt], wt, t["constraints"].get("timeout_s", 1800), ex=ex)
        return result
    finally:
        # A Claude fallback inherits this run key's existing dispatch reservation.
        # Its run_worker() finally owns the matching release, so do not create the
        # gap where start() has returned but the worker has not yet claimed it.
        if not handed_off:
            pool.release(task_id, (result or {}).get("usage", {}))


def _exhausted(pool, t, run=None):
    """§4.10: hold by default; with on_exhausted=fallback_claude dispatch to sonnet (<=5) / opus (6-8) on an account with headroom.
    Complexity >=9 always holds for Astra. Review of a Claude-executed task must be another model on the other account."""
    pol = pool.cfg["codex"]["on_exhausted"]
    tier = fallback_tier(t["complexity"]) if pol == "fallback_claude" else None
    acct = pool.pick("execute")
    if tier is None or acct is None:
        bus.update(t["id"], status="held", hold_reason=f"codex unavailable; policy={pol}; no Claude fallback for complexity {t['complexity']}")
        return {"status": "held", "policy": pol, "codex": pool.status()["codex"]}
    from .spawn import run_worker
    bus.update(t["id"], tier=tier, fallback="claude", review_rule="same-family-review: other account, different model")
    worker = run or run_worker
    try:
        parameters = inspect.signature(worker).parameters.values()
        accepts_account = any(p.kind == inspect.Parameter.VAR_KEYWORD or
                              (p.name == "account_id" and p.kind != inspect.Parameter.POSITIONAL_ONLY)
                              for p in parameters)
    except (TypeError, ValueError):
        accepts_account = True
    kwargs = {"account_id": acct.id} if accepts_account else {}
    thread = None

    def run_fallback():
        try:
            worker(t["id"], **kwargs)
        finally:
            with _fallback_threads_lock:
                _fallback_threads.remove(thread)

    thread = threading.Thread(target=run_fallback, daemon=True)
    with _fallback_threads_lock:
        _prune_fallback_threads()
        _fallback_threads.append(thread)
    thread.start()
    return {"status": "fallback", "tier": tier, "note": "Claude is executing; result lands on the bus; label the PR same-family-review"}


def packet_span(text):
    """Return only the measurable packet prefix from a rendered or repair prompt."""
    match = re.search(r"(?m)^packet v[0-9a-f]+ base \S+ sources .+$", text)
    if not match:
        return ""
    start = match.start()
    boundary = text.find("\n\nRepair delta:\n", start)
    return text[start:boundary if boundary >= 0 else len(text)]


def reply(task_id, delta, packet_meta=None, fix_round_task_id=None, plan=None):
    """Run one bounded fix round and return its execution outcome.

    The parent owns the Codex thread and round counter; when ``fix_round_task_id``
    is supplied, the fix task owns execution status, reasons, holds, and hints.
    """
    t = bus.get(task_id)
    if fix_round_task_id is not None:
        fix = bus.get(fix_round_task_id)
        t = {**t, "_run_task_id": fix_round_task_id,
             "_run_constraints": fix.get("constraints") or {}, "_resume_mode": "resume"}
    if packet_meta is not None:
        t["packet_meta"] = packet_meta
    if t.get("merged_into") is not None:
        return {"status": "refused", "reason": f"task {task_id} is merged into {t['merged_into']}"}
    current_plan = resume_plan(bus.get(task_id), fix if fix_round_task_id is not None else t)
    if plan is not None and current_plan != plan:
        return {"status": "incompatible", "reason": current_plan["reason"]}
    pool = Pool()
    if not t.get("codex_thread"):
        return {"status": "failed", "reason": "task has no codex_thread; call codex() first"}
    rounds = t.get("rounds", 0) + 1
    if rounds > MAX_ROUNDS:
        bus.update(_state_target(t), status="failed", reason=f"fix loop exceeded {MAX_ROUNDS} rounds; escalate or re-spec")
        return {"status": "failed", "reason": "round budget exhausted"}
    ex = pool.executors.get(t.get("executor") or "") or pool._legacy_executor()  # pre-B2 tasks have no executor field
    # The task being resumed is itself one of the bus-derived running slots.
    if ex is None or ex.cooling() or ex.running > ex.max_parallel:
        bus.update(_state_target(t), status="held", hold_reason=f"executor {ex.id if ex else 'codex'} unavailable")
        return {"status": "held", "codex": pool.status()["codex"]}
    if current_plan["mode"] == "resume":
        args = ["resume", t["codex_thread"], delta]
    elif plan is not None or fix_round_task_id is not None:
        return {"status": "incompatible", "reason": current_plan["reason"]}
    else:
        # Preserve the public direct-reply fallback for callers that did not
        # pre-plan a fix task; daemon fix rounds never execute on the parent.
        from .spawn import packet, packet_run_meta
        briefing = packet(t, t["worktree"])
        t["packet_meta"] = packet_run_meta(briefing)
        repair = briefing + "\n\nRepair delta:\n" + delta
        bus.update(_state_target(t), resume_incompatible=current_plan["reason"])
        args = ["-m", ex.model, repair]
    if t.get("packet_meta"):
        bus.update(_state_target(t), packet_meta=t["packet_meta"])
    result = _run(pool, t, args, t["worktree"],
                  t["constraints"].get("timeout_s", 1800), ex=ex)
    if not result.get("reason", "").startswith("codex argv error:"):
        bus.update(task_id, rounds=rounds)
    return {"round": rounds, **result}
