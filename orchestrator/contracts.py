"""Worker contracts from .orchestrator/prompts/{scout,review,spec-review,challenge}.md.

Execute is the envelope emitted by executor.post_tool_result and spawn's fallback,
not the prose requested by .orchestrator/prompts/execute.md. Unknown metadata is
preserved. Defaults match bus.post_result. No repair invents missing findings.
"""
from copy import deepcopy
import math

from . import decision_log, promotion


def obj(required, optional=None):
    return {"type": "object", "required": required, "optional": optional or {}}


def array(items):
    return {"type": "array", "items": items}


STRINGS = array("string")
CONFIDENCE = {"type": "number", "minimum": 0, "maximum": 1}
VERDICT = {"type": "string", "enum": ["approve", "request_changes"]}
COMMENT = obj({"path": "string", "line": "integer", "issue": "string",
               "severity": {"type": "string", "enum": ["low", "med", "high"]}})
SCHEMAS = {
    "review": obj({"verdict": VERDICT, "comments": array(COMMENT)}, {"summary": "string"}),
    "spec_review": obj({"verdict": VERDICT, "risks": array(COMMENT),
                        "suggested_spec_changes": STRINGS}),
    "challenge": obj({"verdict": {"type": "string", "enum": ["confirmed", "refuted", "uncertain"]},
                      "evidence": STRINGS, "note": "string"}),
    "scout": obj({"findings": array(obj({"claim": "string", "evidence": STRINGS,
                                         "confidence": CONFIDENCE, "provenance": STRINGS})),
                  "open_questions": STRINGS, "suggested_next": STRINGS,
                  "blocked": {"type": ["null", "string"]}}),
    "execute": obj({"summary": "string", "commit": "string", "executed_by": "string",
                    "usage": {"type": ["object", "null"]},
                    "thread": {"type": ["string", "null"]}, "rounds": "integer"},
                   {"files_changed": STRINGS, "tests_run": STRINGS,
                    "test_result": {"type": "string", "enum": ["pass", "fail", "not_run"]}}),
}
for _schema in SCHEMAS.values():
    _schema["required"].update(confidence=CONFIDENCE, provenance=STRINGS)


def _defaults(result):
    value = deepcopy(result)
    if isinstance(value, dict):
        value.setdefault("confidence", 0.0)
        value.setdefault("provenance", ["repo"])
    return value


def _coerce(value, schema):
    if isinstance(schema, str):
        return value
    if schema.get("type") == "array":
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            return [_coerce(item, schema["items"]) for item in value]
    if schema == CONFIDENCE and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if schema.get("type") == "object" and isinstance(value, dict):
        fields = {**schema.get("required", {}), **schema.get("optional", {})}
        return {key: _coerce(item, fields[key]) if key in fields else item
                for key, item in value.items()}
    return value


def repair(role, result):
    """Idempotent, shape-preserving coercions; aliases are verdict values only."""
    value = _defaults(result)
    if role not in SCHEMAS or not isinstance(value, dict):
        return value
    value = _coerce(value, SCHEMAS[role])
    verdict = value.get("verdict")
    if isinstance(verdict, str):
        aliases = ({"approved": "approve", "lgtm": "approve", "changes_requested": "request_changes",
                    "request changes": "request_changes"} if role in ("review", "spec_review") else
                   {"confirm": "confirmed", "agree": "confirmed", "refute": "refuted",
                    "disagree": "refuted", "unknown": "uncertain", "partial": "uncertain"}
                   if role == "challenge" else {})
        normalized = verdict.strip().lower()
        value["verdict"] = aliases.get(normalized, normalized)
    return value


