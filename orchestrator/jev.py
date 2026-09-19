"""Jev (TypeSafe AI): typed questions about a state answered with calibrated probabilities instead of free
text. `POST https://api.typesafe.ai/v1/systemone`. Off by default (pool.toml [jev].enabled); every failure mode
(disabled, no key, timeout, HTTP error, bad JSON, budget exhausted) makes ask() return None rather than raise,
so a caller can always treat Jev as an optional signal. Stdlib only (urllib) -- no new dependency for one
optional HTTP call.

Usage lines go to .orchestrator/runs/jev/<date>.jsonl -- a subdirectory of runs/, not runs/<date>.jsonl itself,
so they never collide with worker run lines that cli.cost() and scorecard.build()/by_task()/by_goal() glob
non-recursively (root/runs/*.jsonl) and key on a "role" field jev lines don't carry. The E3 tool-call gate
writes its own log to .orchestrator/runs/jev/gate.jsonl in that same subdirectory, for the same reason.
"""
import fcntl, json, os, re, sys, tempfile, time, urllib.error, urllib.request
from datetime import date
from . import STATE, bus, spawn
from . import pool as P

URL = "https://api.typesafe.ai/v1/systemone"
STATE_FILE = STATE / "jev_state.json"
STATE_LOCK_FILE = STATE / "jev_state.lock"
RUNS_DIR = STATE / "runs" / "jev"

DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_DAILY_BUDGET_TOKENS = 20_000_000
DEFAULT_MAX_STATE_CHARS = 100_000

RETRY_STATUS = (429, 529)
RETRY_BACKOFF_S = 0.5

API_KEY_TTL_S = 600  # resolve TYPESAFE_API_KEY at most once per this window; never log the value

# Order matters: the specific prefixes first, then KEY=VALUE (so the value half is caught even if it's short),
# then the generic long hex/base64 runs last so they don't fight the more specific patterns above them.
_TOKEN_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"\b(\w*(?:TOKEN|SECRET|PASSWORD|KEY)\w*)\s*=\s*\S+", re.I),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"),
]

# In-memory fallback tally for the current process: _add_day_tokens keeps this in sync with every successful
# write so a corrupt on-disk state file degrades to "whatever this process last knew" instead of silently
# resetting the count to 0 (which would let a caller blow through the daily budget after one bad write).
_tally_cache = {"day": None, "tokens": 0}

_api_key_cache = {"value": None, "resolved_at": None}


def redact(text):
    """Strip token-like substrings from `text`: sk-..., ghp_..., Bearer ..., KEY=VALUE (KEY containing
    TOKEN|SECRET|PASSWORD|KEY), and 32+ char hex/base64 runs. Public so callers other than ask() can hygiene
    their own strings before they'd otherwise be logged or sent anywhere."""
    if not text:
        return text
    out = text
    for i, pat in enumerate(_TOKEN_PATTERNS):
        out = pat.sub(r"\1=[REDACTED]" if i == 3 else "[REDACTED]", out)
    return out


def _cfg():
    try:
        raw = P.config()
    except FileNotFoundError:
        raw = {}
    jev = raw.get("jev") or {}
    return {
        "enabled": jev.get("enabled", False),
        "model": jev.get("model", DEFAULT_MODEL),
        "timeout_s": jev.get("timeout_s", DEFAULT_TIMEOUT_S),
        "daily_budget_tokens": jev.get("daily_budget_tokens", DEFAULT_DAILY_BUDGET_TOKENS),
        "max_state_chars": jev.get("max_state_chars", DEFAULT_MAX_STATE_CHARS),
        "votes": jev.get("votes", 1),
    }


def _today():
    return date.today().isoformat()


def _with_state_lock(fn):
    """Serialize read-modify-write access to jev_state.json under an flock on a sidecar lock file (not the
    state file itself) -- same critical-section discipline as pool._save_planner_account, but locking a stable
    path that's never replaced, since _atomic_write_state below swaps jev_state.json's inode out from under
    any lock that might be held on it directly."""
    STATE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_LOCK_FILE, "a+") as lockfh:
        fcntl.flock(lockfh, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lockfh, fcntl.LOCK_UN)


def _atomic_write_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(STATE_FILE.parent), prefix=".jev_state.json.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(state))
        os.replace(tmp_name, STATE_FILE)
    except Exception:
        os.unlink(tmp_name)
        raise


def _read_state_locked():
    """Read+parse jev_state.json. Returns None (not {}) on a corrupt file so callers can tell "empty/missing"
    apart from "unparseable" and fall back to the in-memory tally instead of treating corruption as a 0."""
    try:
        raw = STATE_FILE.read_text()
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"[jev] {STATE_FILE} is corrupt; keeping in-process tally instead of resetting to 0", file=sys.stderr)
        return None


def _day_tokens_used():
    def op():
        today = _today()
        state = _read_state_locked()
        if state is None:
            return _tally_cache["tokens"] if _tally_cache["day"] == today else 0
        return state.get("tokens", 0) if state.get("day") == today else 0
    return _with_state_lock(op)


def _add_day_tokens(input_tokens):
    def op():
        today = _today()
        state = _read_state_locked()
        if state is None:
            base = _tally_cache["tokens"] if _tally_cache["day"] == today else 0
            state = {"day": today, "tokens": base}
        elif state.get("day") != today:
            state = {"day": today, "tokens": 0}
        state["tokens"] = state.get("tokens", 0) + input_tokens
        _atomic_write_state(state)
        _tally_cache["day"] = today
        _tally_cache["tokens"] = state["tokens"]
    _with_state_lock(op)


