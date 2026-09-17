"""Spawner: one `claude -p` subprocess per job, bound to one account via CLAUDE_CONFIG_DIR, in its own worktree,
with the role's .mcp.json and role-scoped secrets. Never shares or extracts credentials (Anthropic ToS: Claude Code is the harness)."""
import json, os, shutil, subprocess, time
from pathlib import Path
from . import ROOT, STATE, bus
from .pool import Pool, is_rate_limited, parse_reset_hint

TOOLS = {
    "scout":     "Read,Grep,Glob,Bash(git *),Bash(rg *),Bash(ls *),Bash(bash skills/*),mcp__bus__bus_post_result,mcp__bus__bus_read",
    "triage":    "Read,Grep,Glob,mcp__bus__bus_post_result",
    "review":    "Read,Grep,Glob,Bash(git *),mcp__bus__bus_post_result,mcp__bus__bus_read",
    "challenge": "Read,Grep,Glob,Bash(git *),Bash(rg *),mcp__bus__bus_post_result,mcp__bus__bus_read",
    "spec_review": "Read,Grep,Glob,Bash(git *),mcp__bus__bus_post_result,mcp__bus__bus_read",
    # Claude-as-executor (fallback only): may edit, scope-guard hook limits where; tests via the shared script
    "execute":   "Read,Grep,Glob,Edit,Write,Bash(git *),Bash(rg *),Bash(npm *),Bash(npx *),Bash(uv *),Bash(pytest *),Bash(python3 *),Bash(bash skills/*),Bash(.claude/hooks/tests-green.sh*),mcp__bus__bus_post_result",
}


def git(*a, cwd=ROOT, check=True):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(a)} (cwd={cwd}) failed: {r.stderr.strip()}")
    return r


def branch_exists(name):
    return git("rev-parse", "--verify", name, check=False).returncode == 0


def base_for(task):
    """Pick the base branch for a new worktree. review tasks whose inputs[0] is a task id: base on that task's
    own branch so the reviewer sees the code under review, not a worktree cut from origin/main before the
    reviewed task (or its dependency, B1-style) ever landed (review T-0026). Fall back to the reviewed task's
    goal branch, then origin/main. challenge tasks have inputs[0] = {claim, evidence, confidence} (a dict, or
    absent), never a task id, so they always base on the goal branch (falling back to origin/main) rather than
    the review path's isinstance(str) check, which never fires for them (review T-0030). execute tasks with a
    parent whose goal branch already exists stack on it, so the Planner no longer has to pre-create worktrees
    for stacked tasks."""
    role, parent = task["role"], task.get("parent")
    if role == "review" and task.get("inputs") and isinstance(task["inputs"][0], str):
        try:
            src = bus.get(task["inputs"][0])
        except KeyError:
            src = None
        if src is not None:
            branch = f"task/{task['inputs'][0]}"
            if branch_exists(branch):
                return branch
            src_parent = src.get("parent")
            if src_parent and branch_exists(f"goal/{src_parent}"):
                return f"goal/{src_parent}"
    elif role in ("challenge", "execute", "spec_review") and parent and branch_exists(f"goal/{parent}"):
        return f"goal/{parent}"
    return "origin/main"


def ensure_worktree(task_id, base=None):
    wt = ROOT / "wt" / task_id
    if not wt.exists():
        wt.parent.mkdir(exist_ok=True)
        git("fetch", "origin", check=False)
        if base is None:
            base = base_for(bus.get(task_id))
        if git("rev-parse", "--verify", base, check=False).returncode:
            base = "HEAD"  # no remote yet
        git("worktree", "add", str(wt), "-b", f"task/{task_id}", base)
    return wt


def render(name, **kw):
    t = (STATE / "prompts" / f"{name}.md").read_text()
    for k, v in kw.items():
        t = t.replace("{{" + k + "}}", v if isinstance(v, str) else json.dumps(v, indent=0))
    return t


def secrets_for_role(role):
    """pool.toml [secrets.<role>]: ENV_NAME = "bash command printing the value" (e.g. sourcing f.sh for `f tok get X --reveal`).
    Values never touch disk or logs."""
    out = {}
    for name, cmd in Pool().cfg.get("secrets", {}).get(role, {}).items():
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            out[name] = r.stdout.strip()
    return out


def trust_workspace(config_dir, wt):
    """Headless claude ignores project hooks/permissions in an untrusted workspace. Mark the worktree trusted in the
    profile's .claude.json, as the CLI's own warning suggests. Fresh profiles never saw a trust dialog."""
    cfg = Path(os.path.expanduser(config_dir)) / ".claude.json"
    data = json.loads(cfg.read_text()) if cfg.exists() else {}
    proj = data.setdefault("projects", {}).setdefault(str(wt), {})
    if not proj.get("hasTrustDialogAccepted"):
        proj["hasTrustDialogAccepted"] = True
        cfg.parent.mkdir(parents=True, exist_ok=True); cfg.write_text(json.dumps(data, indent=2))


