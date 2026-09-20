"""Spawner: one `claude -p` subprocess per job, bound to one account via CLAUDE_CONFIG_DIR, in its own worktree,
with the role's .mcp.json and role-scoped secrets. Never shares or extracts credentials (Anthropic ToS: Claude Code is the harness)."""
import ast, hashlib, json, os, re, shutil, subprocess, sys, time
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
    for stacked tasks. A fix-round execute task (constraints.fix_round_for names the task it's fixing) instead
    cuts from that task's own task/<id> branch when it still exists, so the fix round starts on the code it is
    fixing rather than the goal branch the original may have already been merged past (gotchas.md 2026-09-19)."""
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
    elif role == "execute" and (task.get("constraints") or {}).get("fix_round_for") and \
            branch_exists(f"task/{task['constraints']['fix_round_for']}"):
        return f"task/{task['constraints']['fix_round_for']}"
    elif role in ("challenge", "execute", "spec_review", "scout", "triage") and parent and branch_exists(f"goal/{parent}"):
        return f"goal/{parent}"
    return "origin/main"


def ensure_worktree(task_id, base=None):
    wt = ROOT / "wt" / task_id
    registered = {
        Path(line[9:]).resolve()
        for line in git("worktree", "list", "--porcelain", check=False).stdout.splitlines()
        if line.startswith("worktree ")
    }
    if wt.exists() and wt.resolve() not in registered:
        git("worktree", "prune")
    if not wt.exists():
        wt.parent.mkdir(exist_ok=True)
        git("fetch", "origin", check=False)
        if base is None:
            base = base_for(bus.get(task_id))
        if git("rev-parse", "--verify", base, check=False).returncode:
            base = "HEAD"  # no remote yet
        branch = f"task/{task_id}"
        if branch_exists(branch):
            git("worktree", "add", str(wt), branch)
        else:
            git("worktree", "add", str(wt), "-b", branch, base)
    return wt


def render(name, **kw):
    if name == "scout":
        kw.setdefault("base_branch", "origin/main")
        kw.setdefault("base_sha", "(unavailable)")
    t = (STATE / "prompts" / f"{name}.md").read_text()
    for k, v in kw.items():
        t = t.replace("{{" + k + "}}", v if isinstance(v, str) else json.dumps(v, indent=0))
    return t


def bounded_text(text, cap_chars, expand_hint):
    """Keep a prompt blob bounded while leaving an actionable expansion command."""
    text = str(text)
    if len(text) <= cap_chars:
        return text
    hint = f"<{len(text) - cap_chars} more chars; expand with: {expand_hint}>"
    keep = max(0, cap_chars - len(hint) - 1)
    return text[:keep].rstrip() + "\n" + hint


def bounded_diff(diff_text, cap_chars=12000, expand_hint="git diff"):
    """Put a compact diffstat before bounded unified-diff hunks."""
    text = str(diff_text)
    files = additions = deletions = 0
    hunks = []
    current = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            files += 1
        if line.startswith("@@"):
            if current:
                hunks.extend(current); current = []
            current.append(line)
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        if line.startswith("-"):
            deletions += 1
        if line.startswith(("diff --git ", "index ", "@@", "+", "-", " ")):
            current.append(line)
    hunks.extend(current)
    summary = f"Diffstat: {files} files changed, {additions} insertions(+), {deletions} deletions(-)"
    body_lines = [summary] + hunks
    body = "\n".join(body_lines)
    if len(body) <= cap_chars:
        return body
    marker_size = len(f"\n0 more lines; expand with: {expand_hint}")
    kept = [summary]
    for line in hunks:
        candidate = "\n".join(kept + [line])
        if len(candidate) + marker_size > cap_chars:
            break
        kept.append(line)
    omitted = len(hunks) - (len(kept) - 1)
    marker = f"{omitted} more lines; expand with: {expand_hint}"
    while len("\n".join(kept) + "\n" + marker) > cap_chars and len(kept) > 1:
        kept.pop()
        omitted += 1
        marker = f"{omitted} more lines; expand with: {expand_hint}"
    return "\n".join(kept) + "\n" + marker


def _memory_entries(path):
    """Return (line, title, body) tuples from a small, heading-based memory file."""
    if not path.exists():
        return []
    lines = path.read_text(errors="replace").splitlines()
    starts = [(i, line.lstrip("# ").strip()) for i, line in enumerate(lines)
              if re.match(r"^#{1,3}\s+", line)]
    return [(i + 1, title, "\n".join(lines[i + 1:starts[n + 1][0] if n + 1 < len(starts) else len(lines)]))
            for n, (i, title) in enumerate(starts)]


def _packet_body(task, worktree) -> tuple[str, dict]:
    """Build the executor's bounded, deterministic briefing solely from task/repository data."""
    wt = Path(worktree)
    scope = [str(p) for p in task.get("scope", [])]
    scope_files = [wt / p for p in scope if (wt / p).is_file()]
    py_files = [p for p in scope_files if p.suffix == ".py"]
    symbols, symbol_names, imported_paths = [], set(), set()
    for path in py_files:
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (OSError, SyntaxError):
            continue
        rel = path.relative_to(wt)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append(f"- {rel}:{node.lineno} {node.name}")
                symbol_names.add(node.name)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    if not name:
                        continue
                    stem = Path(*name.split("."))
                    for candidate in (wt / stem.with_suffix(".py"), wt / stem / "__init__.py"):
                        if candidate.is_file():
                            imported_paths.add(str(candidate.relative_to(wt)))

    read_scope = {"tests/", *imported_paths}
    read_scope.update(str(Path(p).parent) + ("/" if str(Path(p).parent) != "." else "") for p in scope)
    # Scope-owned test files are the most useful starting point for an executor.
    # Keep them first and outside the discovery cap: a broad symbol such as
    # ``get`` must never hide a test explicitly named in a task's scope.
    tests = []
    for item in scope:
        path = Path(item)
        if len(path.parts) > 1 and path.parts[0] == "tests" and (wt / path).is_file():
            tests.append(str(path))
    for path in py_files:
        candidate = wt / "tests" / f"test_{path.stem}.py"
        if candidate.is_file():
            tests.append(str(candidate.relative_to(wt)))
    tests = list(dict.fromkeys(tests))

    stop_symbols = {"get", "read", "update", "events", "claim", "db", "locked", "next_id",
                    "main", "run", "load", "save", "path", "root", "task", "tasks", "result", "status"}
    relevant_symbols = {name for name in symbol_names if len(name) >= 5 and name not in stop_symbols}
    module_names = {path.stem for path in py_files if "tests" not in path.parts}
    qualified = {
        name: re.compile(rf"(?<!\w){re.escape(name)}\s*\(|(?:"
                         + "|".join(re.escape(module) for module in sorted(module_names))
                         + rf")\.{re.escape(name)}\b")
        for name in relevant_symbols
    } if module_names else {
        name: re.compile(rf"(?<!\w){re.escape(name)}\s*\(") for name in relevant_symbols
    }
    matches = []
    for test_file in sorted((wt / "tests").glob("test_*.py")) if (wt / "tests").is_dir() else []:
        try:
            source = test_file.read_text(errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                body = ast.get_source_segment(source, node) or ""
                specificity = sum(bool(pattern.search(body)) for pattern in qualified.values())
                if specificity:
                    matches.append((-specificity, str(test_file.relative_to(wt)), node.name))
    for _, filename, name in sorted(matches):
        if len(tests) >= 15:
            break
        entry = f"{filename}::{name}"
        if entry not in tests:
            tests.append(entry)

    parent = task.get("parent") or "(none)"
    goal_branch = f"goal/{parent}" if task.get("parent") else "origin/main"
    branch = git("branch", "--show-current", cwd=wt, check=False).stdout.strip() or "(detached)"
    merge_base = git("merge-base", "HEAD", goal_branch, cwd=wt, check=False).stdout.strip()
    if not merge_base:
        merge_base = git("rev-parse", "HEAD", cwd=wt, check=False).stdout.strip() or "(unavailable)"
    terms = {p.lower() for p in scope}
    terms.update(Path(p).stem.lower() for p in scope)
    gotchas = []
    for candidate in (wt / ".orchestrator/memory/gotchas.md", wt / "gotchas.md"):
        for line, title, body in _memory_entries(candidate):
            if any(term and term in f"{title}\n{body}".lower() for term in terms):
                gotchas.append(f"- mem:gotchas.md:{line} {title}")
        if candidate.exists():
            break
    matched_gotchas = list(gotchas)
    decisions = []
    for candidate in (wt / ".orchestrator/memory/decisions.md", wt / "decisions.md"):
        decisions = [f"- {title}" for _, title, body in _memory_entries(candidate)
                     if parent != "(none)" and parent.lower() in f"{title}\n{body}".lower()][:3]
        if candidate.exists():
            break
    evidence = []
    for item in task.get("inputs", []):
        value = item
        if isinstance(item, str):
            try:
                value = bus.get(item).get("result") or {}
            except KeyError:
                value = {}
        summary = value.get("summary", value) if isinstance(value, dict) else value
        evidence.append(f"- {item if isinstance(item, str) else 'input'}: {str(summary)[:200]}")

    objective = [str(task.get("title", ""))]
    if task.get("spec"):
        objective.append(f"- discovery: {task['spec']}")
    sections = [
        ("objective", objective),
        ("acceptance", [f"- {x}" for x in task.get("acceptance", [])]),
        ("base", [f"- branch: {branch}", f"- merge-base {goal_branch}: {merge_base}", f"- goal: {parent}"]),
        ("write_scope", [f"- {p}" for p in scope]),
        ("read_scope", [f"- {p or '.'}" for p in sorted(read_scope)]),
        ("constraints", [f"- {key}: {json.dumps(value, sort_keys=True)}"
                         for key, value in sorted((task.get("constraints") or {}).items())] or ["- (none)"]),
        ("relevant_tests", [f"- {p}" for p in tests] or ["- (none found)"]),
        ("symbols", symbols[:40] or ["- (none)"]),
        ("gotchas", gotchas[:5] or ["- (none)"]),
        ("decisions", decisions or ["- (none)"]),
        ("verify", ["- .claude/hooks/tests-green.sh .", "- On failure, report only scripts/failures_only.sh output."]),
        ("evidence", evidence or ["- (none)"]),
    ]
    def build():
        return "\n".join(f"## {name}\n" + "\n".join(lines) for name, lines in sections)
    # The task contract is more valuable than discovery hints.  In particular,
    # acceptance criteria are never summarized: an over-cap packet says so in
    # its provenance header instead.
    trimmable = ("evidence", "decisions", "gotchas", "symbols", "relevant_tests")
    by_name = {name: lines for name, lines in sections}
    while len(build()) >= 4800:
        changed = False
        for name in trimmable:
            lines = by_name[name]
            if lines:
                lines.pop()
                changed = True
                break
        if changed:
            continue
        # Keep the objective title, but trim any supplemental discovery text.
        if len(objective) > 1:
            objective.pop()
            continue
        break
    body = build()
    gotchas_sha = hashlib.sha256("\n".join(matched_gotchas).encode()).hexdigest()[:12]
    policy_version = getattr(bus, "policy_version", lambda: None)()
    if not policy_version:
        pool_path = wt / "pool.toml"
        if not pool_path.is_file():
            pool_path = ROOT / "pool.toml"
        try:
            policy_version = hashlib.sha256(pool_path.read_bytes()).hexdigest()[:12]
        except OSError:
            policy_version = "(unavailable)"
    return body, {"hash": hashlib.sha256(body.encode()).hexdigest()[:12], "base": merge_base[:12],
                  "policy_version": str(policy_version), "gotchas": gotchas_sha}


def packet_meta(task, worktree) -> dict:
    """Provenance values for the packet and its corresponding run record."""
    return _packet_body(task, worktree)[1]


def packet(task, worktree) -> str:
    """Build a bounded executor briefing with a verifiable provenance header."""
    body, meta = _packet_body(task, worktree)
    header = (f"packet v{meta['hash']} base {meta['base']} sources "
              f"pool.toml@{meta['policy_version']} gotchas@{meta['gotchas']}")
    # Account for the header itself, including a possible extra digit in n.
    over = len(header) + 1 + len(body) - 4800
    if over > 0:
        while True:
            extended = f"{header} over cap by {over} chars: acceptance kept whole"
            actual = len(extended) + 1 + len(body) - 4800
            if actual == over:
                header = extended
                break
            over = actual
    return header + "\n" + body


def resolve_secrets(mapping: dict[str, str]) -> dict[str, str]:
    """mapping: ENV_NAME -> "bash command printing the value" (e.g. sourcing f.sh for `f tok get X --reveal`),
    or ENV_NAME = "env:OTHER_NAME" to read OTHER_NAME straight from this process's environment (headless hosts: no
    Keychain, no `f tok get`). Values never touch disk or logs."""
    out = {}
    for name, cmd in mapping.items():
        if cmd.startswith("env:"):
            var = cmd[len("env:"):]
            val = os.environ.get(var)
            if val:
                out[name] = val
            else:
                print(f"resolve_secrets: env var {var} is not set, skipping {name}", file=sys.stderr)
            continue
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            out[name] = r.stdout.strip()
    return out


def secrets_for_role(role):
    """pool.toml [secrets.<role>]. See resolve_secrets for the value-form rules."""
    return resolve_secrets(Pool().cfg.get("secrets", {}).get(role, {}))


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
    # Explicit --mcp-config + --strict-mcp-config means workers never auto-load the project/user configs
    # (github, orchestrator MCP schemas cost tokens every turn a worker never needs them). Role-specific
    # override (.mcp.<role>.json) wins when present; every other role gets the bus-only worker config.
    role_cfg = ROOT / f".mcp.{task['role']}.json"
    mcp_config = role_cfg if role_cfg.exists() else ROOT / ".mcp.worker.json"
    env = {**os.environ, "CLAUDE_CONFIG_DIR": os.path.expanduser(acct.config_dir), "ORCH_TASK_ID": task["id"],
           "ORCH_ROOT": str(ROOT), **secrets_for_role(task["role"])}
    # Headless hosts: `claude setup-token` issues a long-lived CLAUDE_CODE_OAUTH_TOKEN per CLAUDE_CONFIG_DIR,
    # set in this process's environment under the name pool.toml's oauth_token_env points at. Never logged.
    if acct.oauth_token_env and os.environ.get(acct.oauth_token_env):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ[acct.oauth_token_env]
    # claude 2.1.273 has no turn-cap flag; --max-budget-usd + subprocess timeout are the hard stops (§6.5)
    # Full access by user decision (2026-09-16): permissions bypassed; guardrails.sh + scope-guard.sh hooks are the floor.
    # Read-only roles still cannot edit: --disallowedTools is enforced even in bypass mode.
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json", "--max-budget-usd", str(max_budget_usd),
           "--dangerously-skip-permissions", "--allowedTools", tools,
           "--strict-mcp-config", "--mcp-config", str(mcp_config)]
    if task["role"] != "execute":
        cmd += ["--disallowedTools", "Edit,Write,NotebookEdit"]
    log = {"executor": task.get("executor") or f"claude:{task['tier']}", "complexity": task["complexity"]}
    if task.get("packet_meta"):
        log["packet_meta"] = task["packet_meta"]
    if shutil.which("claude") is None:
        bus.log_run(task=task["id"], role=task["role"], tier=task["tier"], account=acct.id, outcome="no_cli", **log)
        return {"status": "held", "reason": "claude CLI not found on PATH"}
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, cwd=wt, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        bus.update(task["id"], pid=p.pid, account=acct.id)
        stdout, stderr = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        return {"status": "failed", "reason": f"timeout after {timeout}s"}
    except FileNotFoundError:
        # shutil.which above should already catch this (gotcha 2026-09-19: a dead worker thread never
        # requeues cleanly), but a TOCTOU race (claude removed from PATH between the check and Popen) lands here.
        bus.log_run(task=task["id"], role=task["role"], tier=task["tier"], account=acct.id, outcome="no_cli", **log)
        return {"status": "held", "reason": "claude CLI not found on PATH"}
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
                outcome="done" if p.returncode == 0 else "error", usd=out.get("total_cost_usd"), turns=out.get("num_turns", 0),
                provider="claude", **log, **{**used, **bus.normalize_usage("claude", used)})
    if not out.get("is_error") and p.returncode == 0:
        return {"status": "done", "output": out}
    reason = f"budget or error exit (rc={p.returncode}): " + (out.get("result") or "")[:500]
    return {"status": "failed", "output": out, "reason": reason}


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


