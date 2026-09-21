from orchestrator import promotion


def evidence(**overrides):
    value = {"n": 50, "first_pass_delta": 0, "fix_rounds_delta": 0,
             "gate_success_delta": 0, "review_findings_delta": 0,
             "security_ok": True, "accepted_cost_delta": 0,
             "accepted_tokens_delta": 0, "latency_delta": 0}
    value.update(overrides)
    return value


def test_insufficient_evidence_stays_shadow():
    result = promotion.evaluate("scheduler", evidence(n=19, accepted_cost_delta=-1,
                                                       first_pass_delta=-1))
    assert result["recommendation"] == "stay"
    assert "insufficient_evidence" in result["reasons"]


def test_token_reduction_alone_does_not_promote():
    result = promotion.evaluate("scheduler", evidence(accepted_tokens_delta=-.3,
                                                       first_pass_delta=-.1))
    assert result["recommendation"] == "stay"
    assert "quality_regression:first_pass" in result["reasons"]


def test_non_inferior_quality_with_cost_improvement_promotes():
    result = promotion.evaluate("scheduler", evidence(accepted_cost_delta=-.15))
    assert result["recommendation"] == "promote"


def test_active_feature_with_regression_demotes():
    cfg = {"scheduler": {"mode": "active"}}
    result = promotion.evaluate("scheduler", evidence(gate_success_delta=-.1), cfg)
    assert result["recommendation"] == "demote"


def test_registry_defaults_and_invalid_config(tmp_path):
    cfg = {"scheduler": {"mode": "broken"}}
    for feature, spec in promotion.FEATURES.items():
        mode, flags = promotion.current_mode(feature, cfg)
        assert mode == spec["default"]
        if feature == "scheduler":
            assert "invalid_config" in flags
        assert promotion.collect(feature, root=tmp_path)["n"] == 0
    assert promotion.current_mode("speculation", {})[0] == "off"
