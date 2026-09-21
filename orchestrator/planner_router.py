"""Pure Planner tier policy; callers resolve aliases and gather all state.

Configuration key -> consumer (load_cfg is the single [planner.routing] reader):
mode/default_tier/escalation_tier/hard_*/min_samples/soft_threshold -> this module
shadow_sample_rate -> this module and planner_shadow.eligible
fable_reserve_share -> fable_reserve_ok, called by planner_runs for availability
jev_mode -> jev_planner

Tokens and USD are never scoring inputs. No bus, pool, or file reads occur here;
only configuration warnings and explicit record() calls have side effects.
"""

from dataclasses import dataclass, field
import math

from . import decision_log, notify


BAND_FLOOR = {"1-3": 1, "4-6": 4, "7-10": 7}
_DEFAULTS = {
    "mode": "shadow",
    "default_tier": "opus",
    "escalation_tier": "fable",
    "hard_complexity_min": 9,
    "hard_security": True,
    "hard_spec_review_request_changes_min": 2,
    "hard_ambiguous": True,
    "min_samples": 20,
    "shadow_sample_rate": 1.0,
    "fable_reserve_share": 0.2,
    "jev_mode": "shadow",
    "soft_threshold": 0.5,
}
_WARNED = set()


def load_cfg(pool_cfg):
    """Return routing defaults plus valid overrides, warning once per bad key."""
    table = pool_cfg.get("planner", {}).get("routing", {})
    result = {}
    for key, default in _DEFAULTS.items():
        value = table.get(key, default)
        valid = True
        if key in ("mode", "jev_mode"):
            valid = value in ("off", "shadow", "active")
        elif type(default) in (int, float):
            valid = type(value) in (int, float)
            if valid and isinstance(value, float):
                valid = math.isfinite(value)
        if not valid:
            if key not in _WARNED:
                _WARNED.add(key)
                notify.notify(
                    f"pool.toml [planner.routing].{key} invalid ({value!r}); using {default!r}"
                )
            value = default
        result[key] = value
    return result


def fable_reserve_ok(headroom_share, cfg):
    return headroom_share is None or headroom_share >= cfg.get("fable_reserve_share", 0.2)


def effective_complexity(classification, ctx):
    value = ctx.get("complexity")
    return value if type(value) is int else BAND_FLOOR.get(classification.get("band"), 0)


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


@dataclass(frozen=True)
class RouterDecision:
    tier: str
    mode: str
    hard: bool
    hold: bool
    hold_reason: str | None
    reasons: tuple[str, ...]
    hard_reasons: tuple[str, ...]
    soft_score: float
    shadow_tier: str | None
    candidates: tuple[str, ...] = ("opus", "fable")
    evidence: dict = field(default_factory=dict)
    decision_type: str = "other"


def hard_escalation(classification, ctx, route, cfg):
    if _get(route, "name") != "escalate":
        return []
    reasons = []
    reason = _get(route, "reason") or ""
    if reason.startswith("unknown:") or reason == "routes_disabled":
        reasons.append("unknown_risk")
    if effective_complexity(classification, ctx) >= cfg.get("hard_complexity_min", 9):
        reasons.append("complexity")
    if cfg.get("hard_security", True) and (
        classification.get("risk_class") == "high"
        or any(ctx.get(key) for key in ("security_trigger", "touches_auth", "touches_data_deletion"))
    ):
        reasons.append("security")
    if cfg.get("hard_ambiguous", True) and classification.get("decision_type") in (
        "architectural_replan", "ambiguous_requirement"
    ):
        reasons.append("architectural_or_ambiguous")
    if (ctx.get("spec_review_request_changes") or 0) >= cfg.get("hard_spec_review_request_changes_min", 2):
        reasons.append("repeated_spec_rejection")
    used, cap = ctx.get("auto_fix_rounds_used"), ctx.get("auto_fix_rounds")
    if type(used) is int and type(cap) is int and used >= cap:
        reasons.append("lineage_cap")
    if ctx.get("reescalation"):
        reasons.append("reescalation")
    return reasons


