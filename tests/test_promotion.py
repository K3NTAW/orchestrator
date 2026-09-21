import tempfile
import unittest

from orchestrator import decision_log, promotion


def evidence(**overrides):
    value = {"n": 50, "first_pass_delta": 0, "fix_rounds_delta": 0,
             "gate_success_delta": 0, "review_findings_delta": 0,
             "security_ok": True, "accepted_cost_delta": 0,
             "accepted_tokens_delta": 0, "latency_delta": 0}
    value.update(overrides)
    return value


class TestPromotion(unittest.TestCase):
    def test_collect_jev_sched_reads_decision_rows(self):
        with tempfile.TemporaryDirectory() as root:
            common = dict(candidates=["a|b"], hard_constraints=[], deterministic={}, jev={},
                          reason="shadow", confidence=.8, n=1, mode="shadow")
            decision_log.record("jev_sched", "G-1", selected=[], rejected=["b"], root=root, **common)
            decision_log.record("jev_sched", "G-2", selected=[], rejected=[], root=root, **common)
            self.assertEqual(promotion.collect("jev_sched", root=root),
                             {"n": 2, "jev_disagreement_rate": .5, "applied": 0})

    def test_insufficient_evidence_stays_shadow(self):
        result = promotion.evaluate("scheduler", evidence(
            n=19, accepted_cost_delta=-.15, accepted_tokens_delta=-.3,
            latency_delta=-.1, first_pass_delta=.1, fix_rounds_delta=-.1,
            gate_success_delta=.1, review_findings_delta=.1))
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["recommendation"], "stay")
        self.assertIn("insufficient_evidence", result["reasons"])


    def test_token_reduction_alone_does_not_promote(self):
        result = promotion.evaluate("scheduler", evidence(accepted_tokens_delta=-.3,
                                                           first_pass_delta=-.1))
        self.assertEqual(result["recommendation"], "stay")
        self.assertIn("quality_regression:first_pass", result["reasons"])


    def test_non_inferior_quality_with_cost_improvement_promotes(self):
        result = promotion.evaluate("scheduler", evidence(accepted_cost_delta=-.15))
        self.assertEqual(result["recommendation"], "promote")


    def test_active_feature_with_regression_demotes(self):
        cfg = {"scheduler": {"mode": "active"}}
        result = promotion.evaluate("scheduler", evidence(gate_success_delta=-.1), cfg)
        self.assertEqual(result["recommendation"], "demote")


    def test_registry_defaults_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as root:
            for feature, spec in promotion.FEATURES.items():
                with self.subTest(feature=feature):
                    mode, flags = promotion.current_mode(feature, {})
                    self.assertEqual(mode, spec["default"])
                    self.assertEqual(flags, [])
                    self.assertEqual(promotion.collect(feature, root=root)["n"], 0)
        mode, flags = promotion.current_mode("scheduler", {"scheduler": {"mode": "broken"}})
        self.assertEqual(mode, "shadow")
        self.assertIn("invalid_config", flags)
        self.assertEqual(promotion.current_mode("speculation", {})[0], "off")
