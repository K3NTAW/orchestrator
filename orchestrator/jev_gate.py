"""PreToolUse gate for worker sessions (.claude/hooks/jev-gate.sh): ask Jev whether a proposed tool call is
needed/redundant/destructive against the task's acceptance criteria and recent tool history, and in
[jev].gate_mode = "block" deny the call on a confident redundant-or-unneeded verdict. Every evaluated call is
logged to .orchestrator/runs/jev/gate.jsonl. Fails open on anything unusual: Jev disabled or unreachable
(ask() returns None), a missing/corrupt transcript, or a malformed answer -- the caller never blocks on our
account. destructive is logged only; guardrails.sh is the one hook that actually blocks destructive commands.
"""
import hashlib
import time
_IMPORTED_AT = time.time()
import json, os, re, sys, tempfile
from functools import cache
from . import STATE
from . import pool as P

# Keep the disabled hook free of network imports. jev consumes this declaration
# on import when the gate was loaded first; otherwise register immediately.
BOUNDARY = dict(fields=("task", "recent", "proposed"), max_chars=100_000,
                raw_source_allowed=False,
                notes="Task title/spec/acceptance/scope/role; 20 recent calls; proposed tool/input.")
_jev = sys.modules.get(__package__ + ".jev")
if _jev is not None:
    _jev.declare_boundary("gate", **BOUNDARY)

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


def _target_path(tool_name, tool_input):
    ti = tool_input or {}
    if tool_name in {"Read", "Edit", "Write", "Grep", "Glob"}:
        for key in ("file_path", "path", "notebook_path"):
            if ti.get(key):
                return str(ti[key])
    if tool_name == "Bash":
        return str(ti.get("command") or "")[:80]
    return ""


def _target(tool_name, tool_input):
    from . import jev
    return jev.redact(_target_path(tool_name, tool_input))


