import _harness  # noqa: F401 - share the suite's single isolated ORCH_ROOT
import tempfile
import unittest
from unittest import mock

from orchestrator import decision_log, promotion


def evidence(**overrides):
    value = {"n": 50, "first_pass_delta": 0, "fix_rounds_delta": 0,
             "gate_success_delta": 0, "review_findings_delta": 0,
             "security_ok": True, "accepted_cost_delta": 0,
             "accepted_tokens_delta": 0, "latency_delta": 0}
    value.update(overrides)
    return value


class TestPromotion(unittest.TestCase):
    def test_context_features_default_shadow(self):
        features = ("context_router", "tool_disclosure", "conditional_instructions")
        for feature in features:
            with self.subTest(feature=feature):
                self.assertIn(feature, promotion.FEATURES)
                self.assertEqual(promotion.FEATURES[feature]["default"], "shadow")
                result = promotion.evaluate(feature, {"n": 0})
                self.assertEqual(result["recommendation"], "stay")
                self.assertIn("insufficient_evidence", result["reasons"])
        rows = {row["feature"]: row for row in promotion.report(cfg={}, root="/missing")}
        self.assertTrue(set(features) <= rows.keys())
        self.assertTrue(all(rows[feature]["recommendation"] == "stay" for feature in features))

    def test_mode_loads_pool_config(self):
        with tempfile.TemporaryDirectory() as root:
            pool = promotion.Path(root) / "pool.toml"
            pool.write_text('[context_router]\nmode = "active"\n', encoding="utf-8")
            with mock.patch.object(promotion, "STATE", promotion.Path(root)):
                self.assertEqual(promotion.mode("context_router"), "active")
        self.assertEqual(promotion.mode("tool_disclosure", {}), "shadow")

    def test_planner_routing_feature_registered_and_collected(self):
        self.assertEqual(promotion.FEATURES["planner_routing"]["default"], "shadow")
        rows = [dict(tier="opus", decision_type="close", band="small",
                     task_class="code", architectural=False),
                dict(tier="fable", decision_type="close", band="small",
                     task_class="code", architectural=False)]
        evidence = {"noninferior": True, "deltas": {"first_pass_rate": .1}}
        shadow = {"n": 2, "agreement_rate": .5}
        with tempfile.TemporaryDirectory() as root, \
             mock.patch("orchestrator.planner_telemetry.read_invocations", return_value=rows), \
             mock.patch("orchestrator.planner_scorecard.class_evidence", return_value=evidence), \
             mock.patch("orchestrator.planner_shadow.summary", return_value=shadow):
            result = promotion.collect("planner_routing", root)
            self.assertEqual((result["n"], result["shadow_n"]), (1, 2))
            self.assertEqual(result["classes_noninferior"], 1)
            self.assertEqual(result["classes_inferior"], 0)
            self.assertEqual(result["classes_insufficient"], 0)
            self.assertEqual(result["shadow_agreement_rate"], .5)
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(promotion.collect("planner_routing", root)["n"], 0)

    def test_planner_routing_never_promotes_on_tokens_alone(self):
        tokens = evidence(accepted_tokens_delta=-.99, classes_noninferior=0,
                          classes_inferior=0, classes_insufficient=3)
        self.assertEqual(promotion.evaluate("planner_routing", tokens)["recommendation"], "stay")
        inferior = {**tokens, "classes_inferior": 1}
        self.assertNotEqual(promotion.evaluate("planner_routing", inferior)["recommendation"], "promote")
        noninferior = {**tokens, "classes_noninferior": 1, "classes_insufficient": 0}
        self.assertEqual(promotion.evaluate("planner_routing", noninferior)["recommendation"], "promote")

    def test_format_report_with_reasons_does_not_raise(self):
        line = promotion.format_report([{"feature": "scheduler", "recommendation": "stay",
                                         "mode": "shadow", "n": 1,
                                         "reasons": ["insufficient_evidence"]}])
        self.assertIn("insufficient_evidence", line)

    def test_collect_jev_sched_reads_decision_rows(self):
        with tempfile.TemporaryDirectory() as root:
            common = dict(candidates=["a|b"], hard_constraints=[], deterministic={}, jev={},
                          reason="shadow", confidence=.8, n=1, mode="shadow")
            decision_log.record("jev_sched", "G-1", selected=[], rejected=["b"], root=root, **common)
            decision_log.record("jev_sched", "G-2", selected=[], rejected=[], root=root, **common)
            self.assertEqual(promotion.collect("jev_sched", root=root),
                             {"n": 2, "jev_disagreement_rate": .5, "applied": 0})

    def test_skill_routing_evidence_excludes_static_rows(self):
        with tempfile.TemporaryDirectory() as root:
            common = dict(candidates=["executor/implement-spec"], hard_constraints=[],
                          deterministic={}, selected=["executor/implement-spec"], rejected=[], mode="shadow")
            decision_log.record("skill_selection", "T-static", reason="stage1 static", root=root, **common)
            decision_log.record("skill_selection", "T-routed", reason="mandatory skills", root=root, **common)
            self.assertEqual(promotion.collect("skill_routing", root=root), {"n": 1})

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

    def test_handoff_routing_feature_counts_completed_lineages(self):
        self.assertEqual(promotion.FEATURES["handoff_routing"]["default"], "shadow")
        with tempfile.TemporaryDirectory() as root:
            result = promotion.collect("handoff_routing", root=root)
        self.assertEqual(result["n"], 0)
        self.assertIsNone(result["accepted_tokens_delta"])

    def test_handoff_routing_evidence_counts_handoff_rows(self):
        with tempfile.TemporaryDirectory() as root:
            decision_log.record("handoff", "T-1", candidates=["codex"], selected=["codex"],
                                rejected=[], hard_constraints=[], deterministic={}, jev={},
                                reason="route", confidence=1, n=1, mode="shadow", root=root)
            self.assertEqual(promotion.collect("handoff_routing", root=root)["n"], 1)

    def test_shadow_features_never_promote_without_quality_evidence(self):
        for feature in ("context_router", "tool_disclosure", "conditional_instructions", "handoff_routing"):
            result = promotion.evaluate(feature, {"n": 50, "first_pass_delta": None,
                "fix_rounds_delta": None, "gate_success_delta": None, "review_findings_delta": None,
                "accepted_tokens_delta": -.5, "suite_present": True, "suite_passed": True})
            self.assertEqual(result["recommendation"], "stay")
            self.assertIn("shadow_quality_unmeasured", result["reasons"])

    def test_shadow_features_need_a_passing_context_eval_for_efficiency_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            path = promotion.Path(root)
            (path / "runs").mkdir()
            (path / "runs" / "x.jsonl").write_text('{"context":{"routed_tokens":5,"routed_reduction_ratio":0.5}}\n')
            missing = promotion.collect("context_router", root)
            self.assertIsNone(missing["accepted_tokens_delta"])
            (path / "context_eval.json").write_text('{"suite_passed":true}')
            passed = promotion.collect("context_router", root)
            self.assertEqual(passed["accepted_tokens_delta"], -.5)
