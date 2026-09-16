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
    # Claude-as-executor (fallback only): may edit, scope-guard hook limits where; tests via the shared script
    "execute":   "Read,Grep,Glob,Edit,Write,Bash(git *),Bash(rg *),Bash(npm *),Bash(npx *),Bash(uv *),Bash(pytest *),Bash(python3 *),Bash(bash skills/*),Bash(.claude/hooks/tests-green.sh*),mcp__bus__bus_post_result",
}


def git(*a, cwd=ROOT, check=True):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(a)} (cwd={cwd}) failed: {r.stderr.strip()}")
    return r


def ensure_worktree(task_id, base="origin/main"):
    wt = ROOT / "wt" / task_id
    if not wt.exists():
        wt.parent.mkdir(exist_ok=True)
        git("fetch", "origin", check=False)
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
        bus.log_run(task=task["id"], role=task["role"], tier=task["tier"], account=acct.id, outcome="rate_limit", cooldown_s=secs)
        return {"status": "held", "reason": f"rate_limit on {acct.id}, cooling {secs}s"}
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        return {"status": "failed", "reason": f"non-JSON output (rc={p.returncode}): {text[-500:]}"}
    used = out.get("usage", {})
    n = used.get("input_tokens", 0) + used.get("output_tokens", 0) + used.get("cache_read_input_tokens", 0) // 10
    pool.record(acct, n)
    bus.log_run(task=task["id"], role=task["role"], tier=task["tier"], account=acct.id, duration_s=round(time.time() - t0, 1),
                outcome="done" if p.returncode == 0 else "error", usd=out.get("total_cost_usd"), **{k: used.get(k, 0) for k in
                ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")})
    return {"status": "done" if p.returncode == 0 else "failed", "output": out}


def extract_json(text):
    """Workers are told to return ONLY JSON; tolerate fences or prose around it."""
    s, e = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[s:e + 1]) if s >= 0 else {"summary": text[:2000]}
    except json.JSONDecodeError:
        return {"summary": text[:2000], "parse_error": True}


def run_worker(task_id):
    """Scout / triage / review / challenge: pick account, render prompt, run, post result. Holds instead of failing when no headroom."""
    pool = Pool(); t = bus.get(task_id); role = t["role"]
    acct = pool.pick(role)
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
    elif role == "execute":
        prompt = render("execute", spec=t["spec"], acceptance=t["acceptance"], scope=t["scope"]) + \
            "\nYou are a Claude fallback executor (Codex is unavailable); a human reviews merges. Commit on the task branch when green."
    else:
        prompt = render("scout", id=t["id"], title=t["title"], spec=t["spec"], acceptance=t["acceptance"],
                        turns=str(lim["max_turns"].get(role, 20)))
    bus.claim(task_id, f"claude:{acct.id}", str(ensure_worktree(task_id)))
    r = run_claude(pool, acct, t, prompt, model, TOOLS.get(role, TOOLS["scout"]),
                   lim["max_budget_usd"].get(role, 2.0), t["constraints"].get("timeout_s", lim["timeout_s"].get(role, 900)))
    if r["status"] == "done" and role == "execute":
        bus.post_result(task_id, {"summary": r["output"].get("result", "")[:3000], "executed_by": f"claude:{t['tier']}",
                                  "review": "other account, different model; label PR same-family-review"}, "done")
    elif r["status"] == "done":
        result = extract_json(r["output"].get("result", ""))
        bus.post_result(task_id, {"summary": result.get("summary", ""), **result}, "done")
    elif r["status"] == "held":
        bus.update(task_id, status="held", hold_reason=r["reason"])
    else:
        bus.update(task_id, status="failed", reason=r["reason"])
    return r


def scoped_diff(t):
    """Reviewers see -U3 hunks for the scoped paths of the task under review, never the repo."""
    src = bus.get(t["inputs"][0]) if t.get("inputs") else t
    wt = src.get("worktree") or ROOT
    r = git("diff", "-U3", "origin/main...HEAD", "--", *src["scope"], cwd=wt, check=False)
    return r.stdout[:40000] or "(empty diff)"
