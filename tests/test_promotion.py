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
    def test_fast_path_feature_registered(self):
        self.assertEqual(promotion.FEATURES["fast_path"]["key"], "depth_mode")
        self.assertEqual(promotion.evaluate("fast_path", {"n": 19})["recommendation"], "stay")
        self.assertEqual(promotion.evaluate("fast_path", {"n": 20})["recommendation"], "promote")
        cfg = {"harness": {"depth_mode": "active"}}
        for values, expected in [({"first_pass_delta": 0, "accepted_tokens_delta": -1}, "promote"),
                                 ({"first_pass_delta": -.01, "accepted_tokens_delta": -1}, "stay"),
                                 ({"first_pass_delta": 0, "accepted_tokens_delta": 0}, "stay"),
                                 ({"two_fix_rounds": True}, "demote")]:
            result = promotion.evaluate("fast_path", {"n": 20, "active_n": 1, **values}, cfg)
            self.assertEqual(result["recommendation"], expected)
        self.assertEqual(promotion.evaluate("fast_path", {"n": 0, "two_fix_rounds": True}, cfg)["recommendation"], "demote")

    def test_jev_skill_routing_feature_counts_verdict_rows(self):
        self.assertEqual(promotion.FEATURES["jev_skill_routing"]["default"], "shadow")
        with tempfile.TemporaryDirectory() as root:
            common = dict(candidates=["x"], hard_constraints=[], deterministic={}, selected=[],
                          rejected=["x"], reason="ambiguous", mode="shadow")
            decision_log.record("skill_selection", "T-1", jev={"decisions": {}}, root=root, **common)
            decision_log.record("skill_selection", "T-2", jev=None, root=root, **common)
            self.assertEqual(promotion.collect("jev_skill_routing", root), {"n": 1})

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


class MemoryTierPromotion(unittest.TestCase):
    def test_memory_tiers_feature_registered_with_criteria(self):
        self.assertEqual(promotion.FEATURES["memory_tiers"], {
            "table": "memory", "key": "mode", "modes": ("off", "shadow", "active"),
            "default": "shadow", "evidence": "retrieval"})
        for values, expected in (({"n": 1}, "stay"),
                                 ({"accepted_tokens_delta": -0.2}, "promote"),
                                 ({"accepted_tokens_delta": -0.2, "fix_rounds_delta": None}, "stay"),
                                 ({"accepted_tokens_delta": -0.2, "fix_rounds_delta": 1}, "stay")):
            row = promotion.evaluate("memory_tiers", evidence(**values))
            self.assertEqual(row["recommendation"], expected)
            self.assertEqual(row["criteria"], promotion.CRITERIA)

    def test_memory_tiers_collect_counts_retrieval_rows(self):
        rows = [{"kind": "retrieval", "subject": "T-one", "mode": "shadow",
                 "extra": {"tokens_legacy": 50, "tokens_tiered": 30}},
                {"kind": "retrieval", "subject": "T-two", "mode": "active",
                 "extra": {"tokens_legacy": 100, "tokens_tiered": 70}},
                {"kind": "routing", "extra": {"tokens_legacy": 1000}}]
        with mock.patch.object(decision_log, "read_all", return_value=rows):
            result = promotion.collect("memory_tiers")
        self.assertEqual(result["n"], 2)
        self.assertEqual((result["tokens_legacy"], result["tokens_tiered"]), (150, 100))
        self.assertAlmostEqual(result["accepted_tokens_delta"], -1 / 3)
        self.assertIsNone(result["fix_rounds_delta"])

    def test_memory_tiers_fix_rounds_delta_needs_five_tasks_per_side(self):
        from pathlib import Path
        rows = [{"kind": "retrieval", "subject": f"T-{mode}-{i}", "mode": mode}
                for mode in ("active", "shadow") for i in range(5)]
        paths = [mock.Mock(), mock.Mock(), mock.Mock()]
        for path, parent in zip(paths, ["T-active-0", "T-active-0", "T-shadow-0"]):
            path.read_text.return_value = __import__("json").dumps({"constraints": {"fix_round_for": parent}})
        with mock.patch.object(decision_log, "read_all", return_value=rows) as read, \
                mock.patch.object(Path, "glob", return_value=paths):
            result = promotion.collect("memory_tiers")
            self.assertAlmostEqual(result["fix_rounds_delta"], .2)
            read.return_value = rows[:-1] + [rows[0]] * 10
            self.assertIsNone(promotion.collect("memory_tiers")["fix_rounds_delta"])


