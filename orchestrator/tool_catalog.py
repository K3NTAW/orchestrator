"""Level-0 tool catalog and shadow disclosure policy.

Claude built-in schemas are owned by Claude Code, so their sizes below are
deliberately stable estimates.  Repository MCP tools are measured from their
signature and docstring at import time (characters // 4).
"""
import ast
import json
import re
from pathlib import Path


_BUILTIN_TOKENS = {"Read": 350, "Grep": 400, "Glob": 200, "Edit": 300, "Write": 200}
_LINES = {
    "Read": ("read files", "read"), "Grep": ("search file contents", "search"),
    "Glob": ("find files by pattern", "search"), "Edit": ("edit an existing file", "edit"),
    "Write": ("write a file", "edit"),
}
_BASH = {
    "Bash(git *)": ("inspect or update git state", "git"),
    "Bash(rg *)": ("search repository text", "search"),
    "Bash(ls *)": ("list directory contents", "shell"),
    "Bash(bash skills/*)": ("run repository skill scripts", "shell"),
    "Bash(npm *)": ("run npm commands", "shell"),
    "Bash(npx *)": ("run package executables", "shell"),
    "Bash(uv *)": ("run Python tooling with uv", "shell"),
    "Bash(pytest *)": ("run pytest tests", "test"),
    "Bash(python3 *)": ("run Python programs", "shell"),
    "Bash(.claude/hooks/tests-green.sh*)": ("run the repository test gate", "test"),
}
_MCP = {
    "bus_create_task": ("create a task on the bus", "bus"),
    "bus_claim": ("claim a task for a worker", "bus"),
    "bus_post_result": ("post a worker result", "bus"),
    "bus_read": ("read tasks from the bus", "bus"),
    "bus_events": ("read bus events", "bus"),
    "codex": ("implement software tasks", "executor"),
    "codex_reply": ("continue a Codex fix loop", "executor"),
    "spawn_scout": ("start a research worker", "search"),
    "spawn_review": ("start a code review worker", "review"),
    "spawn_challenge": ("start an evidence challenge worker", "review"),
    "spawn_spec_review": ("start a specification review worker", "review"),
    "merge": ("merge a completed task", "git"),
    "status": ("show orchestrator status", "graph"),
    "executor_fallback": ("choose an executor fallback", "executor"),
    "hold_account": ("temporarily hold an account", "graph"),
    "resume_account": ("resume a held account", "graph"),
}


_SCHEMAS = {}


def _mcp_measurements():
    root = Path(__file__).parent
    measured = {}
    for filename in ("bus_mcp.py", "mcp.py"):
        tree = ast.parse((root / filename).read_text())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _MCP:
                signature = ast.unparse(node.args)
                _SCHEMAS[node.name] = f"{node.name}({signature})\n{ast.get_docstring(node) or chr(32)}"
                measured[node.name] = (len(signature) + len(ast.get_docstring(node) or "")) // 4
    return measured


_MEASURED = _mcp_measurements()
CATALOG = {tool: {"line": line, "category": category, "est_schema_tokens": _BUILTIN_TOKENS[tool]}
           for tool, (line, category) in _LINES.items()}
CATALOG.update({tool: {"line": line, "category": category, "est_schema_tokens": 500}
                for tool, (line, category) in _BASH.items()})
for name, (line, category) in _MCP.items():
    tool_id = f"mcp__bus__{name}" if name.startswith("bus_") else name
    CATALOG[tool_id] = {"line": line, "category": category,
                        "est_schema_tokens": _MEASURED.get(name, 0)}
CATALOG["CODEX_TOOLS"] = {"line": "codex CLI built-ins; not controlled here",
                           "category": "executor", "est_schema_tokens": 0}


def level0(ids):
    return "\n".join(f"- {tool_id} — {CATALOG[tool_id]['line']}" for tool_id in ids)


def disclosed(role):
    if role == "codex_execute":
        return ["CODEX_TOOLS"]
    from .spawn import TOOLS
    return [item.strip() for item in TOOLS.get(role, TOOLS["scout"]).split(",") if item.strip()]


def tokens(ids):
    return sum(CATALOG[tool_id]["est_schema_tokens"] for tool_id in ids)


def _task_class(task):
    from .attribution import task_class
    return task_class(task)


