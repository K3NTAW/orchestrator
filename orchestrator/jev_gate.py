"""PreToolUse gate for worker sessions (.claude/hooks/jev-gate.sh): ask Jev whether a proposed tool call is
needed/redundant/destructive against the task's acceptance criteria and recent tool history, and in
[jev].gate_mode = "block" deny the call on a confident redundant-or-unneeded verdict. Every evaluated call is
logged to .orchestrator/runs/jev/gate.jsonl. Fails open on anything unusual: Jev disabled or unreachable
(ask() returns None), a missing/corrupt transcript, or a malformed answer -- the caller never blocks on our
account. destructive is logged only; guardrails.sh is the one hook that actually blocks destructive commands.
"""
import time
_IMPORTED_AT = time.time()
import json, os, re, sys
from functools import cache
from . import STATE
from . import pool as P

GATE_LOG = STATE / "runs" / "jev" / "gate.jsonl"
RECENT_LIMIT = 20
NEEDED_LOW = 0.15
REDUNDANT_HIGH = 0.85
CONFIDENCE_MIN = 0.6
GATED_ROLES = frozenset({"scout", "triage", "execute", "review", "challenge", "spec_review"})
# Empty input on any tool, or a Glob containing only its pattern, needs no judgment.
SKIP_RULES = ("empty_input", "bare_glob")

QUESTIONS = {
    "needed": {"type": "noul", "instructions":
               "Does the agent need this tool call to satisfy the acceptance criteria, given what it has "
               "already read or run?"},
    "redundant": {"type": "noul", "instructions":
                  "Does this call repeat an earlier call whose result the agent already has, with no "
                  "intervening change to that file or command?"},
    "destructive": {"type": "noul", "instructions":
                     "Would this call delete, overwrite, force-push, or exfiltrate data outside the task "
                     "scope?"},
}

# bus_post_result never reaches this hook via the PreToolUse matcher (MCP tools aren't matched), but is listed
# here too so direct callers of is_protected() get the same answer the matcher would imply.
PROTECTED_TOOL_NAMES = {"bus_post_result", "mcp__bus__bus_post_result"}
_PROTECTED_BASH_RES = [
    re.compile(r"tests-green\.sh"),
    re.compile(r"(?:^|[;&|]|\s)git\s+(?:commit|add|status|diff|log)(?:\s|$)"),
]


@cache
def _cfg():
    try:
        raw = P.config()
    except FileNotFoundError:
        raw = {}
    return raw.get("jev") or {}


def is_enabled():
    return bool(_cfg().get("enabled", False))


def should_skip(tool_name, tool_input):
    return (("empty_input" in SKIP_RULES and not tool_input)
            or ("bare_glob" in SKIP_RULES and tool_name == "Glob"
                and isinstance(tool_input, dict) and set(tool_input) == {"pattern"}))


def is_protected(tool_name, tool_input):
    if tool_name in PROTECTED_TOOL_NAMES:
        return True
    if tool_name == "Bash":
        cmd = (tool_input or {}).get("command") or ""
        return any(p.search(cmd) for p in _PROTECTED_BASH_RES)
    return False


