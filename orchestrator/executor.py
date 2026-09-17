"""Executor: GPT-6 Astra via `codex exec` (Codex CLI 0.154.0 has no `codex mcp-server`). One thread per atomic task,
`codex exec resume <thread>` for the bounded fix loop. Usage-limit errors cool Codex down and hold the task (§4.10)."""
import json, subprocess, time
from pathlib import Path
from . import ROOT, bus
import threading
from .pool import Pool, fallback_tier, is_rate_limited, parse_reset_hint

MAX_ROUNDS = 5


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
    return out


def _run(pool, task, args, cwd, timeout, ex=None):
    cfg = pool.cfg["codex"]
    # dangerous_full_access (pool.toml): user decision 2026-09-16; otherwise workspace-write sandbox (container-safe default)
    access = ["--dangerously-bypass-approvals-and-sandbox"] if cfg.get("dangerous_full_access") else ["-s", "workspace-write"]
    cmd = ["codex", "exec", *args, "--json", "-C", str(cwd), *access]
    log = {"executor": ex.id if ex else "codex", "complexity": task["complexity"]}
    t0 = time.time()
    if ex:
        ex.running += 1
    pool.codex.running += 1; pool.save()          # legacy mirror, until B3 drops pool.codex
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": f"timeout after {timeout}s"}
    finally:
        if ex:
            ex.running -= 1
        pool.codex.running -= 1; pool.save()
    ev = parse_events(r.stdout.splitlines() + r.stderr.splitlines())
    if ev["thread_id"]:
        bus.update(task["id"], codex_thread=ev["thread_id"])
    if ev["error"] and is_rate_limited(ev["error"]):
        secs = parse_reset_hint(ev["error"], pool.cfg["limits"]["cooldown_default_s"])
        if ex:
            pool.cooldown_executor(ex.id, secs, "codex usage limit")   # a usage limit is the quota group's, not one model's
        pool.codex.cooldown_until = time.time() + secs; pool.save()
        bus.update(task["id"], status="held", hold_reason=f"codex usage limit; resets in {secs // 60} min",
                   resume_hint={"thread": ev["thread_id"], "diff_stat": _diff_stat(cwd)})
        bus.log_run(task=task["id"], role="execute", tier=log["executor"], account="codex", outcome="usage_limit",
                    cooldown_s=secs, **log)
        return {"status": "held", "reason": ev["error"], "resets_in_s": secs}
    u = ev["usage"]
    bus.log_run(task=task["id"], role="execute", tier=log["executor"], account="codex", duration_s=round(time.time() - t0, 1),
                outcome="error" if ev["error"] else "done", **log, **_tokens(u))
    if ev["error"] or r.returncode:
        return {"status": "failed", "reason": ev["error"] or r.stderr[-800:], "thread": ev["thread_id"]}
    return {"status": "done", "thread": ev["thread_id"], "message": ev["message"][:6000], "usage": u}


def _diff_stat(cwd):
    return subprocess.run(["git", "diff", "--stat"], cwd=cwd, capture_output=True, text=True).stdout[-1500:]


def start(task_id, prompt):
    """Fresh Codex thread for one atomic task, in its worktree, on the executor pick_executor routes the task to.
    Held (not failed) when every executor in the task's complexity band is cooling, busy or over its daily budget.
    scores() is B3's ranking input; absent, every executor scores 1.0."""
    pool = Pool(); t = bus.get(task_id)
    ex = pool.pick_executor("execute", t["complexity"], scores=getattr(pool, "scores", dict)())
    if ex is None or ex.provider != "codex":
        return _exhausted(pool, t)
    from .spawn import ensure_worktree
    wt = Path(t.get("worktree") or ensure_worktree(task_id))
    bus.claim(task_id, "codex", str(wt)); bus.update(task_id, rounds=0, executor=ex.id, tier=ex.id)
    ex.roll_day(); ex.day_tasks += 1
    pool.codex.day_tasks += 1; pool.save()       # legacy mirror, until B3 drops pool.codex
    return _run(pool, t, ["-m", ex.model, prompt], wt, t["constraints"].get("timeout_s", 1800), ex=ex)


def _exhausted(pool, t, run=None):
    """§4.10: hold by default; with on_exhausted=fallback_claude dispatch to sonnet (<=5) / opus (6-8) on an account with headroom.
    Complexity >=9 always holds for Astra. Review of a Claude-executed task must be another model on the other account."""
    pol = pool.cfg["codex"]["on_exhausted"]
    tier = fallback_tier(t["complexity"]) if pol == "fallback_claude" else None
    if tier is None or pool.pick("execute") is None:
        bus.update(t["id"], status="held", hold_reason=f"codex unavailable; policy={pol}; no Claude fallback for complexity {t['complexity']}")
        return {"status": "held", "policy": pol, "codex": pool.status()["codex"]}
    from .spawn import run_worker
    bus.update(t["id"], tier=tier, fallback="claude", review_rule="same-family-review: other account, different model")
    threading.Thread(target=run or run_worker, args=(t["id"],), daemon=True).start()
    return {"status": "fallback", "tier": tier, "note": "Claude is executing; result lands on the bus; label the PR same-family-review"}


def reply(task_id, delta):
    """Fix-loop round: resume the task's thread with a delta (failing tests + assertion lines) on the executor that
    started it — same thread, same model, never a re-pick mid-task. Capped at MAX_ROUNDS."""
    pool = Pool(); t = bus.get(task_id)
    if not t.get("codex_thread"):
        return {"status": "failed", "reason": "task has no codex_thread; call codex() first"}
    rounds = t.get("rounds", 0) + 1
    if rounds > MAX_ROUNDS:
        bus.update(task_id, status="failed", reason=f"fix loop exceeded {MAX_ROUNDS} rounds; escalate or re-spec")
        return {"status": "failed", "reason": "round budget exhausted"}
    ex = pool.executors.get(t.get("executor") or "") or pool._legacy_executor()  # pre-B2 tasks have no executor field
    if ex is None or ex.cooling() or ex.running >= ex.max_parallel:
        bus.update(task_id, status="held", hold_reason=f"executor {ex.id if ex else 'codex'} unavailable")
        return {"status": "held", "codex": pool.status()["codex"]}
    bus.update(task_id, rounds=rounds)
    return {"round": rounds, **_run(pool, t, ["resume", t["codex_thread"], delta], t["worktree"],
                                    t["constraints"].get("timeout_s", 1800), ex=ex)}