def minimal_set(task, role):
    offered = disclosed(role)
    if role == "codex_execute":
        return {"keep": offered, "drop": [], "mandatory": offered,
                "reason": "Codex CLI tool schemas are not controlled by this repository"}
    docs_only = bool(task.get("scope")) and all(Path(path).suffix.lower() == ".md" for path in task["scope"])
    task_class = _task_class(task)
    needed = {
        "review": {"read", "search", "git", "bus", "review"},
        "scout": {"read", "search", "git", "bus"},
        "triage": {"read", "search", "bus"},
        "challenge": {"read", "search", "git", "bus"},
        "spec_review": {"read", "search", "git", "bus", "review"},
        # Routing classes describe difficulty/risk, not the implementation's
        # command needs. Keep shell and tests conservatively for every class;
        # only concrete task evidence (docs-only scope below) narrows them.
        "execute": {"read", "search", "edit", "shell", "test", "git", "bus"},
    }.get(role, {"read", "search", "bus"})
    acceptance = " ".join(map(str, task.get("acceptance") or [])).lower()
    if role == "execute" and ("test" in acceptance or "gate" in acceptance):
        needed.add("test")
    if docs_only:
        needed -= {"test", "shell"}
    mandatory = [tool for tool in offered if tool in {"Read", "mcp__bus__bus_post_result"}]
    if role in {"review", "scout"} and "Bash(git *)" in offered:
        mandatory.append("Bash(git *)")
    if role == "execute" and "Bash(.claude/hooks/tests-green.sh*)" in offered:
        mandatory.append("Bash(.claude/hooks/tests-green.sh*)")
    if role == "execute":
        mandatory.extend(tool for tool in ("Edit", "Write") if tool in offered)
    keep = [tool for tool in offered if CATALOG[tool]["category"] in needed or tool in mandatory]
    drop = [tool for tool in offered if tool not in keep]
    reason = f"{role}/{task_class}: categories {','.join(sorted(needed))}"
    if docs_only:
        reason += "; docs-only scope drops optional test and shell tools"
    return {"keep": keep, "drop": drop, "mandatory": mandatory, "reason": reason}


def recovery_events(root):
    """Find recorded attempts to use undisclosed tools; current run rows rarely carry this signal."""
    events = []
    pattern = re.compile(r"permission denied|not allowed|disallowed tool|outside.*allowlist", re.I)
    for path in sorted((Path(root) / "runs").glob("*.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if pattern.search(json.dumps(row, sort_keys=True)):
                events.append(row)
    return {"events": events, "note": None if events else
            "permission-denial/tool-request signal is not yet recorded in structured run rows"}


def level2(ids):
    """Render repository signatures; provider-owned schemas remain external."""
    parts = []
    for tool_id in ids:
        name = tool_id.removeprefix("mcp__bus__")
        definition = _SCHEMAS.get(name)
        if definition is None:
            definition = (CATALOG[tool_id]["line"] +
                          "; schema supplied by the worker harness")
        parts.append(f"### {tool_id}\n{definition}")
    return "\n\n".join(parts)


def cache_view(role, keep, previous_row):
    """Measure the static role catalog and selected definitions without I/O."""
    return {
        "stable_catalog_chars": len(level0(disclosed(role))),
        "dynamic_chars": len(level2(keep)),
        "changed_since_previous": (None if previous_row is None else
            set(keep) != set(previous_row["deterministic"]["kept"])),
    }


def cache_fields(task, role, keep, cfg):
    """Attach optional cache measurements to either disclosure writer."""
    from . import STATE, context_router, decision_log
    data = (cfg.get("_cache_decisions") or {}).get("tool_disclosure")
    if data is None:
        try:
            configured = context_router.cache_mode(cfg, "tool_disclosure")
            mode, reason = context_router.effective_cache_mode(cfg, "tool_disclosure", root=STATE)
        except ValueError:
            return {"cache_mode": "off", "invalid_config": True}
        data = {"configured_cache_mode": configured, "cache_mode": mode,
                "refused_reason": reason, "invalid_config": False}
    if data["cache_mode"] == "off":
        return data if data.get("invalid_config") else {}
    previous = decision_log.last_row("tool_disclosure", role=role,
        exclude_subject=task.get("id"), require_key="deterministic.kept")
    return {**cache_view(role, keep, previous), **data}
