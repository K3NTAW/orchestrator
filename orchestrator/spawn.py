"""Spawner: one `claude -p` subprocess per job, bound to one account via CLAUDE_CONFIG_DIR, in its own worktree,
with the role's .mcp.json and role-scoped secrets. Never shares or extracts credentials (Anthropic ToS: Claude Code is the harness)."""
import logging
import ast, hashlib, importlib.util, json, os, re, shutil, subprocess, sys, time
from collections import OrderedDict
from pathlib import Path
from . import contracts, worker_registry, env_policy, worker_control
from . import harness_depth, memory_hot, memory_store
from . import ROOT, STATE, attribution, bus, decision_log, evidence, instructions, notify, promotion, skill_router, specialist, skill_scorecard, tool_catalog, skills_registry
from .pool import Pool, is_rate_limited, parse_reset_hint

_MEMORY_RECALL = None
_PACKET_BUILD_META_MAX = 512
_PACKET_BUILD_META = OrderedDict()
_INSTRUCTION_RENDER_META = OrderedDict()
NEEDS_TOOL_PREFIX = "needs_tool:"

_SKILL_EVIDENCE_WINDOW_S = 7 * 24 * 60 * 60
_SKILL_PRESENTATION_CAP = 2400


def _skill_records():
    return skills_registry.load().get("skills", {})


def _skill_exposure(task, role):
    """Stage-1 exposure mirrors the shared Claude skills directory; it does not route skills."""
    records = _skill_records()
    exposed = sorted(skill_id for skill_id, record in records.items()
                     if record.get("state") == "active" and record.get("provenance") == "builtin")
    tokens = sum(int(records[skill_id].get("est_tokens_l0") or 0) for skill_id in exposed)
    decision_log.record("skill_selection", task["id"], role=role, candidates=sorted(records),
                        hard_constraints=["static exposure (stage 1)"],
                        deterministic={"role": role, "task_class": attribution.task_class(task),
                                       "exposed": exposed, "skill_tokens_l0": tokens},
                        selected=exposed, reason="stage1 static", mode="shadow")
    return {"skills_exposed": exposed, "skill_tokens_l0": tokens}


def _prepare_skills(task, role, cfg):
    """Select once, apply the active safety gate, and prepare bounded rendering."""
    if harness_depth.active(task):
        return None
    mode = promotion.mode("skill_routing", cfg)
    if mode not in ("shadow", "active"):
        return None
    composition = specialist.compose(task, role, cfg=cfg)
    choice = composition.decision
    choice["_specialist"] = composition
    choice["mode"] = mode
    if role not in ("execute", "review", "security_review", "codex_execute"):
        choice["mode"] = "shadow"
    if choice["mode"] == "active":
        rows = skill_scorecard.selection_rows(STATE, role, _SKILL_EVIDENCE_WINDOW_S)
        recovery = skill_scorecard.recovery_rate(STATE, role, _SKILL_EVIDENCE_WINDOW_S)
        maximum = float((cfg.get("skills") or {}).get("max_recovery", .10))
        if rows < 30 or recovery > maximum:
            choice["mode"] = "shadow"
            notify.notify(f"{task['id']}: active skill routing refused; using shadow "
                          f"(role={role}, rows={rows}, recovery={recovery:.3f})")
    choice.update(_skills_section(choice) if choice["mode"] == "active"
                  else {"section": "", "presented": [], "demoted": [], "skill_tokens_presented_l2": 0})
    catalog = skill_router.catalog_block(role, _skill_records())
    choice["catalog_chars"] = len(catalog)
    choice["selected_chars"] = 0
    for item in choice["selected"]:
        try:
            choice["selected_chars"] += len(skills_registry.render(item, 2))
        except (KeyError, OSError):
            # Missing registry bodies must not make shadow telemetry block dispatch.
            continue
    try:
        cache_mode = context_router.cache_mode(cfg, "skills")
        choice["invalid_config"] = False
    except ValueError:
        cache_mode, choice["invalid_config"] = "off", True
    choice["cache_mode"] = cache_mode
    if cache_mode == "active" and catalog:
        choice["section"] = "## skills\n" + catalog + "\n\n" + choice["section"].removeprefix("## skills\n")
    return choice


def _skills_section(choice):
    selected = list(choice["selected"])
    mandatory = set(choice.get("mandatory") or [])
    levels = {skill_id: 2 for skill_id in selected}
    dropped, demoted = [], []
    def l2_chars():
        return sum(len(skills_registry.render(skill_id, 2)) for skill_id in selected
                   if levels.get(skill_id) == 2)
    for skill_id in reversed(selected):
        if l2_chars() <= _SKILL_PRESENTATION_CAP:
            break
        if skill_id in mandatory:
            continue
        levels[skill_id] = 1
        demoted.append(skill_id)
    while l2_chars() > _SKILL_PRESENTATION_CAP:
        candidate = next((item for item in reversed(selected) if item not in mandatory), None)
        if candidate is None:
            break
        selected.remove(candidate); levels.pop(candidate, None); dropped.append(candidate)
    parts = []
    for skill_id in selected:
        version = _skill_records().get(skill_id, {}).get("version", "unknown")
        parts.append(f"### {skill_id} (v{version})\n{skills_registry.render(skill_id, levels[skill_id])}")
    ambiguous = [item for item in choice.get("ambiguous", []) if item not in selected]
    if ambiguous:
        parts.append("### available on request\n" + "\n".join(skills_registry.render(item, 0) for item in ambiguous))
    return {"section": "## skills\n" + "\n\n".join(parts), "presented": selected,
            "demoted": demoted, "dropped": dropped,
            "skill_tokens_presented_l2": sum(len(skills_registry.render(item, 2)) // 4
                                               for item in selected if levels[item] == 2)}


def _skill_routing(task, role, cfg, exposure, choice=None):
    if harness_depth.active(task):
        return {"skills_selected": [], "skill_routing_mode": "fast_path"}
    choice = choice or _prepare_skills(task, role, cfg)
    if choice is None:
        return {}
    presented = choice["presented"] if choice["mode"] == "active" else choice["selected"]
    decision_log.record("skill_selection", task["id"], role=role, candidates=choice["candidates"],
                        hard_constraints=choice["mandatory"],
                        deterministic={"triggers": choice["triggers"], "task_class": choice["task_class"],
                                       "mandatory": choice["mandatory"], "role": role,
                                       **{key: choice[key] for key in ("catalog_chars", "selected_chars",
                                                                      "cache_mode", "invalid_config") if key in choice}},
                        selected=presented, rejected=choice["rejected"],
                        reason=choice["reason"], mode=choice["mode"],
                        jev=choice.get("jev"),
                        extra={key: choice[key] for key in ("tokens_exposed_l0", "tokens_selected_l0",
                                                            "tokens_selected_l2", "ambiguous")}
                              | {"role": role, "demoted": choice["demoted"],
                                 "specialist": choice.get("specialist")})
    return {"skills_selected": presented,
            "skill_tokens_selected_l0": choice["tokens_selected_l0"],
            "skill_tokens_selected_l2": choice["tokens_selected_l2"],
            "skill_tokens_presented_l2": choice["skill_tokens_presented_l2"],
            "skill_routing_mode": choice["mode"]}


def _skills_from_gate(task_id, session_id=None):
    used = set()
    path = STATE / "runs" / "jev" / "gate.jsonl"
    records = _skill_records()
    if not path.exists():
        return [], 0
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if row.get("task") != task_id and not (session_id and row.get("session") == session_id):
            continue
        raw = row.get("tool_target") or ""
        for skill_id, record in records.items():
            role, name = skill_id.split("/", 1)
            base = f"skills/{role}/{name}/"
            if (row.get("tool") == "Read" and base + "SKILL.md" in raw) or \
                    (row.get("tool") == "Bash" and base + "scripts/" in raw):
                used.add(skill_id)
    ordered = sorted(used)
    return ordered, sum(int(records[item].get("est_tokens_l2") or 0) for item in ordered)


def _remember_packet_meta(version, meta):
    _PACKET_BUILD_META[version] = meta
    _PACKET_BUILD_META.move_to_end(version)
    while len(_PACKET_BUILD_META) > _PACKET_BUILD_META_MAX:
        _PACKET_BUILD_META.popitem(last=False)


