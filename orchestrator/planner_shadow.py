"""Isolated shadow Planner launches and bounded, prompt-free evaluation rows."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
import threading
import time

from . import ROOT, STATE, bus, goals, jev, spawn, env_policy

_ACTIONS = frozenset(("fix_round", "respec", "escalate", "noop", "synthesize_now",
                      "wait_for_more", "drop_low_confidence", "write_specs", "close", "other"))
_LOCK = threading.Lock()


def eligible(router_decision, cfg, *, sample=None):
    """Defensively re-check the caller's router draw; never draw a second sample."""
    if router_decision.mode != "shadow":
        return False, "mode_not_shadow"
    if router_decision.hold:
        return False, "held"
    if router_decision.shadow_tier is None:
        return False, "no_shadow_tier"
    if sample is not None and sample > cfg["shadow_sample_rate"]:
        return False, "sampled_out"
    return True, "eligible"


def argv(prompt, *, model, budget_usd, system_prompt_path):
    return ["claude", "-p", prompt, "--model", model, "--output-format", "json",
            "--max-budget-usd", str(budget_usd), "--mcp-config", ".mcp.planner-shadow.json",
            "--strict-mcp-config", "--disallowedTools",
            "Edit,Write,MultiEdit,NotebookEdit,Bash,Task,WebFetch,WebSearch",
            "--append-system-prompt", Path(system_prompt_path).read_text(encoding="utf-8"),
            "--dangerously-skip-permissions"]


def launch(prompt, *, model, account, budget_usd, log, root=ROOT, popen=None, task_id=None):
    """Launch independently of production; never resolve Planner service secrets."""
    try:
        root, log = Path(root), Path(log)
        command = argv(spawn.render("planner-shadow", packet=prompt), model=model,
                       budget_usd=budget_usd,
                       system_prompt_path=root / ".orchestrator/prompts/planner.md")
        extra = dict(CLAUDE_CONFIG_DIR=os.path.expanduser(account["config_dir"]),
                   ORCH_ROOT=str(root), ORCH_SHADOW="1")
        token = os.environ.get(account.get("oauth_token_env") or "")
        if token:
            extra["CLAUDE_CODE_OAUTH_TOKEN"] = token
        env, _ = env_policy.worker_env("planner_shadow", base=os.environ, extra=extra,
                                       cfg=spawn.Pool().cfg, task_id=task_id or log.stem, root=root)
        log.parent.mkdir(parents=True, exist_ok=True)
        stderr_log = str(log) + ".stderr"
        with log.open("w", encoding="utf-8") as stdout, open(stderr_log, "w", encoding="utf-8") as stderr:
            proc = (popen if popen is not None else goals.Popen)(
                command, cwd=root, env=env, stdout=stdout, stderr=stderr,
                start_new_session=True)
        return {"pid": proc.pid, "log": str(log), "stderr_log": stderr_log, "argv": command}
    except Exception as exc:
        print(f"shadow Planner launch failed: {jev.redact(str(exc))}", file=sys.stderr)
        return None


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _text(value, cap):
    return jev.redact(value)[:cap] if isinstance(value, str) else ""


def _strings(value, cap):
    return [_text(item, cap) for item in value if isinstance(item, str)][:10] if isinstance(value, list) else []


def _decision(result):
    action = result.get("proposed_action")
    tasks = result.get("tasks_proposed")
    confidence = _number(result.get("confidence"))
    return {
        "proposed_action": action if isinstance(action, str) and action in _ACTIONS else "other",
        "summary": _text(result.get("summary"), 800),
        "tasks_proposed": [{"title": _text(task.get("title"), 120),
                            "scope": _strings(task.get("scope"), 800)}
                           for task in tasks if isinstance(task, dict)][:10] if isinstance(tasks, list) else [],
        "confidence": max(0.0, min(1.0, confidence)) if confidence is not None else None,
        "needs_fable": result.get("needs_fable") is True,
        "unresolved": _strings(result.get("unresolved"), 200),
    }


def parse(log_path):
    """Read the final JSON envelope, retaining usage even on invalid result text."""
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    envelope = {}
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        envelope = value if isinstance(value, dict) else {}
        break
    usage = envelope.get("usage")
    fields = {"usage": bus.normalize_usage("claude", usage if isinstance(usage, dict) else {}),
              "total_cost_usd": _number(envelope.get("total_cost_usd")),
              "session_id": _text(envelope.get("session_id"), 200) or None,
              "is_error": bool(envelope.get("is_error"))}
    result_text = envelope.get("result")
    result = spawn.extract_json(result_text) if isinstance(result_text, str) and "{" in result_text else None
    if not isinstance(result, dict) or result.get("parse_error"):
        return {"parse_error": True, **fields}
    return {**_decision(result), **fields}


def record(*, launch_id, goal_id, decision_type, state_version, model, tier,
           started_at, parsed, latency_s, root=None):
    """Append one whitelisted result directly to the shadow ledger."""
    parsed = parsed or {"parse_error": True}
    result = {"parse_error": True} if parsed.get("parse_error") else _decision(parsed)
    result.update({key: parsed[key] for key in
                   ("usage", "total_cost_usd", "session_id", "is_error") if key in parsed})
    row = {**result, "launch_id": launch_id, "goal_id": goal_id,
           "decision_type": decision_type, "state_version": state_version,
           "model": model, "tier": tier, "started_at": started_at,
           "latency_s": latency_s, "ts": time.time()}
    path = (Path(root) if root is not None else STATE) / "runs/sched/planner_shadow.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")
    return row


def production_action(materiality):
    if materiality.get("escalated") is True:
        return "escalate"
    if materiality.get("fix_strategy_for"):
        return "fix_round"
    if materiality.get("new_tasks"):
        return "write_specs"
    return "noop"


def score(shadow_row, production_materiality):
    shadow = shadow_row.get("proposed_action", "other")
    production = production_action(production_materiality) if production_materiality is not None else None
    agreement = None
    if production is not None and not shadow_row.get("parse_error"):
        agreement = ("write_specs" if shadow == "synthesize_now" else shadow) == production
    matched = None
    if production_materiality is not None and "escalated" in production_materiality:
        matched = bool(shadow_row.get("needs_fable")) == bool(production_materiality["escalated"])
    return {"agreement": agreement, "basis": f"shadow={shadow}; production={production}",
            "needs_fable_matched": matched}


def summary(root=None):
    """Summarize valid ledger rows; unknown agreement is excluded from its rate."""
    path = (Path(root) if root is not None else STATE) / "runs/sched/planner_shadow.jsonl"
    rows = []
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    n = len(rows)
    agreements = [row["agreement"] for row in rows if type(row.get("agreement")) is bool]
    tokens = 0
    for row in rows:
        usage = row.get("usage")
        if isinstance(usage, dict):
            tokens += _number(usage.get("total_tokens")) or 0
    return {"n": n,
            "agreement_rate": sum(agreements) / len(agreements) if agreements else None,
            "needs_fable_rate": sum(bool(row.get("needs_fable")) for row in rows) / n if n else None,
            "parse_error_rate": sum(bool(row.get("parse_error")) for row in rows) / n if n else None,
            "usd": sum(_number(row.get("total_cost_usd")) or 0 for row in rows),
            "tokens": tokens}