def _input_hash(tool_name, tool_input):
    normalized = json.dumps(tool_input or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1((tool_name + normalized).encode("utf-8")).hexdigest()


def _session_state(session_id):
    path = STATE / "runs" / "jev" / f"session-{session_id}.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return path, {}
    except (OSError, ValueError):
        print(f"[jev-gate] corrupt session state {path}; starting over", file=sys.stderr)
        return path, {}
    if not isinstance(data, dict):
        print(f"[jev-gate] corrupt session state {path}; starting over", file=sys.stderr)
        return path, {}
    return path, data


def _target_mtime(target):
    try:
        return os.stat(target).st_mtime_ns
    except (OSError, TypeError):
        return None


def _call_record(tool_name, tool_input):
    path = _target_path(tool_name, tool_input)
    try:
        stat = os.stat(path)
        mtime, size = stat.st_mtime_ns, stat.st_size
    except (OSError, TypeError):
        mtime = size = None
    return {"name": tool_name, "input": tool_input or {}, "path": path,
            "mtime": mtime, "size": size}


def _history(session_id, new_call=None):
    """Read, and optionally append to, the compact deterministic call history."""
    from . import jev

    def op():
        path = STATE / "runs" / "jev" / f"history-{session_id or 'unknown'}.json"
        try:
            calls = json.loads(path.read_text())
            if not isinstance(calls, list):
                calls = []
        except (OSError, ValueError):
            calls = []
        previous = calls[-RECENT_LIMIT:]
        if new_call is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            calls.append(new_call)
            path.write_text(json.dumps(calls[-RECENT_LIMIT:], sort_keys=True))
        return previous

    return jev._with_state_lock(op)


def _record_call(session_id, tool_name, tool_input):
    """Return (hash, target, repeat, zero-based call index), updating the per-session index."""
    from . import jev

    def op():
        path, state = _session_state(session_id or "unknown")
        input_hash = _input_hash(tool_name, tool_input)
        target = _target(tool_name, tool_input)
        mtime = _target_mtime(_target_path(tool_name, tool_input))
        previous = state.get(input_hash) or {}
        repeat = input_hash in state and previous.get("mtime") == mtime
        call_index = sum(int(v.get("count", 0)) for v in state.values() if isinstance(v, dict))
        state[input_hash] = {"count": int(previous.get("count", 0)) + 1, "mtime": mtime}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as tmp:
                tmp.write(json.dumps(state, sort_keys=True))
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        return input_hash, target, repeat, call_index

    return jev._with_state_lock(op)


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
    ask_fn = jev.bind_site("gate")
    result = ask_fn(state, QUESTIONS)
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


def _log(task_id, session_id, tool_name, answers, mode, blocked, scored, latency_ms, startup_ms=0.0,
         tool_target="", input_hash="", repeat=False, sampled=False, economy=None,
         transcript_path="", call_index=None, role=None, task_class=None):
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
        "tool_target": tool_target, "input_hash": input_hash, "repeat": repeat, "sampled": sampled,
        "transcript_path": transcript_path, "call_index": call_index, "role": role,
        "task_class": task_class,
    }
    if economy:
        entry.update(read_kind=economy["kind"], tokens_estimate=economy["tokens_estimate"],
                     would_suppress=economy["would_suppress"])
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

    input_hash, target, repeat, call_index = _record_call(session_id, tool_name, tool_input)

    if is_protected(tool_name, tool_input):
        _log(task_id, session_id, tool_name, None, mode, blocked=False, scored=False, latency_ms=0.0,
             tool_target=target, input_hash=input_hash, repeat=repeat)
        return 0

    economy = None
    if tool_name in {"Read", "Grep", "Glob"}:
        from . import read_economy
        call = _call_record(tool_name, tool_input)
        history = _history(session_id)
        economy = read_economy.classify(call, history)
        _history(session_id, call)

    constraints = task.get("constraints") or {}
    task_class = task.get("task_class") or constraints.get("task_class")

    def finish(answers, *, scored=False, sampled=False, latency_ms=0.0, startup_ms=0.0,
               blocked=False):
        _log(task_id, session_id, tool_name, answers, mode, blocked=blocked, scored=scored,
             latency_ms=latency_ms, startup_ms=startup_ms, tool_target=target,
             input_hash=input_hash, repeat=repeat, sampled=sampled, economy=economy,
             transcript_path=transcript_path, call_index=call_index, role=task.get("role"),
             task_class=task_class)
        if economy:
            try:
                from . import decision_log
                jev_data = None
                if answers:
                    jev_data = {"needed_p": (answers.get("needed") or {}).get("p"),
                                "redundant_p": (answers.get("redundant") or {}).get("p")}
                decision_log.record(
                    kind="action_gate", subject=task_id, candidates=["allow", "suppress"],
                    hard_constraints=["guardrails.sh decides destructive", "block_repeats=false"],
                    deterministic={"read_kind": economy["kind"],
                                   "tokens_estimate": economy["tokens_estimate"],
                                   "would_suppress": economy["would_suppress"]},
                    jev=jev_data, selected="allow", reason=economy["kind"], mode=mode)
            except Exception:
                pass
        return 0

    # P18: Jev only judges ambiguous semantic equivalence. Deterministic cases are shadow-logged and allowed.
    prior_same_path = any(item.get("path") == _target_path(tool_name, tool_input) for item in history) if economy else False
    ambiguous = bool(economy and (economy["kind"] in ("repeated_read_changed", "narrower_search")
                                 or (economy["kind"] == "other" and prior_same_path)))
    if mode == "block" and cfg.get("block_repeats", False) and repeat:
        _log(task_id, session_id, tool_name, None, mode, blocked=True, scored=False, latency_ms=0.0,
             tool_target=target, input_hash=input_hash, repeat=True, economy=economy,
             transcript_path=transcript_path, call_index=call_index, role=task.get("role"),
             task_class=task_class)
        print("jev-gate: identical read already made this session; use the earlier result", file=sys.stderr)
        return 2

    if economy and not ambiguous:
        return finish(None)

    sample_rate = float(cfg.get("sample_rate", 0.1))
    sampled = mode != "sample" or (int(hashlib.sha1(f"{session_id}{call_index}".encode()).hexdigest(), 16) % 1000
                                    < sample_rate * 1000)
    if mode == "sample" and not sampled:
        return finish(None)

    from . import jev  # Network imports only after enabled, role and skip checks.
    recent = read_transcript(transcript_path)
    state = build_state(task, recent, tool_name, tool_input)

    started = time.monotonic()
    startup_ms = max(0.0, (time.time() - float(os.environ.get("ORCH_JEV_STARTED_AT", _IMPORTED_AT))) * 1000)
    answers = ask_jev(state)
    latency_ms = (time.monotonic() - started) * 1000

    if answers is None:
        return finish(None, sampled=sampled, latency_ms=latency_ms, startup_ms=startup_ms)

    blocked, reason = decide(answers, mode)
    if economy:
        finish(answers, scored=True, sampled=sampled, latency_ms=latency_ms,
               startup_ms=startup_ms, blocked=blocked)
    else:
        _log(task_id, session_id, tool_name, answers, mode, blocked=blocked, scored=True, latency_ms=latency_ms,
             startup_ms=startup_ms, tool_target=target, input_hash=input_hash, repeat=repeat, sampled=sampled,
             transcript_path=transcript_path, call_index=call_index, role=task.get("role"), task_class=task_class)
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