def memory_recall(query, **kwargs):
    """Load the shared memory skill lazily so packet building has no optional service dependency."""
    global _MEMORY_RECALL
    if _MEMORY_RECALL is None:
        path = Path(__file__).resolve().parents[1] / ".claude/skills/memory/scripts/recall.py"
        spec = importlib.util.spec_from_file_location("orchestrator_memory_recall", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MEMORY_RECALL = module.recall
    kwargs.setdefault("root", STATE.parent)
    return _MEMORY_RECALL(query, **kwargs)

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


# Imported after ``git`` because context_router -> failures -> merge imports it.
from . import context_router


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


def render(name, *, task=None, signals=None, **kw):
    if name in ("review", "spec-review", "scout") and "packet" not in kw:
        raise ValueError(f"packet is required for {name}")
    if name == "scout":
        kw.setdefault("base_branch", "origin/main")
        kw.setdefault("base_sha", "(unavailable)")
    t = (STATE / "prompts" / f"{name}.md").read_text()
    legacy_template = t
    if task is not None and instructions.mode() in ("shadow", "active"):
        role = "security_review" if name == "review" and (signals or {}).get("security") else name.replace("-", "_")
        choice = instructions.select(task, role, signals=signals)
        base = legacy_template
        for module, _ in instructions.RULES:
            text = instructions.module_text(module)
            if text in base:
                base = base.replace(text, "", 1)
        modular_template = instructions.compose(base, choice["modules"])
        placeholder = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
        def fill(template):
            missing = next((m.group(1) for m in placeholder.finditer(template) if m.group(1) not in kw), None)
            if missing is not None:
                raise ValueError(f"unfilled_placeholder: {missing}")
            return placeholder.sub(lambda m: kw[m.group(1)] if isinstance(kw[m.group(1)], str)
                                   else json.dumps(kw[m.group(1)], indent=0), template)
        legacy, modular = fill(legacy_template), fill(modular_template)
        decision_log.record(
            kind="instruction_loading", subject=task.get("id", "(unknown)"),
            candidates=[module for module, _ in instructions.RULES], hard_constraints=choice["mandatory"],
            deterministic={"selected": choice["modules"], "reasons": choice["reasons"],
                           "legacy_tokens": len(legacy) // 4, "modular_tokens": len(modular) // 4,
                           "delta_tokens": len(modular) // 4 - len(legacy) // 4},
            selected=choice["modules"], reason="deterministic rules v1", mode=instructions.mode())
        result = modular if instructions.mode() == "active" else legacy
        _INSTRUCTION_RENDER_META[result] = {"instruction_tokens_modular": len(modular) // 4}
        while len(_INSTRUCTION_RENDER_META) > _PACKET_BUILD_META_MAX:
            _INSTRUCTION_RENDER_META.popitem(last=False)
        return _record_prefix_identity(result, task)
    placeholder = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
    missing = next((match.group(1) for match in placeholder.finditer(t) if match.group(1) not in kw), None)
    if missing is not None:
        raise ValueError(f"unfilled_placeholder: {missing}")
    result = placeholder.sub(
        lambda match: kw[match.group(1)] if isinstance(kw[match.group(1)], str)
        else json.dumps(kw[match.group(1)], indent=0),
        t,
    )
    return _record_prefix_identity(result, task)


def _record_prefix_identity(rendered, task=None):
    """Record the stable wrapper before the task-specific packet header."""
    header = re.search(r"(?m)^packet v([0-9a-f]+)\b", rendered)
    if not header:
        return rendered
    version = header.group(1)
    prefix, suffix = rendered[:header.start()], rendered[header.start():]
    values = {"prefix_sha": hashlib.sha256(prefix.encode()).hexdigest()[:12],
              "prefix_chars": len(prefix), "suffix_chars": len(suffix),
              "dynamic_sections": re.findall(r"(?m)^## ([^\n]+)", suffix)}
    _remember_packet_meta(version, {**_PACKET_BUILD_META.get(version, {}), **values})
    if task is not None and task.get("id"):
        try:
            current = bus.get(task["id"])
            meta = {**(current.get("packet_meta") or {}), **values}
            bus.update(task["id"], packet_meta=meta)
        except (KeyError, OSError, ValueError):
            pass
    return rendered


def hold_render_error(task_id, exc):
    with bus.locked():
        task = bus.get(task_id)
        pipeline = dict(task.get("pipeline") or {})
        pipeline["render_error"] = str(exc)[:300]
        pipeline.pop("dispatched_at", None)
        bus.update(task_id, status="held", hold_reason="render_error: " + str(exc)[:200],
                   assigned_to=None, pipeline=pipeline)
    notify.notify(f"{task_id}: render_error: {exc}")


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


def _context_mode(cfg):
    invalid_config = False
    try:
        cache_mode = context_router.cache_mode(cfg)
    except ValueError:
        cache_mode, invalid_config = "off", True
    mode = promotion.mode("context_router", cfg)
    if mode != "active":
        return mode, cache_mode, invalid_config
    try:
        from datetime import datetime, timezone
        report = json.loads((STATE / "context_eval.json").read_text())
        if not isinstance(report, dict):
            raise ValueError("invalid context evaluation")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(report["ran_at"])).total_seconds()
        if report.get("suite_passed") is True and 0 <= age <= 7 * 86400:
            return "active", cache_mode, invalid_config
    except (OSError, ValueError, KeyError, TypeError):
        pass
    notify.notify("context_router active refused; running shadow: context_eval missing, stale, or failed")
    return "shadow", cache_mode, invalid_config

def _shadow_route(task, candidates, *, role, head_sha, cfg, skills=None, provider=None):
    """Persist routing telemetry and return section items for guarded active mode."""
    mode, cache_mode, invalid_config = _context_mode(cfg)
    if mode == "off" or not task.get("id"):
        return {}
    try:
        pool = evidence.EvidencePool(task.get("parent") or task["id"])
        candidates = [pool.add(candidate) for candidate in candidates]
        # Input occurrences describe the packet's uncompressed baseline. Stable
        # storage identities must not erase occurrences from routing telemetry.
        seen = {item.id for item in candidates}
        for item in pool.by_type("worker_partial"):
            if item.id not in seen:
                candidates.append(item)
                seen.add(item.id)
        composition = (skills or {}).get("_specialist")
        if composition:
            for item in composition.shared_evidence(task):
                if item.id not in seen:
                    candidates.append(item)
                    seen.add(item.id)
            candidates = composition.filter_evidence(candidates)
        routed = context_router.route(task, candidates, role=role, head_sha=head_sha, cfg=cfg,
                                      required_types=composition.context_requirements if composition else (),
                                      provider=provider, effective_mode=mode,
                                      cache_mode=cache_mode, invalid_config=invalid_config)
        row = context_router.decision_row(task, routed, mode=mode)
        partial_items = context_router.section_items(
            routed, {ev.id: ev for ev in candidates}, "prior_worker")
        row["extra"] = {"head_sha": head_sha,
                        "prior_worker_ids": [ev.id for ev in candidates
                                             if ev.source_type == "worker_partial" and
                                             any(item.evidence_id == ev.id and item.level != "HIDE" for item in routed.items)],
                        "prior_worker_chars": sum(len(text) for _, text in partial_items)}
        decision_log.record(**row)
        return {
            "routed_mode": mode,
            "_routed_sections": {section: context_router.section_items(
                routed, {ev.id: ev for ev in candidates}, section)
                for section in ("gotchas", "decisions", "evidence", "prior_worker", "read_scope", "routed-findings")}
                if mode == "active" else {},
            "routed_tokens": routed.routed_tokens,
            "routed_reduction_ratio": routed.reduction_ratio,
            "routed_hidden": sum(item.level == "HIDE" for item in routed.items),
            "routed_ambiguous": len(routed.ambiguous_ids),
            "routed_rules_version": routed.rules_version,
            "evidence_ids": [item.evidence_id for item in routed.items][:200],
        }
    except Exception as exc:
        notify.notify(f"{task.get('id', '(unknown)')}: context_router shadow failed: {exc}")
        return {}


def _shadow_tool_disclosure(task, role, cfg, skills=None):
    """Record disclosure choice and return its telemetry and selected allowlist."""
    if role == "codex_execute":
        return {"tool_allowlist_source": "codex"}
    mode = promotion.mode("tool_disclosure", cfg)
    if mode not in ("shadow", "active"):
        return {"tool_allowlist_source": "legacy"}
    offered = tool_catalog.disclosed(role)
    choice = tool_catalog.minimal_set(task, role)
    specialist_choice = (skills or {}).get("specialist") or {}
    hand_over = (mode == "active" and (skills or {}).get("mode") == "active"
                 and promotion.mode("skill_routing", cfg) == "active")
    selected = specialist_choice.get("tools", choice["keep"]) if hand_over else choice["keep"]
    source = "specialist" if hand_over else "minimal" if mode == "active" else "legacy"
    disclosed_tokens = tool_catalog.tokens(offered)
    minimal_tokens = tool_catalog.tokens(choice["keep"])
    extra = {"tool_allowlist_source": source}
    reason = choice["reason"]
    if hand_over:
        reason = "specialist hand-over"
        extra.update({"tools_added": specialist_choice.get("tools_added", []),
                      "skills": list(skills.get("selected") or [])})
    decision_log.record(
        kind="tool_disclosure", subject=task.get("id", "(unknown)"), candidates=offered,
        hard_constraints=choice["mandatory"],
        selected=selected if mode == "active" else "allowlist unchanged (shadow)",
        deterministic={"task_class": tool_catalog._task_class(task), "role": role,
                       "kept": selected, "dropped": [tool for tool in offered if tool not in selected],
                       "tokens_disclosed": disclosed_tokens, "tokens_minimal": minimal_tokens,
                       **tool_catalog.cache_fields(task, role, selected, cfg)},
        reason=reason, mode=mode, extra=extra)
    return {"tool_tokens_disclosed": disclosed_tokens, "tool_tokens_minimal": minimal_tokens,
            "tool_allowlist": ",".join(selected) if mode == "active" else TOOLS.get(role, TOOLS["scout"]),
            "tool_disclosure_mode": mode, "tool_allowlist_source": source}


def _hidden_tool_request(task_id):
    result = bus.get(task_id).get("result") or {}
    reason = result.get("reason", "") if isinstance(result, dict) else ""
    return reason[len(NEEDS_TOOL_PREFIX):].strip() if reason.startswith(NEEDS_TOOL_PREFIX) else None


def _combined_usage(first, second):
    outputs = [r.get("output", {}) for r in (first, second) if isinstance(r, dict)]
    usage = {}
    for output in outputs:
        for key, value in (output.get("usage") or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    return {"usage": usage, "total_cost_usd": sum(float(o.get("total_cost_usd", 0) or 0) for o in outputs)}



def _trim_routed_item(by_name, routed_sections):
    """Drop one atomic item, lowest level first, retaining complete fences."""
    for level in ("SHORT", "LONG", "FULL"):
        for name, items in routed_sections.items():
            for index in range(len(items) - 1, -1, -1):
                if items[index][0] == level:
                    items.pop(index)
                    if name in by_name:
                        by_name[name].pop(index)
                    return True
    return False


def _tiered_memory(task, cfg):
    """Read the existing HOT projection; packet builders never refresh it."""
    fresh = memory_hot.fresh(ROOT)
    hot_path = ROOT / ".orchestrator" / "memory" / "HOT.md"
    hot_lines = hot_path.read_text().splitlines() if hot_path.exists() else []
    indexed = {memory_hot._line(record): record for record in memory_store.all_records(ROOT)}
    candidates = []
    records = {}
    for line in hot_lines:
        record = indexed.get(line)
        if (record and record["id"] not in records
                and record["kind"] in ("gotcha", "decision", "architecture")):
            records[record["id"]] = record
            candidates.append({"id": record["id"], "tier": "hot", "score": 1.0})
    # Quote individual words for FTS syntax; punctuation in titles is data.
    words = re.findall(r"\w+", " ".join([task.get("title", ""),
                       *(Path(path).stem for path in task.get("scope", []))]))
    query = " OR ".join('"' + word + '"' for word in dict.fromkeys(words))
    for kind in ("gotcha", "decision", "architecture"):
        result = memory_store.search(query, kind=kind, tier="warm", limit=8, root=ROOT)
        for rank, record in enumerate(result["records"], 1):
            if record["id"] not in records:
                records[record["id"]] = record
                candidates.append({"id": record["id"], "tier": "warm", "score": 1.0 / rank})
    sections = {"gotchas": [], "decisions": []}
    selected = {}
    remaining = max(0, int(cfg.get("memory", {}).get("packet_hot_tokens", 1200))) * 4
    for record in records.values():
        line = " ".join(memory_hot._line(record).splitlines())
        cost = len(line) + 1
        if cost > remaining:
            continue
        remaining -= cost
        section = "gotchas" if record["kind"] == "gotcha" else "decisions"
        sections[section].append(line)
        selected[record["id"]] = (section, line)
    return sections, {"candidates": candidates, "selected_lines": selected, "hot_fresh": fresh}


def _memory_tokens(sections):
    return (sum(len(line) + 1 for name in ("gotchas", "decisions")
                for line in sections.get(name, []) if line != "- (none)") + 3) // 4


def _packet_body(task, worktree, *, cfg=None, skills=None, provider=None) -> tuple[str, dict]:
    """Build the executor's bounded, deterministic briefing solely from task/repository data."""
    from .steering_policy import read_scope as derive_read_scope, safe_scope
    wt = Path(worktree)
    cfg = Pool().cfg if cfg is None else cfg
    scope = safe_scope(task, worktree)
    scope_files = [wt / p for p in scope if (wt / p).is_file()]
    py_files = [p for p in scope_files if p.suffix == ".py"]
    symbols, symbol_names = [], set()
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

    read_scope = set(derive_read_scope(task, worktree))
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
    memory = memory_recall(" ".join([task.get("title", ""), *scope]), layers=("notes", "bus"),
                           budget_hits=5, task=task.get("id"))
    gotchas = [f"- {hit['id']} {hit['title']}" for hit in memory["hits"]
               if hit["id"].startswith("mem:gotchas.md:")]
    matched_gotchas = list(gotchas)
    decisions = []
    legacy_ids = [hit["id"] for hit in memory["hits"]
                  if hit["id"].startswith("mem:gotchas.md:")]
    for candidate in (wt / ".orchestrator/memory/decisions.md", wt / "decisions.md"):
        entries = [(line, title) for line, title, body in _memory_entries(candidate)
                   if parent != "(none)" and parent.lower() in f"{title}\n{body}".lower()][:3]
        decisions = [f"- {title}" for _, title in entries]
        legacy_ids.extend(f"mem:{candidate.name}:{line}" for line, _ in entries)
        if candidate.exists():
            break
    memory_mode = promotion.mode("memory_tiers", cfg)
    legacy_sections = {"gotchas": gotchas[:5], "decisions": decisions}
    legacy_lines = dict(zip(legacy_ids, [(name, line) for name, lines in legacy_sections.items()
                                       for line in lines]))
    tiered_sections, retrieval = None, None
    if memory_mode != "off":
        try:
            tiered_sections, retrieval = _tiered_memory(task, cfg)
        except Exception:
            logging.getLogger(__name__).warning("packet memory retrieval failed")
        if memory_mode == "active" and tiered_sections is not None:
            gotchas, decisions = tiered_sections["gotchas"], tiered_sections["decisions"]
    # 2026-09-23: bound legacy evidence while old, oversized pools age out.
    legacy_evidence_chars = max(0, int((cfg.get("context_router") or {}).get(
        "legacy_evidence_chars", 6000)))
    input_evidence_lines = []
    for item in task.get("inputs", []):
        value = item
        if isinstance(item, str):
            try:
                value = bus.get(item).get("result") or {}
            except KeyError:
                value = {}
        summary = value.get("summary", value) if isinstance(value, dict) else value
        input_evidence_lines.append(
            f"- {item if isinstance(item, str) else 'input'}: {str(summary)[:200]}")

    def capped_lines(lines, limit):
        kept, used = [], 0
        for line in lines:
            available = limit - used
            if available <= 0:
                break
            value = str(line)[:available]
            kept.append(value)
            used += len(value) + 1
        return kept

    evidence_lines = capped_lines(input_evidence_lines, legacy_evidence_chars)

    composition = (skills or {}).get("_specialist")
    if composition and skills.get("mode") == "active":
        shared_lines = capped_lines(
            (f"- {item.id} {item.location}: {item.summary_short}"
             for item in composition.shared_evidence(task)), legacy_evidence_chars)
        evidence_lines = capped_lines([*evidence_lines, *shared_lines], legacy_evidence_chars)
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
        ("gotchas", gotchas or ["- (none)"]),
        ("decisions", decisions or ["- (none)"]),
        ("verify", ["- .claude/hooks/tests-green.sh .", "- On failure, report only scripts/failures_only.sh output."]),
        ("evidence", evidence_lines or capped_lines(["- (none)"], legacy_evidence_chars)),
    ]
    if skills and skills.get("section"):
        sections.insert(0, ("skills", skills["section"].removeprefix("## skills\n").splitlines()))
    try:
        tool_cache_mode = context_router.cache_mode(cfg, "tool_disclosure")
    except ValueError:
        tool_cache_mode = "off"
    if tool_cache_mode == "active" and promotion.mode("tool_disclosure", cfg) != "off":
        tool_role = "codex_execute" if provider == "codex" else task.get("role", "execute")
        kept = tool_catalog.minimal_set(task, tool_role)["keep"]
        if (skills or {}).get("mode") == "active" and promotion.mode("tool_disclosure", cfg) == "active":
            kept = ((skills or {}).get("specialist") or {}).get("tools", kept)
        sections.insert(0, ("tools", [tool_catalog.level0(tool_catalog.disclosed(tool_role)),
                                      tool_catalog.level2(kept)]))
    candidates = []
    task_id = task.get("id", "(none)")
    if task.get("spec"):
        candidates.append(evidence.make(
            "architecture_note", f"task:{task_id}:spec", task["spec"], provenance="repo", task=task))
    candidates.extend(evidence.make(
        "architecture_note", f"task:{task_id}:acceptance:{index}", value,
        provenance="repo", task=task)
        for index, value in enumerate(task.get("acceptance", []), 1))
    for path in scope_files:
        candidates.append(evidence.make(
            "source_chunk", str(path.relative_to(wt)), path.read_text(errors="replace")[:12000],
            commit=merge_base, provenance="repo", task=task))
    for path in sorted(read_scope - set(scope)):
        path_symbols = "\n".join(line for line in symbols if line.startswith(f"- {path}"))
        candidates.append(evidence.make(
            "source_chunk", path or ".", path_symbols, commit=merge_base, provenance="repo", task=task, section="read_scope"))
    candidates.extend(evidence.make(
        "memory_entry", f"task:{task_id}:gotcha:{index}", value,
        provenance="memory", task=task, section="gotchas") for index, value in enumerate(gotchas, 1))
    candidates.extend(evidence.make(
        "decision", f"task:{task_id}:decision:{index}", value,
        provenance="memory", task=task, section="decisions") for index, value in enumerate(decisions, 1))
    for previous_result in capped_lines(input_evidence_lines, legacy_evidence_chars):
        content_hash = hashlib.sha256(previous_result.encode()).hexdigest()
        candidates.append(evidence.make(
            "previous_result", f"task:{task_id}:evidence:{content_hash}", previous_result,
            provenance="bus", task=task, section="evidence"))
    candidates.extend(evidence.make(
        "test_result", value, value, commit=merge_base, provenance="repo", task=task)
        for value in tests)
    failure_match = re.search(r"(?ms)^Failures:\s*(.*)$", str(task.get("spec") or ""))
    if (task.get("constraints") or {}).get("fix_round_for") and failure_match:
        candidates.append(evidence.make(
            "test_result", f"task:{task_id}:failures", failure_match.group(1),
            commit=merge_base, provenance="repo", task=task))
    routing_task = {**task, "packet_read_scope": sorted(read_scope)}
    shadow_meta = _shadow_route(routing_task, candidates, role="execute", head_sha=merge_base, cfg=cfg,
                                skills=skills, provider=provider)
    routed_sections = shadow_meta.pop("_routed_sections", {})
    if "evidence" in routed_sections:
        # Keep routed items whole (including fences) under the same cap.
        items = routed_sections["evidence"]
        size = sum(len(text) + 1 for _, text in items)
        while items and size > legacy_evidence_chars:
            _, text = items.pop()
            size -= len(text) + 1
    if memory_mode == "active" and retrieval is not None:
        routed_sections = {name: items for name, items in routed_sections.items()
                           if name not in tiered_sections}
    sections = [(name, [text for _, text in routed_sections[name]] if name in routed_sections else lines)
                for name, lines in sections]
    if "prior_worker" in routed_sections and routed_sections["prior_worker"]:
        evidence_index = next(i for i, (name, _) in enumerate(sections) if name == "evidence")
        sections.insert(evidence_index + 1,
                        ("prior_worker", [text for _, text in routed_sections["prior_worker"]]))
    dependencies = []
    for dependency_id in task.get("depends_on", []):
        try:
            dependency = bus.get(dependency_id)
        except KeyError:
            dependencies.append(f"- {dependency_id}: (unavailable)")
            continue
        sha = dependency.get("sha") or dependency.get("merged_sha")
        dependencies.append(
            f"- {dependency_id}: {dependency.get('title', '')[:90]}"
            f"; status: {dependency.get('status')}; merged_into: {dependency.get('merged_into')}"
            + (f"; merged sha: {sha}" if sha else ""))
    if dependencies:
        sections.append(("dependencies", dependencies))
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
    header_chars = len(f"packet v{'0' * 12} base {merge_base[:12]} sources "
                       f"pool.toml@{policy_version} gotchas@{gotchas_sha} memory@{','.join(memory['layers_consulted'])}"
                       " routed=active") + 1
    def build(values):
        return "\n".join(f"## {name}\n" + "\n".join(lines) for name, lines in values
                         if name != "dependencies" or lines)
    candidate_tokens = len(build(sections)) // 4
    # Memory remains in the existing body trim order; acceptance stays whole.
    trimmable = ("dependencies", "evidence", "decisions", "gotchas", "symbols", "relevant_tests")
    def trim(values, routed):
        by_name = dict(values)
        while len(build(values)) >= (4800 - header_chars if routed else 4800):
            if _trim_routed_item(by_name, routed):
                continue
            for name in trimmable:
                lines = [] if name in routed else by_name.get(name, [])
                if lines:
                    lines.pop()
                    break
            else:
                break
        return by_name

    hot_fresh = memory_hot.fresh(ROOT)
    if retrieval is not None:
        import copy
        comparisons = []
        for alternative in (legacy_sections, tiered_sections):
            values = [(name, list(alternative[name]) if name in alternative else list(lines))
                      for name, lines in sections]
            routed = copy.deepcopy({name: items for name, items in routed_sections.items()
                                    if name not in alternative})
            comparisons.append(trim(values, routed))
        legacy_view, tiered_view = comparisons
        presented = trim(sections, routed_sections)
        if memory_mode == "active":
            tiered_view = presented
        else:
            legacy_view = presented
        legacy_ids = [record_id for record_id, (section, line) in legacy_lines.items()
                      if line in legacy_view[section]]
        selected = [record_id for record_id, (section, line) in retrieval["selected_lines"].items()
                    if line in tiered_view[section]]
        hot_fresh = retrieval["hot_fresh"]
        try:
            decision_log.record(
                kind="retrieval", subject=task.get("id", "(none)"), candidates=retrieval["candidates"],
                hard_constraints={"packet_hot_tokens": cfg.get("memory", {}).get("packet_hot_tokens", 1200)},
                deterministic={"query_source": "title and scope basenames"}, selected=selected,
                mode=memory_mode, reason="HOT plus WARM packet comparison",
                extra={"legacy_ids": legacy_ids, "tokens_legacy": _memory_tokens(legacy_view),
                       "tokens_tiered": _memory_tokens(tiered_view), "hot_fresh": hot_fresh})
        except Exception:
            logging.getLogger(__name__).warning("packet memory retrieval logging failed")
    if retrieval is None:
        presented = trim(sections, routed_sections)
    memory_ids = [record_id for record_id, (section, line) in legacy_lines.items()
                  if line in presented[section]]
    if memory_mode == "active" and retrieval is not None:
        memory_ids = [record_id for record_id, (section, line) in retrieval["selected_lines"].items()
                      if line in presented[section]]
    body = build(sections)
    return body, {"hash": hashlib.sha256(body.encode()).hexdigest()[:12], "base": merge_base[:12],
                  "policy_version": str(policy_version), "gotchas": gotchas_sha,
                  "memory_layers": ",".join(memory["layers_consulted"]),
                  "memory_mode": memory_mode, "memory_ids": memory_ids, "hot_fresh": hot_fresh,
                  "candidate_tokens": candidate_tokens, "candidate_known": True,
                  "skill_tokens_presented_l2": (skills or {}).get("skill_tokens_presented_l2", 0),
                  **shadow_meta}


def packet_meta(task, worktree) -> dict:
    """Provenance values for the packet and its corresponding run record."""
    return packet_run_meta(packet(task, worktree))


def packet_run_meta(text) -> dict:
    """Measure the exact packet sent, using its H8 header as the version identity."""
    header = re.match(r"packet v([0-9a-f]+) base (\S+) sources (.+)", text)
    meta = {"chars": len(text), "est_tokens": len(text) // 4, "presented_tokens": len(text) // 4,
            "sections": section_meta(text), "hash": None, "version": None,
            "candidate_known": False}
    if header:
        version, base, sources = header.groups()
        meta.update(hash=version, version=version, base=base, sources=sources)
        legacy = re.match(r"pool.toml@(\S+) gotchas@(\S+)", sources)
        if legacy:
            meta.update(policy_version=legacy.group(1), gotchas=legacy.group(2))
        meta.update(_PACKET_BUILD_META.get(version, {}))
    return meta


def section_meta(text) -> dict:
    """Attribute every byte of a packet to its preamble or a named ``##`` section."""
    matches = list(re.finditer(r"(?m)^## ([^\n]+)\n?", text))
    spans = [("_preamble", 0, matches[0].start() if matches else len(text))]
    spans.extend((match.group(1), match.start(), matches[i + 1].start() if i + 1 < len(matches) else len(text))
                 for i, match in enumerate(matches))
    out = {}
    for name, start, end in spans:
        value = text[start:end]
        out[name] = {"chars": len(value), "est_tokens": len(value) // 4,
                     "sha256": hashlib.sha256(value.encode()).hexdigest()}
    return out


def with_instruction_tokens(meta, rendered_prompt, packet):
    """Return packet metadata augmented with deterministic prompt-wrapper cost."""
    rendered = next((text for text in reversed(_INSTRUCTION_RENDER_META) if rendered_prompt.startswith(text)), None)
    extra = _INSTRUCTION_RENDER_META.get(rendered, {})
    if extra:
        extra = {"instruction_tokens_modular": extra["instruction_tokens_modular"] - len(packet) // 4}
    return {**meta, "instruction_tokens": len(rendered_prompt) // 4 - len(packet) // 4, **extra}


def packet(task, worktree, *, cfg=None, skills=None, provider="codex") -> str:
    """Build the Codex dispatch briefing; Claude fallback supplies its provider explicitly."""
    body, meta = _packet_body(task, worktree, cfg=cfg, skills=skills, provider=provider)
    header = (f"packet v{meta['hash']} base {meta['base']} sources "
              f"pool.toml@{meta['policy_version']} gotchas@{meta['gotchas']} memory@{meta['memory_layers']}")
    if skills and skills.get("mode") == "active" and skills.get("specialist"):
        header += f" specialist: {skills['specialist']['name']}"
    marker = " routed=" + ("active" if meta.get("routed_mode") == "active" else "shadow")
    # Account for the header itself, including a possible extra digit in n.
    over = len(header) + len(marker) + 1 + len(body) - 4800
    if over > 0:
        while True:
            extended = f"{header} over cap by {over} chars: acceptance kept whole"
            actual = len(extended) + len(marker) + 1 + len(body) - 4800
            if actual == over:
                header = extended
                break
            over = actual
    result = header + marker + "\n" + body
    _remember_packet_meta(meta["hash"],
                          {key: value for key, value in meta.items()
                           if key in ("candidate_tokens", "candidate_known", "routed_tokens",
                                      "routed_reduction_ratio", "routed_hidden", "routed_ambiguous",
                                      "routed_rules_version", "routed_mode", "evidence_ids",
                                      "skill_tokens_presented_l2")})
    return result


def _role_packet(body, base, sources, **build_meta):
    version = hashlib.sha256(body.encode()).hexdigest()[:12]
    _remember_packet_meta(version, build_meta or {"candidate_known": False})
    return f"packet v{version} base {base or '(unavailable)'} sources {sources}\n{body}"


def _section(name, value):
    if isinstance(value, (list, tuple)):
        value = "\n".join(f"- {item}" for item in value) or "- (none)"
    return f"## {name}\n{value if value not in (None, '') else '(none)'}"


def _base_sha(task, worktree=None):
    wt = Path(worktree or task.get("worktree") or ROOT)
    return git("rev-parse", "HEAD", cwd=wt, check=False).stdout.strip()[:12] or "(unavailable)"


def _acceptance_test_ids(acceptance):
    return sorted(set(re.findall(r"(?:tests/[^\s,:'\"]+\.py::)?test_[A-Za-z0-9_]+",
                                 "\n".join(map(str, acceptance)))))


def review_packet(task, reviewed, *, cfg=None, skills=None, provider="claude") -> str:
    from .daemon import SECURITY_CHECKLIST_COMPLEXITY

    src = reviewed or task
    wt = Path(src.get("worktree") or ROOT)
    raw_diff = scoped_diff(src)
    hint = f"git -C {wt} diff -- {' '.join(src.get('scope', []))}"
    changed = sorted(set(re.findall(r"^[+\-]{3} [ab]/(tests/\S+)", raw_diff, re.M)))
    tests = [f"{path}: present" for path in changed]
    for test_id in _acceptance_test_ids(src.get("acceptance", [])):
        path = test_id.split("::", 1)[0] if "::" in test_id else None
        if path:
            present = (wt / path).is_file()
        else:
            present = any(re.search(rf"\bdef\s+{re.escape(test_id)}\b", candidate.read_text(errors="replace"))
                          for candidate in sorted((wt / "tests").glob("test_*.py"))) if (wt / "tests").is_dir() else False
        tests.append(f"{test_id}: {'present' if present else 'missing'}")
    pipeline = src.get("pipeline") or {}
    gate_lines = [f"gated_at: {pipeline.get('gated_at')}", f"gate_attempts: {pipeline.get('gate_attempts')}",
                  f"gate_reds: {pipeline.get('gate_reds')}", f"first_green_at: {pipeline.get('first_green_at')}"]
    if pipeline.get("gate_reds"):
        failure = pipeline.get("last_failure_text") or pipeline.get("gate_failure") or src.get("reason") or "(unavailable)"
        gate_lines.append("last_failure_head: " + str(failure).splitlines()[0][:500])
    sections = ([_section("skills", skills["section"].removeprefix("## skills\n"))]
                if skills and skills.get("section") else []) + [_section("spec", src.get("spec")), _section("acceptance", src.get("acceptance", [])),
                _section("scope", src.get("scope", [])), None,
                _section("changed tests", tests or ["(none)"]), _section("gate", gate_lines)]
    reviewer_role = (task.get("constraints") or {}).get("reviewer_role")
    role_focus = {
        "acceptance": ("Focus: every acceptance criterion met by the diff, functional correctness, regressions "
                       "in touched code paths, tests present and meaningful for each acceptance id; leave security "
                       "and adversarial edge cases to the second reviewer unless blocking"),
        "adversarial": ("Focus: adversarial reasoning about the diff, security and privilege boundaries, edge cases "
                        "and failure modes, hidden assumptions and untested paths; leave acceptance bookkeeping to "
                        "the first reviewer unless blocking"),
    }
    if reviewer_role in role_focus:
        sections.append(_section("role", role_focus[reviewer_role]))
    cfg = Pool().cfg if cfg is None else cfg
    security_globs = cfg.get("review", {}).get("security_paths", [])
    matched = sorted({glob for glob in security_globs for path in src.get("scope", []) if Path(path).match(glob)})
    semantic = re.search(r"\b(auth|credential|secret|token|permission|crypt|security)\b",
                         f"{src.get('spec', '')}\n{raw_diff}", re.I)
    review_reason = (src.get("pipeline") or {}).get("review_reason")
    reasons = []
    if not security_globs:
        reasons.append("security_paths_empty")
    if review_reason in ("security_paths", "diff_unavailable", "security_paths_empty"):
        reasons.append(review_reason)
    if matched or semantic or task.get("complexity", 0) >= SECURITY_CHECKLIST_COMPLEXITY or reasons:
        triggers = [*matched]
        if semantic:
            triggers.append("semantic security trigger")
        if task.get("complexity", 0) >= SECURITY_CHECKLIST_COMPLEXITY:
            triggers.append(f"complexity >= {SECURITY_CHECKLIST_COMPLEXITY}")
        triggers.extend(f"review_reason: {reason}" for reason in dict.fromkeys(reasons))
        sections.append(_section("security", [*triggers,
                        "checklist: skills/review/adversarial-review/references/security-checklist.md"]))
    fix_for = (src.get("constraints") or {}).get("fix_round_for")
    if fix_for:
        comments = []
        for candidate_id in [*src.get("inputs", []), fix_for]:
            if not isinstance(candidate_id, str):
                continue
            try:
                candidate = bus.get(candidate_id)
            except KeyError:
                continue
            if candidate.get("role") == "review" or candidate_id == fix_for:
                comments = (candidate.get("result") or {}).get("comments", [])
                if comments:
                    break
        sections.append(_section("fix-round context", [json.dumps(x, sort_keys=True) for x in comments] or ["(none)"]))
    else:
        comments = []
    composition = (skills or {}).get("_specialist")
    if composition and skills.get("mode") == "active":
        shared = composition.shared_evidence(task)
        sections.append(_section("evidence", [f"{item.id} {item.location}: {item.summary_short}"
                                              for item in shared]))
    other_chars = len("\n".join(section for section in sections if section is not None)) + 1
    diff_heading_chars = len("## diff\n")
    configured_cap = cfg.get("limits", {}).get("review_diff_chars", 12000)
    diff_budget = max(1, min(configured_cap, 8000 - other_chars - diff_heading_chars))
    sections[sections.index(None)] = _section("diff", bounded_diff(raw_diff, diff_budget, hint))
    body = "\n".join(sections)
    role_source = f" reviewer-role@{reviewer_role}" if reviewer_role in role_focus else ""
    candidates = []
    if src.get("spec"):
        candidates.append(evidence.make("architecture_note", f"task:{src['id']}:spec", src["spec"],
                                        provenance="repo", task=src))
    candidates.extend(evidence.make("architecture_note", f"task:{src['id']}:acceptance:{index}", value,
                                    provenance="repo", task=src)
                      for index, value in enumerate(src.get("acceptance", []), 1))
    for chunk in raw_diff.split("diff --git ")[1:]:
        header = chunk.splitlines()[0] if chunk else ""
        match = re.match(r"a/(\S+) b/(\S+)", header)
        location = match.group(2) if match else header
        candidates.append(evidence.make("source_chunk", location, "diff --git " + chunk,
                                        commit=_base_sha(src, wt), provenance="repo", task=src, section="diff"))
    candidates.extend(evidence.make("review_finding", f"task:{src['id']}:review:{index}",
                                    json.dumps(value, sort_keys=True), provenance="bus", task=src, section="routed-findings")
                      for index, value in enumerate(comments, 1))
    for value in [src.get("result"), *src.get("inputs", [])]:
        if isinstance(value, str):
            try:
                value = bus.get(value).get("result")
            except KeyError:
                continue
        if isinstance(value, dict) and value.get("summary"):
            candidates.append(evidence.make("previous_result", f"task:{src['id']}:result:{len(candidates)}",
                                            value["summary"], provenance="bus", task=src,
                                            section="routed-findings"))
    shadow_meta = _shadow_route({**src, "id": task["id"], "parent": task.get("parent") or src.get("parent")}, candidates,
                                role="security_review" if any(section.startswith("## security\n")
                                                               for section in sections) else "review",
                                head_sha=_base_sha(src, wt), cfg=cfg, skills=skills, provider=provider)
    if skills and skills.get("mode") == "active" and skills.get("specialist"):
        role_source += f" specialist: {skills['specialist']['name']}"
    routed_sections = shadow_meta.pop("_routed_sections", {})
    if shadow_meta.get("routed_mode") == "active":
        # Findings move out of the legacy fix context; diff/security bytes stay intact.
        sections = ["## fix-round context\n" if section.startswith("## fix-round context\n") else section
                    for section in sections]
        sections.append("## routed-findings\n" + "\n".join(
            text for _, text in routed_sections.get("routed-findings", [])))
        body = "\n".join(sections)
    role_source += " routed=" + ("active" if shadow_meta.get("routed_mode") == "active" else "shadow")
    return _role_packet(body, _base_sha(src, wt),
                        f"task@{src.get('id', '(none)')} scoped-diff@HEAD{role_source}",
                        candidate_tokens=len(raw_diff) // 4, candidate_known=True,
                        skill_tokens_presented_l2=(skills or {}).get("skill_tokens_presented_l2", 0),
                        **shadow_meta)


def spec_review_packet(task) -> str:
    wt = Path(task.get("worktree") or ROOT)
    dependencies = []
    for task_id in task.get("depends_on", []):
        try:
            dependencies.append(f"{task_id}: {bus.get(task_id).get('title', '')}")
        except KeyError:
            dependencies.append(f"{task_id}: (missing)")
    tests = []
    for scope_path in task.get("scope", []):
        candidate = Path("tests") / f"test_{Path(scope_path).stem}.py"
        tests.append(f"{scope_path} -> {candidate}: {'present' if (wt / candidate).is_file() else 'missing'}")
    body = "\n".join([_section("spec", task.get("spec")), _section("acceptance", task.get("acceptance", [])),
                       _section("scope", task.get("scope", [])), _section("depends_on", dependencies),
                       _section("existing tests", tests), _section("complexity", task.get("complexity")),
                       _section("tier", task.get("tier"))])
    return _role_packet(body, _base_sha(task, wt), f"task@{task.get('id', '(none)')} tree@HEAD")


def scout_packet(task) -> str:
    wt = Path(task.get("worktree") or ROOT)
    tree = []
    for item in task.get("scope", []):
        tree.append(item)
        path = wt / item
        directory = path if path.is_dir() else path.parent
        if directory.is_dir():
            tree.extend(str(child.relative_to(wt)) for child in sorted(directory.iterdir()))
    tree = list(dict.fromkeys(tree))[:60]
    memory = memory_recall(" ".join([task.get("title", ""), *map(str, task.get("scope", []))]),
                           layers=("notes", "bus"), budget_hits=5, task=task.get("id"))
    titles = [hit["title"] for hit in memory["hits"]]
    body = "\n".join([_section("question", task.get("spec")),
                       _section("expected output", task.get("acceptance", [])),
                       _section("scope tree", tree), _section("memory titles", titles),
                       _section("result contract", ["bus_post_result fields: findings, open_questions, suggested_next, blocked",
                                                    "result limit: 1,500 tokens"])])
    return _role_packet(body, _base_sha(task, wt),
                        f"task@{task.get('id', '(none)')} tree@HEAD memory@{','.join(memory['layers_consulted'])}")


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


def run_claude(pool, acct, task, prompt, model, tools, max_budget_usd, timeout, *, resume_session=None):
    log_run = (lambda **fields: None) if resume_session else bus.log_run
    epoch = task.get("_launch_epoch", worker_control.launch_epoch(task["id"]))
    wt = Path(task.get("worktree") or ensure_worktree(task["id"]))
    trust_workspace(acct.config_dir, wt)
    # Explicit --mcp-config + --strict-mcp-config means workers never auto-load the project/user configs
    # (github, orchestrator MCP schemas cost tokens every turn a worker never needs them). Role-specific
    # override (.mcp.<role>.json) wins when present; every other role gets the bus-only worker config.
    role_cfg = ROOT / f".mcp.{task['role']}.json"
    mcp_config = role_cfg if role_cfg.exists() else ROOT / ".mcp.worker.json"
    extra = {"CLAUDE_CONFIG_DIR": os.path.expanduser(acct.config_dir), "ORCH_TASK_ID": task["id"],
           "ORCH_ROOT": str(ROOT), **secrets_for_role(task["role"])}
    # Headless hosts: `claude setup-token` issues a long-lived CLAUDE_CODE_OAUTH_TOKEN per CLAUDE_CONFIG_DIR,
    # set in this process's environment under the name pool.toml's oauth_token_env points at. Never logged.
    if acct.oauth_token_env and os.environ.get(acct.oauth_token_env):
        extra["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ[acct.oauth_token_env]
    env, _ = env_policy.worker_env(task["role"], base=os.environ, extra=extra,
                                   cfg=pool.cfg, task_id=task["id"])
    # claude 2.1.273 has no turn-cap flag; --max-budget-usd + subprocess timeout are the hard stops (§6.5)
    # Full access by user decision (2026-09-16): permissions bypassed; guardrails.sh + scope-guard.sh hooks are the floor.
    # Read-only roles still cannot edit: --disallowedTools is enforced even in bypass mode.
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json", "--max-budget-usd", str(max_budget_usd),
           "--dangerously-skip-permissions", "--allowedTools", tools,
           "--strict-mcp-config", "--mcp-config", str(mcp_config)]
    if resume_session:
        cmd += ["--resume", resume_session, "--tools", ""]
        cmd[cmd.index("--mcp-config") + 1] = '{"mcpServers": {}}'
    elif task.get("_resume_session"):
        cmd += ["--resume", task["_resume_session"]]
    if (task.get("packet_meta") or {}).get("skill_routing_mode") == "active":
        cmd.append("--disable-slash-commands")
    if task["role"] != "execute":
        cmd += ["--disallowedTools", "Edit,Write,NotebookEdit"]
    log = {"executor": task.get("executor") or f"claude:{task['tier']}", "complexity": task["complexity"]}
    if task["role"] == "execute":
        log["prompt_chars"] = len(prompt)
    if task.get("packet_meta"):
        log["packet_meta"] = task["packet_meta"]
    t0 = time.time()

    if shutil.which("claude") is None:
        worker_registry.finish(task["id"], "held", "no_cli", epoch=epoch)
        log_run(task=task["id"], role=task["role"], tier=task["tier"], account=acct.id, outcome="no_cli", **log)
        return {"status": "held", "reason": "claude CLI not found on PATH"}
    try:
        with bus.locked():
            if not worker_control.is_current(task["id"], epoch):
                return {"status": "superseded"}
            worker_registry.upsert(task["id"], status="starting", role=task["role"], provider="claude",
                                   model=model, account=acct.id, worktree=str(wt),
                                   branch=task.get("branch") or f"task/{task['id']}",
                                   parent=task.get("parent"), started_at=t0, epoch=epoch, tools=tools.split(",") if tools else [])
            p = subprocess.Popen(cmd, cwd=wt, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            worker_registry.event(task["id"], "spawned", pid=p.pid, account=acct.id,
                                  model=model, worktree=str(wt), branch=task.get("branch") or f"task/{task['id']}")
            if task.get("_steering"):
                worker_registry.event(task["id"], "steered", status="running")
            worker_control.write_if_current(task["id"], epoch, bus.update, task["id"], pid=p.pid, account=acct.id)
        stdout, stderr = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        worker_registry.finish(task["id"], "failed", "timeout", epoch=epoch)
        return {"status": "failed", "reason": f"timeout after {timeout}s"}
    except FileNotFoundError:
        worker_registry.finish(task["id"], "held", "no_cli", epoch=epoch)
        # shutil.which above should already catch this (gotcha 2026-09-19: a dead worker thread never
        # requeues cleanly), but a TOCTOU race (claude removed from PATH between the check and Popen) lands here.
        log_run(task=task["id"], role=task["role"], tier=task["tier"], account=acct.id, outcome="no_cli", **log)
        return {"status": "held", "reason": "claude CLI not found on PATH"}
    except OSError:
        worker_registry.finish(task["id"], "failed", "launch_error", epoch=epoch)
        raise
    with bus.locked():
        if not worker_control.is_current(task["id"], epoch):
            return {"status": "superseded"}
        text = stdout + stderr
        if p.returncode != 0 and is_rate_limited(text):
            secs = parse_reset_hint(text, pool.cfg["limits"]["cooldown_default_s"])
            pool.cooldown(acct, secs)
            log_run(task=task["id"], role=task["role"], tier=task["tier"], account=acct.id, outcome="rate_limit",
                        cooldown_s=secs, **log)
            worker_registry.finish(task["id"], "held", "rate_limit", epoch=epoch)
            return {"status": "held", "reason": f"rate_limit on {acct.id}, cooling {secs}s"}
        try:
            out = json.loads(stdout)
        except json.JSONDecodeError:
            worker_registry.finish(task["id"], "failed", "non_json", epoch=epoch)
            return {"status": "failed", "reason": f"non-JSON output (rc={p.returncode}): {text[-500:]}"}
        session_id = out.get("session_id")
        if session_id is not None:
            task.setdefault("packet_meta", {})["session_id"] = session_id
            bus.update(task["id"], packet_meta=task["packet_meta"])
        try:
            skills_used, skill_tokens_l2 = _skills_from_gate(task["id"], session_id)
            task.setdefault("packet_meta", {}).update(skills_used=skills_used, skill_tokens_l2=skill_tokens_l2)
            pipeline = dict(bus.get(task["id"]).get("pipeline") or {})
            pipeline["skills_used"] = skills_used
            bus.update(task["id"], packet_meta=task["packet_meta"], pipeline=pipeline)
            decision_log.outcome(task["id"], "skill_selection", skills_used=skills_used,
                                 skill_tokens_l2=skill_tokens_l2,
                                 skill_recovery=sorted(set(skills_used) - set(task["packet_meta"].get("skills_selected") or [])))
            log["packet_meta"] = task["packet_meta"]
        except Exception as exc:
            notify.notify(f"{task['id']}: skill telemetry unavailable: {exc}")
        used = out.get("usage", {})
        worker_registry.usage(task["id"], "claude", used, out.get("total_cost_usd"))
        worker_registry.finish(task["id"], "done" if not out.get("is_error") and p.returncode == 0 else "failed",
                               None if not out.get("is_error") and p.returncode == 0 else "process_error", epoch=epoch)
        n = used.get("input_tokens", 0) + used.get("output_tokens", 0) + used.get("cache_read_input_tokens", 0) // 10
        pool.record(acct, n)
        review_log = {}
        if task.get("role") == "review":
            parsed = extract_json(out.get("result", ""))
            if not parsed.get("verdict"):
                parsed = (bus.get(task["id"]).get("result") or parsed)
            parsed["packet_version"] = (task.get("packet_meta") or {}).get("version")
            facts = attribution.review_facts({**task, "result": parsed})
            review_log = {key: facts[key] for key in ("verdict", "findings_count", "findings_by_severity",
                          "reviewer_role", "checklist_used", "reviewed_sha", "packet_version", "review_pass_index")}
        log_run(task=task["id"], role=task["role"], tier=task["tier"], account=acct.id, duration_s=round(time.time() - t0, 1),
                    outcome="done" if p.returncode == 0 else "error", **({"usd": out.get("total_cost_usd")} if used else {}), turns=out.get("num_turns", 0),
                    provider="claude", usage=used, session_id=session_id, **log, **review_log, **used)
        if not out.get("is_error") and p.returncode == 0:
            return {"status": "done", "output": out}
        reason = f"budget or error exit (rc={p.returncode}): " + (out.get("result") or "")[:500]
        return {"status": "failed", "output": out, "reason": reason}


def extract_json(text):
    """Workers are told to return ONLY JSON; tolerate fences or prose around it."""
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    for body in reversed(fences):
        try:
            value = json.loads(body)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    candidates = []
    start = None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:index + 1])
                start = None
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

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


def run_worker(task_id, account_id=None, *, resume_task=None, resume_prompt=None, session_id=None):
    """Scout / triage / review / challenge: pick account, render prompt, run, post result. Holds instead of failing when no headroom."""
    pool = Pool(); t = dict(resume_task or bus.get(task_id)); role = t["role"]
    epoch = t.get("_launch_epoch", worker_control.launch_epoch(task_id))
    t["_launch_epoch"] = epoch
    recorded = worker_registry.get(task_id) if resume_task else None
    if (t.get("constraints") or {}).get("goal"):
        return {"status": "refused", "reason": "goal container"}
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
        worker_control.write_if_current(task_id, epoch, bus.update, task_id, status="held", hold_reason="no account with headroom")
        return {"status": "held"}
    lim = pool.cfg["limits"]
    model = pool.cfg["models"][t["tier"]]
    if resume_task:
        prompt = resume_prompt
        t["_resume_session"] = session_id
        model = recorded["model"]
        allowlist = ",".join(recorded["tools"])
        disclosure_mode = "off"
    else:
        try:
            skill_choice = None if harness_depth.active(t) else _prepare_skills(t, role, pool.cfg)
            if role == "review":
                src = reviewed if reviewed is not None else t
                role_packet = review_packet(t, src, cfg=pool.cfg, skills=skill_choice, provider="claude")
                security_signals = {"security": "## security\n" in role_packet}
                prompt = render("review", packet=role_packet, task=t, signals=security_signals)
                t["packet_meta"] = {**with_instruction_tokens(packet_run_meta(role_packet), prompt, role_packet), "role": role}
            elif role == "challenge":
                prompt = render("challenge", task=t, **{k: t["inputs"][0].get(k, "") if t["inputs"] and isinstance(t["inputs"][0], dict) else t["spec"]
                                                for k in ("claim", "evidence", "confidence")})
            elif role == "spec_review":
                src = bus.get(t["inputs"][0])
                role_packet = spec_review_packet(src)
                prompt = render("spec-review", packet=role_packet, task=t)
                t["packet_meta"] = {**with_instruction_tokens(packet_run_meta(role_packet), prompt, role_packet), "role": role}
            elif role == "execute":
                t["executor"] = f"claude:{t['tier']}"          # Codex was unavailable; the run log says which tier took it
                worker_control.write_if_current(task_id, epoch, bus.update, task_id, executor=t["executor"])
                packet_worktree = t.get("worktree") or ROOT
                role_packet = packet(t, packet_worktree, cfg=pool.cfg, skills=skill_choice, provider="claude")
                prompt = render("execute", packet=role_packet, task=t) + \
                    "\nYou are a Claude fallback executor (Codex is unavailable); a human reviews merges. Commit on the task branch when green." \
                    "\nIf you need a tool outside your allowlist, post bus_post_result with status held and result reason needs_tool:<tool id>."
                t["packet_meta"] = {**with_instruction_tokens(packet_run_meta(role_packet), prompt, role_packet), "role": role}
            else:
                role_packet = scout_packet(t)
                prompt = render("scout", packet=role_packet, task=t)
                t["packet_meta"] = {**with_instruction_tokens(packet_run_meta(role_packet), prompt, role_packet), "role": role}
        except Exception as exc:
            hold_render_error(task_id, exc)
            return {"status": "held", "reason": "render_error"}
        disclosure_meta = _shadow_tool_disclosure(t, role, pool.cfg, skill_choice)
        allowlist = disclosure_meta.pop("tool_allowlist", TOOLS.get(role, TOOLS["scout"]))
        disclosure_mode = disclosure_meta.pop("tool_disclosure_mode", "off")
        t["packet_meta"] = {**(t.get("packet_meta") or {}), **disclosure_meta}
        try:
            exposure = (_skill_exposure(t, role) if not harness_depth.active(t)
                        and promotion.mode("skill_routing", pool.cfg) == "off" else None)
            if exposure is None:
                records = _skill_records()
                exposed = sorted(skill_id for skill_id, record in records.items()
                                 if record.get("state") == "active" and record.get("provenance") == "builtin")
                exposure = {"skills_exposed": exposed,
                            "skill_tokens_l0": sum(int(records[item].get("est_tokens_l0") or 0) for item in exposed)}
            t["packet_meta"].update(exposure)
            t["packet_meta"].update(_skill_routing(t, role, pool.cfg, exposure, skill_choice))
            worker_control.write_if_current(task_id, epoch, bus.update, task_id, packet_meta=t["packet_meta"])
        except Exception as exc:
            notify.notify(f"{task_id}: skill telemetry unavailable: {exc}")
        if pool.reserve(task_id, acct.id, role, t) is None:
            pipeline = dict(t.get("pipeline") or {})
            pipeline["hold_note"] = "budget"
            pipeline.pop("dispatched_at", None)
            worker_control.write_if_current(task_id, epoch, bus.update, task_id, status="queued", pipeline=pipeline)
            return {"status": "budget"}
        bus.claim(task_id, f"claude:{acct.id}", str(ensure_worktree(task_id)))
        worker_control.write_if_current(task_id, epoch, bus.update, task_id, account=acct.id)  # explicit account, alongside assigned_to, for the avoid-derivation above
    r = None
    release_usage = None
    try:
        budget = lim["max_budget_usd"].get(role, 2.0)
        timeout = t["constraints"].get("timeout_s", lim["timeout_s"].get(role, 900))
        started = time.monotonic()
        first = run_claude(pool, acct, t, prompt, model, allowlist, budget, timeout)
        r = first
        if not worker_control.is_current(task_id, epoch):
            return {"status": "superseded"}
        tool_id = _hidden_tool_request(task_id)
        if (disclosure_mode == "active" and tool_id
                and not (bus.get(task_id).get("pipeline") or {}).get("tool_escalation_used")):
            spent = float(first.get("output", {}).get("total_cost_usd", 0) or 0)
            budget_left = budget - spent
            timeout_left = timeout - (time.monotonic() - started)
            if budget_left < budget * .1 or timeout_left < timeout * .1:
                reason = f"{NEEDS_TOOL_PREFIX}{tool_id} (no budget for respawn)"
                worker_control.write_if_current(task_id, epoch, bus.update, task_id, status="held", hold_reason=reason, result={"reason": reason})
                r = {"status": "held", "reason": reason, "output": first.get("output", {})}
            else:
                decision_log.record(kind="tool_disclosure", subject=task_id,
                    candidates=tool_catalog.disclosed(role), hard_constraints=[], selected=tool_id,
                    deterministic={"role": role, "requested_tool": tool_id},
                    reason="hidden_tool_requested", mode="active")
                pipeline = dict(bus.get(task_id).get("pipeline") or {})
                pipeline["tool_escalation_used"] = True
                worker_control.write_if_current(task_id, epoch, bus.update, task_id, status="running", result=None, pipeline=pipeline)
                r = run_claude(pool, acct, t, prompt, model, TOOLS.get(role, TOOLS["scout"]),
                               budget_left, timeout_left)
                release_usage = _combined_usage(first, r)
        session_id = r.get("output", {}).get("session_id")
        def resume_output(prompt, repair_budget):
            nonlocal release_usage
            reply = {}
            began = time.monotonic()
            try:
                reply = run_claude(pool, acct, t, prompt, model, "", repair_budget,
                                   min(timeout, 120), resume_session=session_id)
            finally:
                output = reply.get("output") or {}
                usage = output.get("usage") or {}
                release_usage = _combined_usage(release_usage or r, reply)
                bus.log_run(task=task_id, role="output_repair", provider="claude",
                            account=acct.id, tier=t["tier"], session_id=session_id,
                            outcome="done" if reply.get("status") == "done" else "failed",
                            usage=usage, usd=output.get("total_cost_usd"), budget_usd=repair_budget,
                            duration_s=round(time.monotonic() - began, 1), **usage)
            return {"result": extract_json(output.get("result", "")) if reply["status"] == "done" else {},
                    "tokens": usage, "usd": output.get("total_cost_usd", 0)}
        repair_session = resume_output if session_id else None
        if r["status"] == "done" and role == "execute":
            posted = fit_result({"summary": r["output"].get("result", "")[:3000], "executed_by": f"claude:{t['tier']}",
                                 "review": "other account, different model; label PR same-family-review"})
            posted = contracts.process(task_id, role, posted, cfg=pool.cfg, session=repair_session)
            worker_control.write_if_current(task_id, epoch, bus.post_result, task_id, posted, "done")
        elif r["status"] == "done":
            text = r["output"].get("result", "")
            result = extract_json(text)
            review_role = role in ("review", "spec_review")
            if role == "review":
                result["packet_version"] = (t.get("packet_meta") or {}).get("version")
            # A review worker may post its verdict itself via bus_post_result mid-run, then end with prose or
            # fenced JSON this parser can't take; or return valid JSON that simply lacks "verdict". Either way
            # treat it as a failed parse for review roles so we never silently drop an already-posted verdict
            # (T-0139: an approve sat unmerged after a second, verdict-less post overwrote the first).
            parse_failed = bool(result.get("parse_error")) or (review_role and not result.get("verdict"))
            existing_result = (bus.get(task_id).get("result") or {}) if parse_failed or review_role else {}
            if existing_result.get("verdict"):
                # Validate the authoritative posted result without offering a repair session
                # or replacing it with a deterministic candidate.
                contracts.process(task_id, role, existing_result, cfg=pool.cfg, preserve=True)
                result = existing_result
                if role == "review":
                    result["packet_version"] = (t.get("packet_meta") or {}).get("version")
                    worker_control.write_if_current(task_id, epoch, bus.post_result, task_id, fit_result(result), "done")
            else:
                # A model repair may supply a verdict only when neither output has one.
                session = repair_session if not review_role or not result.get("verdict") else None
                result = contracts.process(task_id, role, result, cfg=pool.cfg, session=session)
                if role == "review":
                    result["packet_version"] = (t.get("packet_meta") or {}).get("version")
                parse_failed = bool(result.get("parse_error")) or (review_role and not result.get("verdict"))
                if parse_failed and review_role:
                    worker_control.write_if_current(task_id, epoch, bus.update, task_id, status="failed", reason="review returned no parseable verdict",
                              resume_hint={"raw": text[-2000:]})
                    result = None
                else:
                    worker_control.write_if_current(task_id, epoch, bus.post_result, task_id, fit_result({"summary": result.get("summary", ""), **result}), "done")
            if result and review_role and result.get("verdict"):
                verdict_fields = {"spec_review_verdict": result["verdict"], "spec_review_risks": result.get("risks", [])} \
                    if role == "spec_review" else {"review_verdict": result["verdict"]}
                worker_control.write_if_current(task_id, epoch, bus.update, task_id, **verdict_fields)
                if role == "review":
                    completed_task = bus.get(task_id)
                    facts = attribution.review_facts(completed_task)
                    fact_fields = {key: value for key, value in facts.items()
                                   if value is not None or completed_task.get(key) is None}
                    worker_control.write_if_current(task_id, epoch, bus.update, task_id, review_facts=facts, **fact_fields)
                if t.get("inputs") and isinstance(t["inputs"][0], str):
                    try:
                        worker_control.write_if_current(task_id, epoch, bus.update, t["inputs"][0], **verdict_fields)
                    except KeyError:
                        pass
        elif r["status"] == "held":
            worker_control.write_if_current(task_id, epoch, bus.update, task_id, status="held", hold_reason=r.get("reason", "unknown failure"))
        else:
            update_fields = {"status": "failed", "reason": r.get("reason", "unknown failure")}
            result = r.get("output", {}).get("result") if isinstance(r.get("output"), dict) else None
            if isinstance(result, str):
                update_fields["resume_hint"] = {"partial_output": result[:2000]}
            worker_control.write_if_current(task_id, epoch, bus.update, task_id, **update_fields)
    except Exception as e:
        bus.log_run(task=task_id, role=role, outcome="post_failed",
                    executor=t.get("executor") or f"claude:{t['tier']}", complexity=t["complexity"])
        worker_control.write_if_current(task_id, epoch, bus.update, task_id, status="failed", reason=f"post_result failed: {e}"[:500])
    finally:
        worker_control.release_if_current(task_id, epoch, pool, release_usage or r or {})
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


def resume_worker(task, prompt, session_id):
    """Use the original role, account, model, tools, and environment policy."""
    recorded = worker_registry.get(task["id"])
    return run_worker(task["id"], recorded["account"], resume_task=task,
                      resume_prompt=prompt, session_id=session_id)
