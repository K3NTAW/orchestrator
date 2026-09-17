# Fetches a third-party page at the user's explicit decision (2026-09-17); the site's Terms of Use
# restrict automated access, so: manual or at most once per 20 h, identified User-Agent, single page,
# no browser. Page content is untrusted data.
import inspect
import json
import re
import typing
from datetime import datetime, timezone

from . import STATE

URL = "https://artificialanalysis.ai/models"
FILE = STATE / "bench.json"
MIN_INTERVAL_S = 20 * 3600
USER_AGENT = "orchestrator-bench/1 (+https://github.com/K3NTAW/orchestrator)"

DEFAULT_HINTS = {
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "claude-opus-5": "Claude Opus 5",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
    "claude-fable-5-1": "Claude Fable 5.1",
}

PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
CODING_RE = re.compile(r"coding", re.I)
SPEED_RE = re.compile(r"outputspeed", re.I)
PRICE_IN_RE = re.compile(r"price.*in", re.I)
PRICE_OUT_RE = re.compile(r"price.*out", re.I)

# Display names on the site carry an effort suffix, e.g. "GPT-6 Astra (max)" or
# "Claude Opus 5 (Adaptive Reasoning, Max Effort)". These words are the only ones norm()
# tolerates as leftovers outside the stripped parenthetical when comparing a name to a hint.
EFFORT_WORDS = {"max", "high", "xhigh", "low", "medium", "effort"}
# Preference order when several effort variants of the same model match one hint.
VARIANT_PREFERENCE = ["(max)", "max effort", "(xhigh)", "(high)"]


def _strip_first_paren(s):
    start = s.find("(")
    if start == -1:
        return s
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return s[:start] + s[i + 1:]
    return s[:start]  # unmatched "(": drop the rest, nothing sane to keep


