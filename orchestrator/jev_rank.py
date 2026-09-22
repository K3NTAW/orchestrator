"""jev-compactor-style relevance ranking: score each candidate item against a goal with Jev, drop the low ones,
never summarize. One noul question per item ('Is this entry relevant to the goal below?', item text as the
question's criteria, goal text as the shared state), batched up to `batch` questions per ask() call so a large
candidate list stays under Jev's per-request state/question budget. Fail-open: the moment jev.ask() returns None
(disabled, no key, budget exhausted, network error -- see jev.ask), ranking stops and every item is returned in
its original order with p_relevant=None, so a caller can always render the unranked list instead of an empty one.
Same fail-open treatment applies per item: a missing answer, or a noul value that isn't a finite number in
[0, 1] once coerced, keeps that item's p_relevant at None instead of dropping it -- only a numeric score below
threshold is ever dropped. rank() itself never raises: a malformed response is treated like ask() returning
None.
"""
import math
import sys

from . import jev

jev.declare_boundary('rank', fields=(),
                     max_chars=jev.DEFAULT_MAX_STATE_CHARS, raw_source_allowed=False,
                     notes='String state: redacted goal_text; question criteria: redacted item texts; default batch 40.')

DEFAULT_THRESHOLD = 0.35
DEFAULT_BATCH = 40

INSTRUCTIONS = "Is this entry relevant to the goal below?"


def _coerce_p(value):
    """float() on an int/float/numeric string, then require a finite result in [0, 1]; anything else -> None."""
    if not isinstance(value, (int, float, str)):
        return None
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or not (0.0 <= p <= 1.0):
        return None
    return p


def _sort_key(item):
    p = item["p_relevant"]
    return (1, 0.0) if p is None else (0, -p)  # None sorts after every numeric score, never compared to one


def rank(items: list[dict], goal_text: str, *, threshold: float = DEFAULT_THRESHOLD, batch: int = DEFAULT_BATCH) -> list[dict]:
    """Score each {id, text} item for relevance to `goal_text`, attach p_relevant, and return items with
    p_relevant >= threshold (plus every item whose p_relevant is None) sorted by p_relevant desc, None last.
    On jev.ask() returning None or raising, or on any error parsing its response, returns all `items` unchanged (original
    order) with p_relevant=None on each -- never raises, never drops anything itself."""
    if not items:
        return []

    scored = []
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        questions = {item["id"]: {"type": "noul", "instructions": INSTRUCTIONS, "criteria": jev.redact(item["text"])}
                     for item in chunk}
        try:
            ask_fn = jev.bind_site("rank")
            result = ask_fn(jev.redact(goal_text), questions)
            if result is None:
                return [{**item, "p_relevant": None} for item in items]
            answers = result.get("answers") or {}
            for item in chunk:
                answer = answers.get(item["id"]) or {}
                scored.append({**item, "p_relevant": _coerce_p(answer.get("noul"))})
        except Exception:
            print("jev_rank: ranking failed; keeping unranked items", file=sys.stderr)
            return [{**item, "p_relevant": None} for item in items]

    kept = [item for item in scored if item["p_relevant"] is None or item["p_relevant"] >= threshold]
    kept.sort(key=_sort_key)
    return kept
