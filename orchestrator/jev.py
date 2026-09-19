"""Jev (TypeSafe AI): typed questions about a state answered with calibrated probabilities instead of free
text. `POST https://api.typesafe.ai/v1/systemone`. Off by default (pool.toml [jev].enabled); every failure mode
(disabled, no key, timeout, HTTP error, bad JSON, budget exhausted) makes ask() return None rather than raise,
so a caller can always treat Jev as an optional signal. Stdlib only (urllib) -- no new dependency for one
optional HTTP call."""
import json, re, sys, time, urllib.error, urllib.request
from datetime import date
from . import STATE, spawn
from . import pool as P

URL = "https://api.typesafe.ai/v1/systemone"
STATE_FILE = STATE / "jev_state.json"
RUNS_DIR = STATE / "runs"

DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_DAILY_BUDGET_TOKENS = 20_000_000
DEFAULT_MAX_STATE_CHARS = 100_000

RETRY_STATUS = (429, 529)
RETRY_BACKOFF_S = 0.5

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
    }


def _today():
    return date.today().isoformat()


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _day_tokens_used():
    state = _load_state()
    return state.get("tokens", 0) if state.get("day") == _today() else 0


def _add_day_tokens(input_tokens):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    today = _today()
    state = _load_state()
    if state.get("day") != today:
        state = {"day": today, "tokens": 0}
    state["tokens"] = state.get("tokens", 0) + input_tokens
    STATE_FILE.write_text(json.dumps(state))


def _log_usage(caller, input_tokens, model, latency_ms, ok):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "caller": caller, "input_tokens": input_tokens, "model": model,
              "latency_ms": latency_ms, "ok": ok}
    with open(RUNS_DIR / f"jev-{_today()}.jsonl", "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _api_key():
    return spawn.secrets_for_role("jev").get("TYPESAFE_API_KEY")


def ask(state, questions, *, model=None, timeout_s=5.0):
    """POST typed `questions` about `state` to Jev, return the parsed {"answers", "usage"} dict, or None on any
    failure (fail-open by design: disabled, no key, timeout, HTTP error, invalid JSON, budget exhausted).
    One retry with a 0.5s backoff on 429/529; every other failure returns None immediately."""
    caller = sys._getframe(1).f_code.co_name
    cfg = _cfg()
    if not cfg["enabled"]:
        return None
    api_key = _api_key()
    if not api_key:
        return None
    if _day_tokens_used() > cfg["daily_budget_tokens"]:
        return None

    model = model or cfg["model"]
    state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    state_text = redact(state_text[:cfg["max_state_chars"]])

    payload = json.dumps({"state": state_text, "model": model, "questions": questions}).encode()
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
            _log_usage(caller, 0, model, (time.monotonic() - started) * 1000, False)
            return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            _log_usage(caller, 0, model, (time.monotonic() - started) * 1000, False)
            return None
        else:
            input_tokens = (body.get("usage") or {}).get("input_tokens", 0)
            _log_usage(caller, input_tokens, model, (time.monotonic() - started) * 1000, True)
            _add_day_tokens(input_tokens)
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