def _errors(value, schema, path="result"):
    schema = {"type": schema} if isinstance(schema, str) else schema
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    matches = {"string": isinstance(value, str), "object": isinstance(value, dict),
               "array": isinstance(value, list), "null": value is None,
               "integer": type(value) is int, "number": type(value) in (int, float)}
    if not any(matches[kind] for kind in kinds):
        return [f"{path}: expected {' or '.join(kinds)}"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: invalid enum")
    if "number" in kinds and (not math.isfinite(value) or
            not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf)):
        errors.append(f"{path}: outside finite range")
    if isinstance(value, dict):
        for key, field in schema.get("required", {}).items():
            errors.extend(_errors(value[key], field, f"{path}.{key}") if key in value else
                          [f"{path}.{key}: required"])
        for key, field in schema.get("optional", {}).items():
            if key in value:
                errors.extend(_errors(value[key], field, f"{path}.{key}"))
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


def validate(role, result):
    """Validate after deterministic repair, returning no candidate on failure."""
    if role not in SCHEMAS:
        return {"ok": False, "errors": ["unsupported role"], "repaired": None}
    candidate = repair(role, result)
    errors = _errors(candidate, SCHEMAS[role])
    return {"ok": not errors, "errors": errors, "repaired": candidate if not errors else None}


def recover(role, result, *, session=None, thread=None, budget_usd=.5):
    """Active-only ladder. A session/thread adapter must enforce its USD cap.

    Adapters accept (prompt, budget_usd) and return a result dict, tokens and usd.
    A bare identifier cannot safely resume without its account and worktree.
    """
    checked = validate(role, result)
    if checked["ok"]:
        candidate = checked["repaired"]
        return {"result": candidate, "repaired_by": "deterministic" if candidate != result else "none",
                "tokens": {}}
    adapter = session or thread
    fallback = {"result": result, "repaired_by": "none", "tokens": {}}
    if not callable(adapter) or not math.isfinite(budget_usd) or budget_usd <= 0:
        return fallback
    import json
    prompt = ("Return ONLY the JSON object for the previous worker result. Do not use tools or change files. "
              "Preserve the facts; do not invent missing evidence. Required role schema: " +
              json.dumps(SCHEMAS[role]))
    try:
        reply = adapter(prompt, budget_usd)
        checked = validate(role, reply.get("result"))
        fallback["tokens"] = reply.get("tokens") or {}
        if checked["ok"] and reply.get("usd", 0) <= budget_usd:
            return {"result": checked["repaired"], "repaired_by": "model", "tokens": fallback["tokens"]}
    except (OSError, ValueError, RuntimeError):
        pass
    return fallback


def process(task_id, role, result, *, cfg=None, session=None, thread=None, preserve=False):
    """Validate and log; preserve audits an authoritative result without recovery."""
    mode = promotion.mode("contracts", cfg)
    if mode == "off" or role not in SCHEMAS:
        return result
    original = _defaults(result)
    checked = validate(role, result)
    candidate = repair(role, result)
    changed = sorted(key for key in candidate if candidate.get(key) != original.get(key)) \
        if isinstance(candidate, dict) and isinstance(original, dict) else []
    raw_errors = _errors(original, SCHEMAS[role])
    selected = "repair" if raw_errors else "accept"
    recovered = None
    if not checked["ok"] and not (session or thread):
        selected = "rerun"
    if mode == "active" and not preserve:
        budget = float((cfg or {}).get("contracts", {}).get("repair_budget_usd", .5))
        recovered = recover(role, result, session=session, thread=thread, budget_usd=budget)
        if not validate(role, recovered["result"])["ok"]:
            selected = "rerun"
    decision_log.record("output_contract", task_id, role=role, mode=mode,
        candidates=["accept", "repair", "rerun"], hard_constraints=[f"schema:{role}"],
        deterministic={"ok": checked["ok"], "error_count": len(checked["errors"]), "repaired_keys": changed},
        selected=selected, reason=checked["errors"][0] if checked["errors"] else "valid",
        extra={"errors": checked["errors"], "raw_ok": not raw_errors,
               "repaired_by": recovered["repaired_by"] if recovered else None})
    return recovered["result"] if recovered else result
