"""jev-compactor-style relevance ranking: score each candidate item against a goal with Jev, drop the low ones,
never summarize. One noul question per item ('Is this entry relevant to the goal below?', item text as the
question's criteria, goal text as the shared state), batched up to `batch` questions per ask() call so a large
candidate list stays under Jev's per-request state/question budget. Fail-open: the moment jev.ask() returns None
(disabled, no key, budget exhausted, network error -- see jev.ask), ranking stops and every item is returned in
its original order with p_relevant=None, so a caller can always render the unranked list instead of an empty one.
"""
from . import jev

DEFAULT_THRESHOLD = 0.35
DEFAULT_BATCH = 40

INSTRUCTIONS = "Is this entry relevant to the goal below?"


def rank(items: list[dict], goal_text: str, *, threshold: float = DEFAULT_THRESHOLD, batch: int = DEFAULT_BATCH) -> list[dict]:
    """Score each {id, text} item for relevance to `goal_text`, attach p_relevant, and return items with
    p_relevant >= threshold sorted by p_relevant desc. On jev.ask() returning None, returns all `items`
    unchanged (original order) with p_relevant=None on each -- never raises, never drops anything itself."""
    if not items:
        return []

    scored = []
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        questions = {item["id"]: {"type": "noul", "instructions": INSTRUCTIONS, "criteria": item["text"]}
                     for item in chunk}
        result = jev.ask(goal_text, questions)
        if result is None:
            return [{**item, "p_relevant": None} for item in items]
        answers = result.get("answers") or {}
        for item in chunk:
            answer = answers.get(item["id"]) or {}
            scored.append({**item, "p_relevant": answer.get("noul")})

    kept = [item for item in scored if item["p_relevant"] is not None and item["p_relevant"] >= threshold]
    kept.sort(key=lambda item: item["p_relevant"], reverse=True)
    return kept