def _log_usage(caller, input_tokens, model, latency_ms, ok, votes=1, task=None):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    task = task or os.environ.get("ORCH_TASK_ID")
    goal_id = None
    if task:
        try:
            row = bus.get(task)
            goal_id = row.get("parent") or task
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            pass
    entry = {"ts": time.time(), "caller": caller, "input_tokens": input_tokens, "model": model,
              "latency_ms": latency_ms, "ok": ok, "votes": votes, "task": task, "goal_id": goal_id}
    with open(RUNS_DIR / f"{_today()}.jsonl", "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _api_key():
    """TYPESAFE_API_KEY, resolved at most once per API_KEY_TTL_S: spawn.secrets_for_role reads pool.toml +
    the environment on every call, which is wasted work on Jev's hot path (ask() calls this every time).
    Never logged -- only the resolved value is cached, never printed or included in _log_usage's entry."""
    now = time.monotonic()
    resolved_at = _api_key_cache["resolved_at"]
    if resolved_at is not None and now - resolved_at < API_KEY_TTL_S:
        return _api_key_cache["value"]
    value = spawn.secrets_for_role("jev").get("TYPESAFE_API_KEY")
    _api_key_cache["value"] = value
    _api_key_cache["resolved_at"] = now
    return value


def ask(state, questions, *, model=None, timeout_s=None, task=None):
    """POST typed `questions` about `state` to Jev, return the parsed {"answers", "usage"} dict, or None on any
    failure (fail-open by design: disabled, no key, timeout, HTTP error, invalid JSON, budget exhausted).
    One retry with a 0.5s backoff on 429/529; every other failure returns None immediately. timeout_s=None
    (the default) uses pool.toml [jev].timeout_s rather than hard-coding a value in the signature."""
    caller = sys._getframe(1).f_code.co_name
    cfg = _cfg()
    if not cfg["enabled"]:
        return None
    api_key = _api_key()
    if not api_key:
        return None
    if _day_tokens_used() >= cfg["daily_budget_tokens"]:
        return None

    model = model or cfg["model"]
    timeout_s = cfg["timeout_s"] if timeout_s is None else timeout_s
    state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    state_text = redact(state_text)[:cfg["max_state_chars"]]

    votes = cfg.get("votes", 1)
    if not isinstance(votes, int) or isinstance(votes, bool) or votes < 1:
        return None
    sent_questions = questions if votes == 1 else {
        f"{key}_{i}": question for key, question in questions.items() for i in range(1, votes + 1)
    }
    payload = json.dumps({"state": state_text, "model": model, "questions": sent_questions}).encode()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    started = time.monotonic()
    for attempt in range(2):
        try:
            req = urllib.request.Request(URL, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in RETRY_STATUS and attempt == 0:
                time.sleep(RETRY_BACKOFF_S)
                continue
            _log_usage(caller, 0, model, (time.monotonic() - started) * 1000, False, votes, task)
            return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            _log_usage(caller, 0, model, (time.monotonic() - started) * 1000, False, votes, task)
            return None
        else:
            input_tokens = (body.get("usage") or {}).get("input_tokens", 0)
            _log_usage(caller, input_tokens, model, (time.monotonic() - started) * 1000, True, votes, task)
            _add_day_tokens(input_tokens)
            try:
                answers = body.get("answers") or {}
                if votes > 1:
                    averaged = {}
                    for key, question in questions.items():
                        rows = [answers[f"{key}_{i}"] for i in range(1, votes + 1)]
                        answer = dict(rows[0])
                        if question["type"] == "noul":
                            answer["noul"] = sum(row["noul"] for row in rows) / votes
                        elif question["type"] == "choice":
                            answer["probabilities"] = {
                                option: sum(row["probabilities"][option] for row in rows) / votes
                                for option in question["criteria"]
                            }
                            answer["choice"] = max(answer["probabilities"], key=answer["probabilities"].get)
                        elif question["type"] == "score":
                            answer["score"] = sum(row["score"] for row in rows) / votes
                        confidences = [row.get("confidence") for row in rows]
                        answer["confidence"] = (None if None in confidences else sum(confidences) / votes)
                        averaged[key] = answer
                    body["answers"] = averaged
                else:
                    for answer in answers.values():
                        answer.setdefault("confidence", None)
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
            return body
    return None


def noul(state, instructions, criteria=None):
    """P(true) for a yes/no question, or None if Jev is unavailable/disabled."""
    q = {"type": "noul", "instructions": instructions}
    if criteria is not None:
        q["criteria"] = criteria
    result = ask(state, {"q": q})
    try:
        return result["answers"]["q"]["noul"]
    except (KeyError, TypeError):
        return None


def choice(state, instructions, options: dict):
    """(choice, probabilities, confidence) among `options`, or None if Jev is unavailable/disabled."""
    result = ask(state, {"q": {"type": "choice", "instructions": instructions, "criteria": options}})
    try:
        a = result["answers"]["q"]
        return a["choice"], a["probabilities"], a["confidence"]
    except (KeyError, TypeError):
        return None


def score(state, instructions, levels: list):
    """(score, confidence) against `levels`, or None if Jev is unavailable/disabled."""
    result = ask(state, {"q": {"type": "score", "instructions": instructions, "criteria": levels}})
    try:
        a = result["answers"]["q"]
        return a["score"], a["confidence"]
    except (KeyError, TypeError):
        return None
