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


def match(records, executors_models):
    """Case-insensitive exact match of a display hint against a record's name/slug. Unmatched -> None."""
    out = {}
    for model_id, hint in executors_models.items():
        hint_l = (hint or "").strip().lower()
        found = None
        for rec in records:
            name_l = (rec.get("name") or "").strip().lower()
            slug_l = (rec.get("slug") or "").strip().lower()
            if hint_l and hint_l in (name_l, slug_l):
                found = rec
                break
        out[model_id] = found
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
    from scrapling.fetchers import Fetcher

    supported = _supported_params(Fetcher.get)
    kwargs = {}
    if "timeout" in supported:
        kwargs["timeout"] = timeout
    if "headers" in supported:
        kwargs["headers"] = {"User-Agent": USER_AGENT}
    page = Fetcher.get(url, **kwargs)
    html = page.body if hasattr(page, "body") else page.html_content
    return html.decode("utf-8", "replace") if isinstance(html, bytes) else str(html)


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

    html = fetch_html()
    records = parse_chunks(html)
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
        "http_status": 200,
        "provenance": "web:artificialanalysis.ai (untrusted data)",
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