class ContractPromotion(unittest.TestCase):
    def test_contracts_feature_registered(self):
        self.assertEqual(promotion.mode("contracts", {}), "shadow")
        self.assertEqual(promotion.FEATURES["contracts"]["criteria"]["min_shadow_samples"], 30)
        self.assertEqual(promotion.evaluate("contracts", {"n": 29})["recommendation"], "stay")
        good = {"n": 30, "repair_success_rate": 1, "failed_results_delta": 0}
        self.assertEqual(promotion.evaluate("contracts", good)["recommendation"], "promote")
        self.assertEqual(promotion.evaluate("contracts", {**good, "failed_results_delta": .1},
            {"contracts": {"mode": "active"}})["recommendation"], "demote")

    def test_contracts_collect_reads_output_contract_rows(self):
        rows = [dict(kind="output_contract", mode="shadow", subject="example", selected="accept",
                     deterministic={"ok": True}, extra={"raw_ok": True}),
                dict(kind="output_contract", mode="shadow", subject="example", selected="repair",
                     deterministic={"ok": True}, extra={"raw_ok": False}),
                dict(kind="unrelated")]
        with mock.patch.object(decision_log, "read_all", return_value=rows):
            result = promotion.collect("contracts", root="/missing")
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["shadow_n"], 2)
        self.assertEqual(result["ok_rate"], .5)
        self.assertEqual(result["repair_rate"], .5)
        self.assertEqual(result["repair_success_rate"], 1)
        self.assertIsNone(result["failed_results_delta"])


class SteeringPromotionTests(unittest.TestCase):
    def test_steering_policy_feature_registered(self):
        spec = promotion.FEATURES["steering_policy"]
        self.assertEqual((spec["table"], spec["key"], spec["default"]), ("steering", "mode", "shadow"))
        self.assertEqual(spec["criteria"]["min_shadow_samples"], 20)
        self.assertEqual(promotion.evaluate("steering_policy", {"n": 20, "shadow_n": 19})["recommendation"], "stay")
        self.assertEqual(promotion.evaluate("steering_policy", {"n": 20, "shadow_n": 20})["recommendation"], "promote")
        cfg = {"steering": {"mode": "active"}}
        measured = {"n": 40, "shadow_n": 20, "active_n": 1, "fix_rounds_delta": -.5, "accepted_tokens_delta": 0}
        self.assertEqual(promotion.evaluate("steering_policy", measured, cfg)["recommendation"], "promote")
        for change in ({"fix_rounds_delta": 0}, {"accepted_tokens_delta": 1}, {"accepted_tokens_delta": None}):
            self.assertEqual(promotion.evaluate("steering_policy", {**measured, **change}, cfg)["recommendation"], "stay")

    def test_steering_collection_compares_lineages_and_candidates(self):
        import json
        from pathlib import Path
        from unittest.mock import patch
        from orchestrator import scorecard
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tasks").mkdir()
            tasks = [{"id": "shadow", "constraints": {}}, {"id": "active", "constraints": {}},
                     {"id": "fix-one", "constraints": {"fix_round_for": "shadow"}},
                     {"id": "fix-two", "constraints": {"fix_round_for": "fix-one"}}]
            for task in tasks:
                (root / "tasks" / (task["id"] + ".json")).write_text(json.dumps(task))
            rows = [{"kind": "steering", "subject": "shadow", "mode": "shadow", "selected": "cancel",
                     "extra": {"candidate_action": "cancel", "outcome": "shadow"}}] * 20
            rows += [{"kind": "steering", "subject": "active", "mode": "active", "selected": "steer",
                      "extra": {"candidate_action": "steer", "outcome": "applied"}}]
            with patch.object(promotion.decision_log, "read_all", return_value=rows), \
                    patch.object(scorecard, "efficiency", return_value={"tasks": {
                        "active": {"tokens": 100, "calls": 1}, "shadow": {"tokens": 120, "calls": 1}}}):
                data = promotion.collect("steering_policy", root)
            self.assertEqual((data["n"], data["shadow_n"], data["steer"], data["cancel"]), (21, 1, 1, 20))
            self.assertEqual(data["mean_fix_rounds_steered"], 0)
            self.assertEqual(data["mean_fix_rounds_non_steered"], 2)
            self.assertEqual(data["fix_rounds_delta"], -2)
            self.assertEqual(data["accepted_tokens_delta"], -20)
