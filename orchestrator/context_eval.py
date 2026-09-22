"""Deterministic fixture evaluation for context-economy shadow features."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile

from . import bus, decision_log, evidence, schedlog, spawn

CATEGORIES = ("localized_fix", "cross_cutting", "security", "test_failure",
              "documentation", "review", "repeated_fix_round")
EXPECTATIONS = {
    "localized_fix": (.4, 1.0), "cross_cutting": (.1, .4),
    "security": (0, 1), "test_failure": (0, 1), "documentation": (0, 1),
    "review": (0, 1), "repeated_fix_round": (0, 1),
}


@contextmanager
def isolated_state(root):
    """Redirect every state binding used while fixtures build, then restore it."""
    root = Path(root)
    state = root / ".orchestrator"
    state.mkdir(parents=True, exist_ok=True)
    modules = (spawn, evidence, schedlog, bus)
    names = {spawn: ("ROOT", "STATE"), evidence: ("STATE",),
             schedlog: ("SCHED_DIR",), bus: ("ROOT", "STATE", "TASKS", "RUNS", "LOCK")}
    old_env = os.environ.get("ORCH_ROOT")
    saved = {(module, name): getattr(module, name) for module in modules for name in names[module]}
    os.environ["ORCH_ROOT"] = str(root)
    try:
        spawn.ROOT = bus.ROOT = root
        spawn.STATE = evidence.STATE = bus.STATE = state
        schedlog.SCHED_DIR = state / "runs" / "sched"
        bus.TASKS, bus.RUNS, bus.LOCK = state / "tasks", state / "runs", state / bus.LOCK_NAME
        yield state
    finally:
        for (module, name), value in saved.items():
            setattr(module, name, value)
        if old_env is None:
            os.environ.pop("ORCH_ROOT", None)
        else:
            os.environ["ORCH_ROOT"] = old_env


def _fixture(category, root):
    wt = Path(root) / "fixture"
    wt.mkdir(parents=True, exist_ok=True)
    files = {
        "app.py": "def target():\n    return 1\n" + "# local detail\n" * 120,
        "auth/security.py": "def authorize(token):\n    return bool(token)\n" + "# security evidence\n" * 80,
        "tests/test_app.py": "def test_target():\n    assert False, 'named failure'\n" + "# test evidence\n" * 50,
        "docs/guide.md": "# Guide\n" + "documentation source paragraph\n" * 100,
        "support.py": "def helper():\n    return 2\n" + "# supporting detail\n" * 100,
    }
    for name, text in files.items():
        path = wt / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if not (wt / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
        subprocess.run(["git", "add", "."], cwd=wt, check=True)
        subprocess.run(["git", "-c", "user.name=Context Eval", "-c", "user.email=eval@invalid",
                        "commit", "-qm", "fixture"], cwd=wt, check=True)
    scope = {
        "localized_fix": ["app.py"], "cross_cutting": list(files),
        "security": ["auth/security.py"], "test_failure": ["app.py", "tests/test_app.py"],
        "documentation": ["docs/missing.py"], "review": ["app.py"],
        "repeated_fix_round": ["app.py"],
    }[category]
    spec = "Fix the target with supporting context."
    constraints = {"task_class": category}
    if category == "test_failure":
        spec += "\nFailures:\ntests/test_app.py::test_target FAIL named failure"
        constraints["fix_round_for"] = "T-EVAL-BASE"
    if category == "repeated_fix_round":
        constraints["fix_round_for"] = "T-EVAL-BASE"
        spec += "\nFailures:\ntests/test_app.py::test_target FAIL repeated"
    inputs = [{"summary": "unrelated historical material " * 10}
              for _ in range(18 if category == "localized_fix" else 5 if category == "cross_cutting" else 0)]
    task = {"id": "T-EVAL-" + category.upper(), "role": "review" if category in ("review", "security") else "execute",
            "title": category.replace("_", " "), "spec": spec,
            "acceptance": ["tests/test_app.py::test_target passes"], "scope": scope,
            "constraints": constraints, "worktree": str(wt), "parent": None, "inputs": inputs}
    if category in ("review", "security"):
        target = wt / ("auth/security.py" if category == "security" else "app.py")
        target.write_text(target.read_text() + "\n# pending review change\n")
    return wt, task


def _cfg(mode):
    return {"context_router": {"mode": mode}, "tool_disclosure": {"mode": mode},
            "instructions": {"mode": mode}, "handoff": {"mode": mode},
            "review": {"security_paths": ["auth/*"]}, "limits": {"review_diff_chars": 12000}}


def _selection(root, subject):
    path = Path(root) / "runs" / "sched" / "decisions.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else ():
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if row.get("kind") == "context_selection" and row.get("subject") == subject:
            rows.append(row)
    return rows[-1] if rows else {}


def run(category, root):
    if category not in CATEGORIES:
        raise ValueError(category)
    with isolated_state(root) as state:
        wt, task = _fixture(category, root)
        builder = spawn.review_packet if task["role"] == "review" else spawn.packet
        if task["role"] == "review":
            original_diff = spawn.scoped_diff
            spawn.scoped_diff = lambda _task: subprocess.run(
                ["git", "diff", "--", *task["scope"]], cwd=wt,
                capture_output=True, text=True, check=True).stdout
            try:
                off_text = builder(task, task, cfg=_cfg("off"))
                off_meta = {"presented_tokens": len(off_text) // 4}
                shadow_text = builder(task, task, cfg=_cfg("shadow"))
            finally:
                spawn.scoped_diff = original_diff
        else:
            off_text = builder(task, wt, cfg=_cfg("off"))
            off_meta = spawn.packet_run_meta(off_text)
            shadow_text = builder(task, wt, cfg=_cfg("shadow"))
        meta = spawn.packet_run_meta(shadow_text)
        decision = _selection(state, task["id"])
        deterministic = decision.get("deterministic") or {}
        candidates = int(deterministic.get("candidate_tokens") or meta.get("candidate_tokens") or 0)
        routed = int(deterministic.get("routed_tokens") or meta.get("routed_tokens") or candidates)
        reduction = round(1 - routed / candidates, 3) if candidates else 0.0
        tagged = decision.get("candidates") or []
        full = [value.rsplit(":", 1)[0] for value in tagged if value.endswith(":FULL")]
        full_reasons = decision.get("hard_constraints") or []
        must_keep, kept = [], True
        if category == "localized_fix":
            must_keep = ["app.py"]
        elif category == "security":
            must_keep = ["auth/security.py", "security instructions"]
            kept = "security_path" in full_reasons and "## security" in shadow_text
        elif category == "test_failure":
            must_keep = ["failing output", "tests/test_app.py::test_target"]
            kept = full_reasons.count("failing_output") >= 1 and "tests/test_app.py::test_target" in off_text
        elif category == "documentation":
            must_keep = ["no source_chunk FULL"]
            kept = not any("source_chunk" in value for value in full)
        elif category == "review":
            must_keep = ["diff", "acceptance", "interfaces", "no implementation transcript"]
            kept = all("## " + name in shadow_text for name in ("diff", "acceptance", "scope")) and "transcript" not in shadow_text.lower()
        elif category == "repeated_fix_round":
            must_keep = ["prior evidence overlap >= 0.8"]
            first = set(meta.get("evidence_ids") or [])
            again = builder(task, wt, cfg=_cfg("shadow"))
            second = set(spawn.packet_run_meta(again).get("evidence_ids") or [])
            kept = len(first & second) / max(1, len(first)) >= .8
        else:
            kept = bool(full)
        if category == "localized_fix":
            kept = "in_scope_file" in full_reasons
        low, high = EXPECTATIONS[category]
        instruction_modular = meta.get("instruction_tokens_modular")
        return {"category": category, "candidate_tokens": candidates,
                "presented_tokens": off_meta["presented_tokens"], "routed_tokens": routed,
                "reduction": reduction, "hidden": int(deterministic.get("HIDE") or meta.get("routed_hidden") or 0),
                "instruction_delta": (instruction_modular - off_meta["presented_tokens"]
                                      if isinstance(instruction_modular, int) else 0),
                "tool_delta": int(meta.get("tool_tokens_minimal") or 0) - int(meta.get("tool_tokens_disclosed") or 0),
                "expectation": {"min_reduction": low, "max_reduction": high},
                "within_expectation": low <= reduction <= high, "must_keep": must_keep, "kept": kept}


def run_all(root=None):
    base = Path(root) if root else Path(tempfile.mkdtemp(prefix="orchestrator-context-eval-"))
    return [run(category, base / category) for category in CATEGORIES]


def result_document(results):
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        head = ""
    return {"ran_at": datetime.now(timezone.utc).isoformat(), "git_head": head or "(unavailable)",
            "results": results, "suite_passed": all(x["within_expectation"] and x["kept"] for x in results)}


def format_report(results):
    lines = ["category\tcandidate\tpresented\trouted\treduction\thidden\twithin\tkept"]
    lines += [f"{r['category']}\t{r['candidate_tokens']}\t{r['presented_tokens']}\t{r['routed_tokens']}\t{r['reduction']}\t{r['hidden']}\t{r['within_expectation']}\t{r['kept']}" for r in results]
    return "\n".join(lines)