def run_claude(pool, acct, task, prompt, model, tools, max_budget_usd, timeout):
    wt = Path(task.get("worktree") or ensure_worktree(task["id"]))
    trust_workspace(acct.config_dir, wt)
    role_cfg = ROOT / f".mcp.{task['role']}.json"
    if role_cfg.exists():
        shutil.copy(role_cfg, wt / ".mcp.json")
    env = {**os.environ, "CLAUDE_CONFIG_DIR": os.path.expanduser(acct.config_dir), "ORCH_TASK_ID": task["id"],
           "ORCH_ROOT": str(ROOT), **secrets_for_role(task["role"])}
    # claude 2.1.273 has no turn-cap flag; --max-budget-usd + subprocess timeout are the hard stops (§6.5)
    # Full access by user decision (2026-09-16): permissions bypassed; guardrails.sh + scope-guard.sh hooks are the floor.
    # Read-only roles still cannot edit: --disallowedTools is enforced even in bypass mode.
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json", "--max-budget-usd", str(max_budget_usd),
           "--dangerously-skip-permissions"]
    if task["role"] != "execute":
        cmd += ["--disallowedTools", "Edit,Write,NotebookEdit"]
    log = {"executor": task.get("executor") or f"claude:{task['tier']}", "complexity": task["complexity"]}
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, cwd=wt, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        bus.update(task["id"], pid=p.pid, account=acct.id)
        stdout, stderr = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        return {"status": "failed", "reason": f"timeout after {timeout}s"}
    text = stdout + stderr
    if p.returncode != 0 and is_rate_limited(text):
        secs = parse_reset_hint(text, pool.cfg["limits"]["cooldown_default_s"])
        pool.cooldown(acct, secs)
        bus.log_run(task=task["id"], role=task["role"], tier=task["tier"], account=acct.id, outcome="rate_limit",
                    cooldown_s=secs, **log)
        return {"status": "held", "reason": f"rate_limit on {acct.id}, cooling {secs}s"}
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        return {"status": "failed", "reason": f"non-JSON output (rc={p.returncode}): {text[-500:]}"}
    used = out.get("usage", {})
    n = used.get("input_tokens", 0) + used.get("output_tokens", 0) + used.get("cache_read_input_tokens", 0) // 10
    pool.record(acct, n)
    bus.log_run(task=task["id"], role=task["role"], tier=task["tier"], account=acct.id, duration_s=round(time.time() - t0, 1),
                outcome="done" if p.returncode == 0 else "error", usd=out.get("total_cost_usd"), **log,
                **{k: used.get(k, 0) for k in
                   ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")})
    return {"status": "done" if p.returncode == 0 else "failed", "output": out}


def extract_json(text):
    """Workers are told to return ONLY JSON; tolerate fences or prose around it."""
    s, e = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[s:e + 1]) if s >= 0 else {"summary": text[:2000]}
    except json.JSONDecodeError:
        return {"summary": text[:2000], "parse_error": True}


def fit_result(result, cap=bus.MAX_RESULT_CHARS):
    """Shrink an oversize worker result so it fits under the bus cap: truncate summary to 1,500 chars, then
    binary-search every list-valued top-level key (findings, risks, comments, ...) down to the longest prefix
    that fits, largest list first. Returns result unchanged when it already fits."""
    original_chars = len(json.dumps(result))
    if original_chars <= cap:
        return result
    out = dict(result)
    if isinstance(out.get("summary"), str):
        out["summary"] = out["summary"][:1500]
    trimmed = {}
    list_keys = sorted((k for k, v in out.items() if isinstance(v, list)),
                        key=lambda k: len(json.dumps(out[k])), reverse=True)
    for key in list_keys:
        items = out[key]
        lo, hi, best = 0, len(items), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            trial = {**out, key: items[:mid]}
            if len(json.dumps(trial)) <= cap - 200:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        out[key] = items[:best]
        if best < len(items):
            trimmed[key] = len(items) - best
    out["truncated"] = {"reason": "over MAX_RESULT_CHARS", "original_chars": original_chars, "trimmed": trimmed}
    return out


def run_worker(task_id):
    """Scout / triage / review / challenge: pick account, render prompt, run, post result. Holds instead of failing when no headroom."""
    pool = Pool(); t = bus.get(task_id); role = t["role"]
    avoid = None
    if role == "review" and t.get("inputs") and isinstance(t["inputs"][0], str):
        try:
            avoid = bus.get(t["inputs"][0]).get("account")
        except KeyError:
            avoid = None
    acct = pool.pick(role, avoid=avoid)
    if acct is None:
        bus.update(task_id, status="held", hold_reason="no account with headroom")
        return {"status": "held"}
    lim = pool.cfg["limits"]
    model = pool.cfg["models"][t["tier"]]
    if role == "review":
        prompt = render("review", complexity=str(t["complexity"]), acceptance=t["acceptance"], diff=scoped_diff(t),
                        security="Apply skills/review/adversarial-review/references/security-checklist.md." if t["complexity"] >= 7 else "")
    elif role == "challenge":
        prompt = render("challenge", **{k: t["inputs"][0].get(k, "") if t["inputs"] and isinstance(t["inputs"][0], dict) else t["spec"]
                                        for k in ("claim", "evidence", "confidence")})
    elif role == "spec_review":
        src = bus.get(t["inputs"][0])
        prompt = render("spec-review", complexity=str(t["complexity"]), spec=src["spec"], acceptance=src["acceptance"],
                        scope=src["scope"], code=code_excerpts(src["scope"], src.get("worktree") or ROOT))
    elif role == "execute":
        t["executor"] = f"claude:{t['tier']}"          # Codex was unavailable; the run log says which tier took it
        bus.update(task_id, executor=t["executor"])
        prompt = render("execute", spec=t["spec"], acceptance=t["acceptance"], scope=t["scope"]) + \
            "\nYou are a Claude fallback executor (Codex is unavailable); a human reviews merges. Commit on the task branch when green."
    else:
        prompt = render("scout", id=t["id"], title=t["title"], spec=t["spec"], acceptance=t["acceptance"],
                        turns=str(lim["max_turns"].get(role, 20)))
    bus.claim(task_id, f"claude:{acct.id}", str(ensure_worktree(task_id)))
    r = run_claude(pool, acct, t, prompt, model, TOOLS.get(role, TOOLS["scout"]),
                   lim["max_budget_usd"].get(role, 2.0), t["constraints"].get("timeout_s", lim["timeout_s"].get(role, 900)))
    try:
        if r["status"] == "done" and role == "execute":
            bus.post_result(task_id, fit_result({"summary": r["output"].get("result", "")[:3000], "executed_by": f"claude:{t['tier']}",
                                      "review": "other account, different model; label PR same-family-review"}), "done")
        elif r["status"] == "done":
            result = extract_json(r["output"].get("result", ""))
            bus.post_result(task_id, fit_result({"summary": result.get("summary", ""), **result}), "done")
            if role in ("review", "spec_review") and result.get("verdict"):
                verdict_fields = {"spec_review_verdict": result["verdict"], "spec_review_risks": result.get("risks", [])} \
                    if role == "spec_review" else {"review_verdict": result["verdict"]}
                bus.update(task_id, **verdict_fields)
                if t.get("inputs") and isinstance(t["inputs"][0], str):
                    try:
                        bus.update(t["inputs"][0], **verdict_fields)
                    except KeyError:
                        pass
        elif r["status"] == "held":
            bus.update(task_id, status="held", hold_reason=r["reason"])
        else:
            bus.update(task_id, status="failed", reason=r["reason"])
    except Exception as e:
        bus.log_run(task=task_id, role=role, outcome="post_failed",
                    executor=t.get("executor") or f"claude:{t['tier']}", complexity=t["complexity"])
        bus.update(task_id, status="failed", reason=f"post_result failed: {e}"[:500])
    return r


def code_excerpts(scope, base_dir, cap=12000):
    """First 120 lines of each file matching a scope glob under base_dir (the reviewed task's base branch
    worktree, or ROOT when none exists), joined and capped at `cap` chars total so a spec-review prompt stays
    a fixed size regardless of scope breadth. Lines are prefixed with their 1-based line number so a reviewer
    can cite path:line."""
    out, total = [], 0
    for pattern in scope:
        for path in sorted(Path(base_dir).glob(pattern)):
            if not path.is_file() or total >= cap:
                continue
            lines = "\n".join(f"{i:>4}| {line}" for i, line in
                               enumerate(path.read_text(errors="replace").splitlines()[:120], 1))
            chunk = f"--- {path.relative_to(base_dir)} ---\n{lines}\n"[:cap - total]
            out.append(chunk)
            total += len(chunk)
    return "".join(out) or "(no matching files)"


def scoped_diff(t):
    """Reviewers see -U3 hunks for the scoped paths of the task under review, never the repo. Diffs against the
    reviewed task's goal branch (when it exists) instead of origin/main, so a stacked task's review doesn't
    include its predecessor's already-merged hunks."""
    src = bus.get(t["inputs"][0]) if t.get("inputs") else t
    wt = src.get("worktree") or ROOT
    parent = src.get("parent")
    base = f"goal/{parent}" if parent and branch_exists(f"goal/{parent}") else "origin/main"
    r = git("diff", "-U3", f"{base}...HEAD", "--", *src["scope"], cwd=wt, check=False)
    return r.stdout[:40000] or "(empty diff)"