def soft_score(classification, ctx, evidence, jev_signal=None):
    """Score semantic risk, never tokens saved or USD spent."""
    score = 0.3 if classification.get("architectural") else 0.0
    if effective_complexity(classification, ctx) in (7, 8):
        score += 0.2
    if evidence.get("reescalation_rate", 0) > 0.2:
        score += 0.2
    if evidence.get("noninferior") is False:
        score += 0.2
    if classification.get("risk_class") == "low":
        score += 0.1
    score += max(-0.2, min(0.2, (jev_signal or {}).get("soft_adjust", 0)))
    return max(0.0, min(1.0, score))


def decide(classification, ctx, route, *, cfg, availability, evidence, jev_signal=None, sample=1.0):
    mode = cfg.get("mode", "shadow")
    default = cfg.get("default_tier", "opus")
    escalation = cfg.get("escalation_tier", "fable")

    def available(tier):
        return availability.get(tier, {}).get("available", False)

    def result(tier, reasons, *, hard_reasons=(), hold_reason=None, score=0.0, shadow=None):
        return RouterDecision(
            tier=tier, mode=mode, hard=bool(hard_reasons), hold=hold_reason is not None,
            hold_reason=hold_reason, reasons=tuple(reasons), hard_reasons=tuple(hard_reasons),
            soft_score=score, shadow_tier=shadow, candidates=(default, escalation),
            evidence=dict(evidence), decision_type=classification.get("decision_type", "other"),
        )

    if _get(route, "name") != "escalate":
        return result(_get(route, "tier") or "fable", ("not_escalate_route",))
    if mode == "off":
        return result(_get(route, "tier") or "fable", ("mode_off",))
    hard = hard_escalation(classification, ctx, route, cfg)
    if hard:
        if not available(escalation):
            why = availability.get(escalation, {}).get("reason", "unknown")
            return result(escalation, hard + ["hold"], hard_reasons=hard,
                          hold_reason=f"fable_unavailable:{why}")
        return result(escalation, hard, hard_reasons=hard)
    score = soft_score(classification, ctx, evidence, jev_signal)
    if mode == "shadow":
        reasons = ["shadow_production_fable"]
        shadow = None
        if not available(default):
            reasons.append("shadow_default_unavailable")
        elif sample > cfg.get("shadow_sample_rate", 1.0):
            reasons.append("sampled_out")
        else:
            shadow = default
        return result(escalation, reasons, score=score, shadow=shadow)
    noninferior = evidence.get("noninferior")
    enough = evidence.get("n", 0) >= cfg.get("min_samples", 20)
    if noninferior is True and enough and score < cfg.get("soft_threshold", 0.5):
        tier, reason = default, "evidence_noninferior"
    elif availability.get("fable_reserve_ok") is False and noninferior is not False:
        tier, reason = default, "fable_reserve_low"
    else:
        tier = escalation
        reason = "insufficient_evidence" if noninferior is None or not enough else "soft_escalation"
    reasons = [reason]
    if not available(tier):
        other = escalation if tier == default else default
        if not available(other):
            return result(tier, reasons, score=score, hold_reason="no_planner_tier_available")
        reasons.append(f"fallback:{tier}_unavailable")
        tier = other
    return result(tier, reasons, score=score)


def record(decision, *, subject, route, jev_signal=None, root=None):
    selected = "hold" if decision.hold else decision.tier
    return decision_log.record(
        kind="planner_route", subject=subject, candidates=list(decision.candidates),
        hard_constraints=list(decision.hard_reasons),
        deterministic={"route": _get(route, "name"), "reason": _get(route, "reason")},
        historical=dict(decision.evidence), jev=jev_signal, selected=selected,
        rejected=[c for c in decision.candidates if c != selected],
        reason=";".join(decision.reasons), mode=decision.mode,
        extra={"decision_type": decision.decision_type, "hold_reason": decision.hold_reason,
               "shadow_tier": decision.shadow_tier, "soft_score": decision.soft_score},
        root=root,
    )


def explain(decision):
    return (f"type={decision.decision_type} mode={decision.mode} "
            f"selected={'hold' if decision.hold else decision.tier} "
            f"hard={decision.hard} reasons={';'.join(decision.reasons)} "
            f"hold_reason={decision.hold_reason} shadow={decision.shadow_tier}")