def _account_from_assigned_to(assigned_to):
    """"claude:<acct id>" -> "<acct id>"; anything else (None, "codex", ...) -> None."""
    if assigned_to and assigned_to.startswith("claude:"):
        return assigned_to[len("claude:"):]
    return None


def run_worker(task_id, account_id=None):
    """Scout / triage / review / challenge: pick account, render prompt, run, post result. Holds instead of failing when no headroom."""
    pool = Pool(); t = bus.get(task_id); role = t["role"]
    avoid = None
    reviewed = None
    if role == "review" and t.get("inputs") and isinstance(t["inputs"][0], str):
        try:
            reviewed = bus.get(t["inputs"][0])
        except KeyError:
            reviewed = None
        if reviewed is not None:
            # Older/in-flight tasks may not have an explicit "account" field yet; fall back to parsing the
            # account id out of assigned_to ("claude:<acct id>") so a fallback execution is never reviewed on
            # the same account it ran on.
            avoid = reviewed.get("account") or _account_from_assigned_to(reviewed.get("assigned_to"))
    acct = next((a for a in pool.accounts if a.id == account_id), None) if account_id else pool.pick(role, avoid=avoid)
    if acct is None:
        bus.update(task_id, status="held", hold_reason="no account with headroom")
        return {"status": "held"}
    lim = pool.cfg["limits"]
    model = pool.cfg["models"][t["tier"]]
    if role == "review":
        src = reviewed if reviewed is not None else t
        wt = src.get("worktree") or ROOT
        diff_hint = f"git -C {wt} diff -- {' '.join(src['scope'])}"
        prompt = render("review", complexity=str(t["complexity"]), acceptance=t["acceptance"],
                        diff=bounded_diff(scoped_diff(src), lim.get("review_diff_chars", 12000), diff_hint),
                        security="Apply skills/review/adversarial-review/references/security-checklist.md." if t["complexity"] >= 7 else "")
    elif role == "challenge":
        prompt = render("challenge", **{k: t["inputs"][0].get(k, "") if t["inputs"] and isinstance(t["inputs"][0], dict) else t["spec"]
                                        for k in ("claim", "evidence", "confidence")})
    elif role == "spec_review":
        src = bus.get(t["inputs"][0])
        code_wt = src.get("worktree") or ROOT
        code_hint = f"git -C {code_wt} show HEAD:<path>"
        prompt = render("spec-review", complexity=str(t["complexity"]), spec=src["spec"], acceptance=src["acceptance"],
                        scope=src["scope"], code=bounded_text(code_excerpts(src["scope"], code_wt),
                                                              lim.get("spec_review_code_chars", 8000), code_hint))
    elif role == "execute":
        t["executor"] = f"claude:{t['tier']}"          # Codex was unavailable; the run log says which tier took it
        bus.update(task_id, executor=t["executor"])
        packet_worktree = t.get("worktree") or ROOT
        t["packet_meta"] = packet_meta(t, packet_worktree)
        prompt = render("execute", packet=packet(t, packet_worktree), spec=t["spec"],
                        acceptance=t["acceptance"], scope=t["scope"]) + \
            "\nYou are a Claude fallback executor (Codex is unavailable); a human reviews merges. Commit on the task branch when green."
    else:
        base = base_for(t)
        base_sha = git("rev-parse", base, check=False).stdout.strip() or "(unavailable)"
        prompt = render("scout", id=t["id"], title=t["title"], spec=t["spec"], acceptance=t["acceptance"],
                        turns=str(lim["max_turns"].get(role, 20)), base_branch=base, base_sha=base_sha)
    if pool.reserve(task_id, acct.id, role, t) is None:
        pipeline = dict(t.get("pipeline") or {})
        pipeline["hold_note"] = "budget"
        bus.update(task_id, status="queued", pipeline=pipeline)
        return {"status": "budget"}
    bus.claim(task_id, f"claude:{acct.id}", str(ensure_worktree(task_id)))
    bus.update(task_id, account=acct.id)  # explicit account, alongside assigned_to, for the avoid-derivation above
    r = None
    try:
        r = run_claude(pool, acct, t, prompt, model, TOOLS.get(role, TOOLS["scout"]),
                       lim["max_budget_usd"].get(role, 2.0),
                       t["constraints"].get("timeout_s", lim["timeout_s"].get(role, 900)))
        if r["status"] == "done" and role == "execute":
            bus.post_result(task_id, fit_result({"summary": r["output"].get("result", "")[:3000], "executed_by": f"claude:{t['tier']}",
                                      "review": "other account, different model; label PR same-family-review"}), "done")
        elif r["status"] == "done":
            text = r["output"].get("result", "")
            result = extract_json(text)
            review_role = role in ("review", "spec_review")
            # A review worker may post its verdict itself via bus_post_result mid-run, then end with prose or
            # fenced JSON this parser can't take; or return valid JSON that simply lacks "verdict". Either way
            # treat it as a failed parse for review roles so we never silently drop an already-posted verdict
            # (T-0139: an approve sat unmerged after a second, verdict-less post overwrote the first).
            parse_failed = bool(result.get("parse_error")) or (review_role and not result.get("verdict"))
            existing_result = (bus.get(task_id).get("result") or {}) if parse_failed else {}
            if parse_failed and existing_result.get("verdict"):
                result = existing_result  # keep the worker's own posted result; do not overwrite it
            elif parse_failed and review_role:
                bus.update(task_id, status="failed", reason="review returned no parseable verdict",
                          resume_hint={"raw": text[-2000:]})
                result = None
            else:
                bus.post_result(task_id, fit_result({"summary": result.get("summary", ""), **result}), "done")
            if result and review_role and result.get("verdict"):
                verdict_fields = {"spec_review_verdict": result["verdict"], "spec_review_risks": result.get("risks", [])} \
                    if role == "spec_review" else {"review_verdict": result["verdict"]}
                bus.update(task_id, **verdict_fields)
                if t.get("inputs") and isinstance(t["inputs"][0], str):
                    try:
                        bus.update(t["inputs"][0], **verdict_fields)
                    except KeyError:
                        pass
        elif r["status"] == "held":
            bus.update(task_id, status="held", hold_reason=r.get("reason", "unknown failure"))
        else:
            update_fields = {"status": "failed", "reason": r.get("reason", "unknown failure")}
            result = r.get("output", {}).get("result") if isinstance(r.get("output"), dict) else None
            if isinstance(result, str):
                update_fields["resume_hint"] = {"partial_output": result[:2000]}
            bus.update(task_id, **update_fields)
    except Exception as e:
        bus.log_run(task=task_id, role=role, outcome="post_failed",
                    executor=t.get("executor") or f"claude:{t['tier']}", complexity=t["complexity"])
        bus.update(task_id, status="failed", reason=f"post_result failed: {e}"[:500])
    finally:
        pool.release(task_id, r or {})
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


def scoped_diff(src):
    """Reviewers see -U3 hunks for the scoped paths of the task under review, never the repo. Diffs against the
    reviewed task's goal branch (when it exists) instead of origin/main, so a stacked task's review doesn't
    include its predecessor's already-merged hunks."""
    wt = src.get("worktree") or ROOT
    parent = src.get("parent")
    base = f"goal/{parent}" if parent and branch_exists(f"goal/{parent}") else "origin/main"
    r = git("diff", "-U3", f"{base}...HEAD", "--", *src["scope"], cwd=wt, check=False)
    return r.stdout[:40000] or "(empty diff)"