def norm(name):
    """Lowercase, drop the first (...) span and collapse whitespace, so display-name effort
    suffixes ("(max)", "(xhigh)") don't break equality with a plain model hint."""
    s = _strip_first_paren(str(name or "").lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(".,;:!?-")


def _find_objects(text):
    """Spans of every balanced {...} substring in text, respecting quoted strings. Innermost spans close first."""
    spans, stack, in_str, escape = [], [], False, False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            spans.append((stack.pop(), i + 1))
    return spans


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _first_match(obj, pattern):
    """Recursively search a parsed JSON value for the first numeric value whose key matches pattern."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if pattern.search(k):
                    n = _num(v)
                    if n is not None:
                        return n
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


def _normalize(obj):
    name = obj.get("name") or obj.get("shortName") or obj.get("slug") or obj.get("id")
    if not name:
        return None
    return {
        "name": name,
        "slug": obj.get("slug") or obj.get("id"),
        "intelligence": _num(obj.get("intelligenceIndex")),
        "coding": _first_match(obj, CODING_RE),
        "speed_tps": _first_match(obj, SPEED_RE),
        "price_in": _first_match(obj, PRICE_IN_RE),
        "price_out": _first_match(obj, PRICE_OUT_RE),
        "raw_keys": sorted(obj.keys()),
    }


def parse_chunks(html):
    """Every `self.__next_f.push([1,"..."])` RSC chunk, JS-unescaped, scanned for JSON objects that carry an
    "intelligenceIndex" key. Never raises on one bad chunk -- malformed pushes/objects are skipped."""
    records = []
    for m in PUSH_RE.finditer(html):
        try:
            text = json.loads('"' + m.group(1) + '"')
        except Exception:
            continue
        accepted = []
        for start, end in _find_objects(text):
            span = text[start:end]
            if "intelligenceIndex" not in span:
                continue
            if any(a[0] <= start and end <= a[1] for a in accepted):
                continue  # nested inside an already-accepted (smaller) span for the same model
            try:
                obj = json.loads(span)
            except Exception:
                continue
            if not isinstance(obj, dict) or "intelligenceIndex" not in obj:
                continue
            rec = _normalize(obj)
            if rec is None:
                continue
            accepted.append((start, end))
            records.append(rec)
    return records


def _prefer_variant(raw_names):
    for pat in VARIANT_PREFERENCE:
        for name in raw_names:
            if pat in name.lower():
                return name
    return raw_names[0]


def _startswith_candidate(name_norm, hint_norm):
    """name_norm's only extra content past hint_norm is effort words (an un-parenthesized
    suffix survives norm() only if it wasn't wrapped in "(...)"; guard so "gpt-5.5 pro" never
    matches hint "gpt-5.5" -- "pro" isn't an effort word)."""
    if not hint_norm or not name_norm.startswith(hint_norm + " "):
        return False
    remainder = name_norm[len(hint_norm) + 1:].split()
    return bool(remainder) and all(w in EFFORT_WORDS for w in remainder)


def _token_candidate(name_norm, hint_tokens):
    """Looser fallback: dash/space-insensitive token containment, e.g. hint "gpt 5.6 luna"
    against name "gpt-5.6 luna". Extra tokens beyond the hint must all be effort words or
    non-alphabetic, else a longer name ("gpt-5.5 pro") would wrongly satisfy a shorter hint."""
    if not hint_tokens:
        return False
    name_tokens = name_norm.replace("-", " ").split()
    if not all(t in name_tokens for t in hint_tokens):
        return False
    extra = [t for t in name_tokens if t not in hint_tokens]
    return all((not t.isalpha()) or t in EFFORT_WORDS for t in extra)


def match(records, executors_models):
    """Match a display hint against record names, tolerant of effort suffixes such as
    "(max)" or "(xhigh)". For each hint: exact match on norm(name), else norm(name) with only
    effort words trailing the hint, else (if nothing matched yet) a dash/space-insensitive
    token-containment fallback. When several raw names satisfy a hint, prefer the "(max)"
    variant, then "max effort", then "(xhigh)", then "(high)", else the first found. The
    chosen record is annotated with "display_name" (its raw name) and "variants" (every raw
    name that matched). Unmatched -> None."""
    out = {}
    for model_id, hint in executors_models.items():
        hint_norm = norm(hint)
        hint_tokens = hint_norm.replace("-", " ").split()
        candidates = []
        for rec in records:
            name_norm = norm(rec.get("name"))
            if name_norm == hint_norm or _startswith_candidate(name_norm, hint_norm):
                candidates.append(rec)
        if not candidates:
            for rec in records:
                if _token_candidate(norm(rec.get("name")), hint_tokens):
                    candidates.append(rec)
        if not candidates:
            out[model_id] = None
            continue
        raw_names = [rec.get("name") or "" for rec in candidates]
        chosen_name = _prefer_variant(raw_names)
        chosen = next(rec for rec in candidates if (rec.get("name") or "") == chosen_name)
        result = dict(chosen)
        result["display_name"] = chosen_name
        result["variants"] = raw_names
        out[model_id] = result
    return out


def _supported_params(func):
    """Real keyword names Fetcher.get accepts, read off its Unpack[TypedDict] annotation (PEP 692) rather
    than guessed -- inspect.signature alone only shows a bare **kwargs for this call shape."""
    sig = inspect.signature(func)
    kwargs_param = sig.parameters.get("kwargs")
    if kwargs_param is None:
        return set(sig.parameters)
    args = typing.get_args(kwargs_param.annotation)
    td = args[0] if args else None
    return set(getattr(td, "__annotations__", {}))


def fetch_html(url=URL, timeout=30):
    """Identified, plain fetch: no TLS/JA3 impersonation, no synthetic browser headers or fake
    referer (decision 2026-09-17). Returns (status_code, html)."""
    from scrapling.fetchers import Fetcher

    supported = _supported_params(Fetcher.get)
    kwargs = {}
    if "timeout" in supported:
        kwargs["timeout"] = timeout
    if "headers" in supported:
        kwargs["headers"] = {
            "User-Agent": "orchestrator-bench/1 (+https://github.com/K3NTAW/orchestrator)",
            "Accept": "text/html",
        }
    if "impersonate" in supported:
        kwargs["impersonate"] = None
    if "stealthy_headers" in supported:
        kwargs.update(stealthy_headers=False)
    page = Fetcher.get(url, **kwargs)
    html = page.body if hasattr(page, "body") else page.html_content
    html = html.decode("utf-8", "replace") if isinstance(html, bytes) else str(html)
    return page.status, html


def load():
    return json.loads(FILE.read_text()) if FILE.exists() else {}


def fetch(force=False, by="orchestrator"):
    existing = load()
    fetched_at = existing.get("fetched_at")
    if not force and fetched_at:
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)).total_seconds() / 3600
        if age_h * 3600 < MIN_INTERVAL_S:
            return {"skipped": f"fetched {round(age_h)} h ago; use --force"}

    import scrapling

    status, html = fetch_html()
    records = parse_chunks(html)
    if status != 200 or len(html) < 10 * 1024 or not records:
        return {"error": f"fetch failed: status {status}, {len(html)} bytes, {len(records)} records"}

    matched = match(records, DEFAULT_HINTS)
    used_names = {(r.get("name") or "").lower() for r in matched.values() if r}
    seen, unmatched_names = set(), []
    for r in records:
        name = r.get("name")
        if name and name.lower() not in used_names and name not in seen:
            seen.add(name)
            unmatched_names.append(name)
        if len(unmatched_names) >= 30:
            break

    payload = {
        "source": URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetched_by": by,
        "fetcher": f"scrapling {scrapling.__version__}",
        "http_status": status,
        "provenance": "web:artificialanalysis.ai (untrusted data)",
        "request": {"user_agent": USER_AGENT, "impersonate": False, "stealth_headers": False},
        "models": {mid: rec for mid, rec in matched.items()},
        "unmatched_names": unmatched_names,
    }
    STATE.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(payload, indent=2))
    return payload


def set_model(model_id, by, **metrics):
    data = load()
    data.setdefault("models", {})
    rec = {**(data["models"].get(model_id) or {}), **metrics}
    rec["manual"] = True
    rec["set_by"] = by
    rec["set_at"] = datetime.now(timezone.utc).isoformat()
    data["models"][model_id] = rec
    STATE.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, indent=2))
    return rec
