"""Pure decision routing; callers gather state and perform every side effect.

``ctx['routes']`` is the optional [planner.routes] configuration. Tiers are
aliases: the caller resolves ``fable`` through [models].planner. No model IDs
are returned here. Omitted policy settings use the documented defaults.

State fields have no optimistic defaults. ``infra_failure_kind`` must be None
or a failure kind. Held points need failing_ids (list), in_scope_review_comments
(bool), auto_fix_rounds_used/auto_fix_rounds (lineage counts), failure_signature
(string or None), signature_repeated (bool), spec_review_request_changes
(count), blocked_scouts (list), and touches_auth/migrations/interfaces/
data_deletion (bools). scouts_done needs goal_complexity, scout_count,
all_scouts_posted, blocked_scouts, security_trigger and semantic_trigger.
Closable points need all_children_merged (bool) and gate_state ('green'/'red').
Explicit None is unknown except for infra_failure_kind and failure_signature.

Optional audit fields: task_class, hold_reason, spec_review_rounds, reruns,
premium_launches, and any of the state fields above. They describe work already
done, never work proposed by this router. Inputs are never mutated.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    name: str
    reason: str
    evidence: list[str]
    cheaper_steps: list[str]
    tier: str | None


def route(point: dict, ctx: dict) -> Route:
    """Choose a route deterministically, failing closed on unknown risk."""
    config = ctx.get("routes", {})
    evidence = []
    cheaper_steps = []
    for field in ("goal_id", "task_id", "payload_key"):
        if point.get(field) is not None:
            evidence.append(f"{field}:{point[field]}")
    for task_id in point.get("task_ids", []):
        evidence.append(f"task_id:{task_id}")
    for field in ("task_class", "hold_reason", "failure_signature",
                  "signature_repeated", "spec_review_request_changes",
                  "all_children_merged", "gate_state", "goal_complexity",
                  "all_scouts_posted", "security_trigger", "semantic_trigger",
                  "in_scope_review_comments", "auto_fix_rounds",
                  "touches_auth", "touches_migrations", "touches_interfaces",
                  "touches_data_deletion", "infra_failure_kind"):
        if field in ctx:
            evidence.append(f"{field}:{ctx[field]}")
    for field in ("failing_ids", "blocked_scouts"):
        for value in ctx.get(field) or []:
            evidence.append(f"{field}:{value}")
    for field in ("auto_fix_rounds_used", "scout_count", "spec_review_rounds", "reruns"):
        if field in ctx and ctx[field] is not None:
            cheaper_steps.append(f"{field}:{ctx[field]}")

    def result(name: str, reason: str) -> Route:
        facts = evidence.copy()
        tier = None
        if name == "escalate":
            tier = config.get("escalate_tier", "fable")
            used = ctx.get("premium_launches", 0)
            limit = config.get("premium_launches_soft_per_goal", 2)
            if used >= limit:
                facts.append(f"premium_soft_limit_exception:{used + 1}>{limit};{reason}")
        elif name == "investigate":
            tier = config.get("investigate_tier", "sonnet")
        return Route(name, reason, facts, cheaper_steps.copy(), tier)

    def unknown(fields: dict) -> str | None:
        for field, expected in fields.items():
            value = ctx.get(field)
            if field not in ctx or type(value) not in expected:
                return field
            if type(value) is int and value < 0:
                return field
        return None

    if config.get("enabled", True) is False:
        return result("escalate", "routes_disabled")
    if ctx.get("infra_failure_kind"):
        return result("none", f"infra:{ctx['infra_failure_kind']}")
    if "infra_failure_kind" not in ctx or ctx["infra_failure_kind"] is not None:
        return result("escalate", "unknown:infra_failure_kind")

    kind = point.get("kind")
    if kind == "held":
        required = {
            "failing_ids": (list,), "in_scope_review_comments": (bool,),
            "auto_fix_rounds_used": (int,), "auto_fix_rounds": (int,),
            "failure_signature": (str, type(None)), "signature_repeated": (bool,),
            "spec_review_request_changes": (int,), "blocked_scouts": (list,),
            "touches_auth": (bool,), "touches_migrations": (bool,),
            "touches_interfaces": (bool,), "touches_data_deletion": (bool,),
        }
        missing = unknown(required)
        if missing:
            return result("escalate", f"unknown:{missing}")
        risks = [
            (ctx["signature_repeated"], "repeated_signature"),
            (ctx["auto_fix_rounds_used"] >= ctx["auto_fix_rounds"], "lineage_cap_reached"),
            (ctx["spec_review_request_changes"] >= 2, "spec_review_request_changes_twice"),
            (bool(ctx["blocked_scouts"]), "scout_blocked"),
        ]
        risks.extend((ctx[f"touches_{area}"], f"hold_touches_{area}")
                     for area in ("auth", "migrations", "interfaces", "data_deletion"))
        fixable = bool(ctx["failing_ids"]) or ctx["in_scope_review_comments"]
        if fixable and not any(present for present, _ in risks):
            if ctx["failing_ids"] and not ctx["failure_signature"]:
                return result("escalate", "unknown:failure_signature")
            return result("routine", "auto_fix_possible")
        for present, reason in risks:
            if present:
                return result("escalate", reason)
        return result("escalate", "hold_requires_planner")

    if kind == "scouts_done":
        missing = unknown({
            "goal_complexity": (int,), "security_trigger": (bool,),
            "semantic_trigger": (bool,), "scout_count": (int,),
            "all_scouts_posted": (bool,), "blocked_scouts": (list,),
        })
        if missing:
            return result("escalate", f"unknown:{missing}")
        if ctx["blocked_scouts"]:
            return result("escalate", "scout_blocked")
        if not ctx["all_scouts_posted"]:
            return result("escalate", "scouts_incomplete")
        if ctx["security_trigger"]:
            return result("escalate", "security_trigger")
        if ctx["semantic_trigger"]:
            return result("escalate", "semantic_trigger")
        if ctx["goal_complexity"] <= config.get("investigate_max_complexity", 4):
            return result("investigate", "small_goal_no_review_triggers")
        return result("escalate", "goal_complexity")

    if kind == "closable":
        missing = unknown({"all_children_merged": (bool,), "gate_state": (str,)})
        if missing:
            return result("escalate", f"unknown:{missing}")
        if not ctx["all_children_merged"]:
            return result("escalate", "children_unmerged")
        if ctx["gate_state"] == "red":
            return result("escalate", "goal_head_gate_red")
        if ctx["gate_state"] != "green":
            return result("escalate", "unknown:gate_state")
        return result("routine", "children_merged_goal_head_green")
    return result("escalate", "unknown:kind")