def read_transcript(path, limit=RECENT_LIMIT):
    """Last `limit` tool_use blocks from a Claude Code transcript JSONL, as {name, input, error}. Missing,
    unreadable or partially malformed lines are skipped; this never raises, and returns [] rather than failing
    the caller open/closed on a bad transcript."""
    from . import jev
    if not path:
        return []
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except OSError:
        return []

    tool_uses = []
    errors = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            content = (obj.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            kind = obj.get("type")
            if kind == "assistant":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_uses.append({"id": block.get("id"), "name": block.get("name"),
                                           "input": block.get("input")})
            elif kind == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        errors[block.get("tool_use_id")] = bool(block.get("is_error"))
        except Exception:
            continue

    recent = []
    for tu in tool_uses[-limit:]:
        try:
            redacted = jev.redact(json.dumps(tu.get("input") or {}, ensure_ascii=False))[:300]
        except Exception:
            redacted = ""
        recent.append({"name": tu.get("name"), "input": redacted, "error": errors.get(tu.get("id"), False)})
    return recent


def load_task(task_id):
    from . import bus
    try:
        return bus.get(task_id)
    except Exception:
        return None


def _target(tool_name, tool_input):
    ti = tool_input or {}
    for key in ("file_path", "path", "notebook_path", "pattern", "url"):
        if ti.get(key):
            return ti[key]
    if tool_name == "Bash":
        return (ti.get("command") or "")[:60]
    return ""


def build_state(task, recent, tool_name, tool_input):
    from . import jev
    task = task or {}
    return {
        "task": {
            "title": task.get("title"),
            "spec": (task.get("spec") or "")[:1500],
            "acceptance": task.get("acceptance"),
            "scope": task.get("scope"),
            "role": task.get("role"),
        },
        "recent": recent,
        "proposed": {
            "tool_name": tool_name,
            "input": jev.redact(json.dumps(tool_input or {}, ensure_ascii=False))[:300],
        },
    }


def ask_jev(state):
    """{"needed"|"redundant"|"destructive": {"p", "confidence"} or None}, or None if Jev is unavailable."""
    from . import jev
    result = jev.ask(state, QUESTIONS)
    if result is None:
        return None
    answers = result.get("answers") or {}
    out = {}
    for key in QUESTIONS:
        a = answers.get(key) or {}
        try:
            out[key] = {"p": a["noul"], "confidence": a.get("confidence")}
        except (KeyError, TypeError):
            out[key] = None
    return out


def decide(answers, mode):
    """(blocked, reason) where reason is "redundant", "needed" or None. Only gate_mode="block" ever blocks;
    destructive is logged only (guardrails.sh owns blocking destructive commands)."""
    if mode != "block" or not answers:
        return False, None
    cfg = _cfg()
    redundant = answers.get("redundant") or {}
    p_r, c_r = redundant.get("p"), redundant.get("confidence")
    if p_r is not None and (
        (c_r is None and p_r >= cfg.get("block_redundant_p_noconf", 0.92))
        or (c_r is not None and c_r >= CONFIDENCE_MIN
            and p_r >= cfg.get("block_redundant_p", REDUNDANT_HIGH))
    ):
        return True, "redundant"
    needed = answers.get("needed") or {}
    p_n, c_n = needed.get("p"), needed.get("confidence")
    if p_n is not None and (
        (c_n is None and p_n <= cfg.get("block_needed_p_noconf", 0.08))
        or (c_n is not None and c_n >= CONFIDENCE_MIN
            and p_n <= cfg.get("block_needed_p", NEEDED_LOW))
    ):
        return True, "needed"
    return False, None


def _message(reason, tool_name, tool_input):
    if reason == "redundant":
        return f"jev-gate: this call repeats {tool_name} on {_target(tool_name, tool_input)}; use the result you already have"
    return "jev-gate: not needed for the acceptance criteria; continue with the next criterion"


def _log(task_id, session_id, tool_name, answers, mode, blocked, scored, latency_ms, startup_ms=0.0):
    answers = answers or {}
    _, reason = decide(answers, "block")
    rule = ("none" if reason is None else
            "noconf" if answers[reason].get("confidence") is None else "conf")
    needed = answers.get("needed") or {}
    entry = {
        "ts": time.time(), "task": task_id, "session": session_id, "tool": tool_name,
        "rule": rule,
        "p_needed": needed.get("p"),
        "p_redundant": (answers.get("redundant") or {}).get("p"),
        "p_destructive": (answers.get("destructive") or {}).get("p"),
        "confidence": needed.get("confidence"),
        "mode": mode, "blocked": blocked, "latency_ms": latency_ms, "scored": scored,
        "startup_ms": startup_ms,
    }
    GATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(GATE_LOG, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def run(payload):
    """Evaluate one PreToolUse call; returns 0 (allow) or 2 (block, with one line already on stderr)."""
    task_id = os.environ.get("ORCH_TASK_ID", "")
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""

    cfg = _cfg()
    mode = cfg.get("gate_mode", "log")
    if not task_id or not cfg.get("enabled", False):
        return 0

    if should_skip(tool_name, tool_input):
        return 0
    task = load_task(task_id)
    if not task or task.get("role") not in cfg.get("gate_roles", GATED_ROLES):
        return 0

    if is_protected(tool_name, tool_input):
        _log(task_id, session_id, tool_name, None, mode, blocked=False, scored=False, latency_ms=0.0)
        return 0

    from . import jev  # Network imports only after enabled, role and skip checks.
    recent = read_transcript(transcript_path)
    state = build_state(task, recent, tool_name, tool_input)

    started = time.monotonic()
    startup_ms = max(0.0, (time.time() - float(os.environ.get("ORCH_JEV_STARTED_AT", _IMPORTED_AT))) * 1000)
    answers = ask_jev(state)
    latency_ms = (time.monotonic() - started) * 1000

    if answers is None:
        _log(task_id, session_id, tool_name, None, mode, blocked=False, scored=False, latency_ms=latency_ms, startup_ms=startup_ms)
        return 0

    blocked, reason = decide(answers, mode)
    _log(task_id, session_id, tool_name, answers, mode, blocked=blocked, scored=True, latency_ms=latency_ms, startup_ms=startup_ms)
    if blocked:
        print(_message(reason, tool_name, tool_input), file=sys.stderr)
        return 2
    return 0


def main(argv=None):
    # This CLI handles one hook per process. Share its snapshot with Jev and secret resolution too.
    P.config = cache(P.config)
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--enabled":
        print("1" if is_enabled() else "0")
        return 0
    try:
        if not is_enabled():
            return 0
        payload = json.loads(sys.stdin.read())
        return run(payload)
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
