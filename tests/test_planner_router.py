import _harness
import copy
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import decision, decision_log, planner_router as router, planner_taxonomy


class PlannerRouterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = router.load_cfg({})
        self.classification = {
            "decision_type": "initial_goal_plan", "architectural": False,
            "ambiguous": False, "risk_class": "none", "signals": [],
            "band": "4-6", "task_class": "mechanical",
        }
        self.route = decision.Route("escalate", "goal_complexity", [], [], "fable")
        self.availability = {
            "opus": {"available": True, "reason": ""},
            "fable": {"available": True, "reason": ""},
            "fable_reserve_ok": True,
        }
        self.evidence = {"n": 25, "noninferior": True, "reescalation_rate": 0.0}

    def choose(self, *, mode="active", classification=None, ctx=None, route=None,
               cfg=None, availability=None, evidence=None, **kwargs):
        return router.decide(
            self.classification if classification is None else classification,
            {} if ctx is None else ctx, self.route if route is None else route,
            cfg={**self.cfg, "mode": mode, **(cfg or {})},
            availability=self.availability if availability is None else availability,
            evidence=self.evidence if evidence is None else evidence, **kwargs,
        )

    def test_mode_off_reproduces_route_tier(self):
        for kind in planner_taxonomy.DECISION_TYPES:
            with self.subTest(kind=kind):
                result = self.choose(mode="off", classification={"decision_type": kind},
                                     ctx={"complexity": 10}, availability={})
                self.assertEqual(result.tier, "fable")
                self.assertEqual(result.reasons, ("mode_off",))
                self.assertIsNone(result.shadow_tier)
                self.assertFalse(result.hold)
                self.assertEqual(result.candidates, ("opus", "fable"))
        for mode in ("off", "shadow", "active"):
            for name, tier in (("investigate", "sonnet"), ("routine", "custom")):
                result = self.choose(mode=mode, route={"name": name, "tier": tier},
                                     ctx={"complexity": 10}, availability={})
                self.assertEqual(result.tier, tier)
                self.assertEqual(result.reasons, ("not_escalate_route",))
                self.assertFalse(result.hard or result.hold)
                self.assertEqual(result.candidates, ("opus", "fable"))

    def test_hard_escalation_never_downgrades_and_holds_when_fable_unavailable(self):
        cases = [
            ({}, {"complexity": 9}, "complexity"),
            ({"risk_class": "high"}, {}, "security"),
            ({}, {"security_trigger": True}, "security"),
            ({}, {"touches_auth": True}, "security"),
            ({}, {"touches_data_deletion": True}, "security"),
            ({"decision_type": "ambiguous_requirement"}, {}, "architectural_or_ambiguous"),
            ({"decision_type": "architectural_replan"}, {}, "architectural_or_ambiguous"),
            ({}, {"spec_review_request_changes": 2}, "repeated_spec_rejection"),
            ({}, {"auto_fix_rounds_used": 2, "auto_fix_rounds": 2}, "lineage_cap"),
            ({}, {"reescalation": True}, "reescalation"),
        ]
        for classification, ctx, reason in cases:
            with self.subTest(reason=reason, ctx=ctx):
                args = dict(classification=classification, ctx=ctx,
                            jev_signal={"soft_adjust": -0.2})
                result = self.choose(**args)
                self.assertEqual(result.tier, "fable")
                self.assertTrue(result.hard)
                self.assertEqual(result.reasons, (reason,))
                self.assertEqual(result.reasons, result.hard_reasons)
                held = self.choose(**args, availability={
                    **self.availability, "fable": {"available": False, "reason": "budget"},
                })
                self.assertTrue(held.hold)
                self.assertEqual(held.tier, "fable")
                self.assertEqual(held.hold_reason, "fable_unavailable:budget")
                self.assertEqual(held.reasons, held.hard_reasons + ("hold",))
        self.assertFalse(self.choose(classification={"band": "7-10"}).hard)
        for reason in ("unknown:kind", "routes_disabled"):
            self.assertEqual(self.choose(route={"name": "escalate", "reason": reason}).hard_reasons,
                             ("unknown_risk",))
        self.assertFalse(self.choose(ctx={"auto_fix_rounds_used": 0}).hard)

    def test_shadow_mode_keeps_fable_and_sets_shadow_tier(self):
        result = self.choose(mode="shadow")
        self.assertEqual((result.tier, result.shadow_tier), ("fable", "opus"))
        self.assertEqual(result.reasons, ("shadow_production_fable",))
        self.assertIsNone(self.choose(mode="shadow", ctx={"complexity": 9}).shadow_tier)
        unavailable = self.choose(mode="shadow", availability={
            **self.availability, "opus": {"available": False},
        })
        self.assertEqual(unavailable.tier, "fable")
        self.assertIsNone(unavailable.shadow_tier)
        self.assertIn("shadow_default_unavailable", unavailable.reasons)
        sampled = self.choose(mode="shadow", cfg={"shadow_sample_rate": 0.5}, sample=0.6)
        self.assertIsNone(sampled.shadow_tier)
        self.assertIn("sampled_out", sampled.reasons)
        self.assertEqual(self.choose(mode="shadow", cfg={"shadow_sample_rate": 0.5},
                                     sample=0.5).shadow_tier, "opus")

    def test_active_mode_requires_evidence_and_ignores_tokens(self):
        result = self.choose()
        self.assertEqual(result.tier, "opus")
        self.assertEqual(result.reasons, ("evidence_noninferior",))
        insufficient = self.choose(evidence={**self.evidence, "n": 5})
        self.assertEqual(insufficient.tier, "fable")
        self.assertEqual(insufficient.reasons, ("insufficient_evidence",))
        inferior = self.choose(evidence={**self.evidence, "noninferior": False})
        self.assertEqual(inferior.tier, "fable")
        self.assertEqual(inferior.reasons, ("soft_escalation",))
        tokens = self.choose(evidence={**self.evidence, "tokens_saved": 1e9, "usd": 1e9})
        self.assertEqual((tokens.tier, tokens.reasons, tokens.soft_score),
                         (result.tier, result.reasons, result.soft_score))
        reserve = {**self.availability, "fable_reserve_ok": False}
        low = self.choose(availability=reserve, evidence={"noninferior": None})
        self.assertEqual((low.tier, low.reasons), ("opus", ("fable_reserve_low",)))
        self.assertEqual(self.choose(availability=reserve, evidence={
            **self.evidence, "noninferior": False}).tier, "fable")
        self.assertFalse(router.fable_reserve_ok(0.1, self.cfg))
        self.assertTrue(router.fable_reserve_ok(None, self.cfg))
        self.assertTrue(router.fable_reserve_ok(0.2, self.cfg))
        high_score = self.choose(classification={"architectural": True, "band": "7-10"})
        self.assertEqual(high_score.tier, "fable")
        self.assertEqual(high_score.soft_score, 0.5)

    def test_load_cfg_defaults_and_record_kind(self):
        self.assertEqual(router.load_cfg({}), {
            "mode": "shadow", "default_tier": "opus", "escalation_tier": "fable",
            "hard_complexity_min": 9, "hard_security": True,
            "hard_spec_review_request_changes_min": 2, "hard_ambiguous": True,
            "min_samples": 20, "shadow_sample_rate": 1.0, "fable_reserve_share": 0.2,
            "jev_mode": "shadow", "soft_threshold": 0.5, "cheap_tier": "sonnet",
            "cheap_kinds": ["gate_red", "conflict"], "cheap_reescalation_max": 0.3,
            "cheap_min_samples": 5,
        })
        with patch.object(router, "_WARNED", set()), patch.object(router.notify, "notify") as notify, \
                patch.object(router.notify.bus, "locked", side_effect=AssertionError("bus access")):
            for _ in range(2):
                self.assertEqual(router.load_cfg({"planner": {"routing": {"mode": "bad"}}})["mode"],
                                 "shadow")
            notify.assert_called_once_with(
                "pool.toml [planner.routing].mode invalid ('bad'); using 'shadow'")
            for key, value in (("jev_mode", "bad"), ("min_samples", "twenty"),
                               ("soft_threshold", float("nan"))):
                self.assertEqual(router.load_cfg({"planner": {"routing": {key: value}}})[key],
                                 self.cfg[key])
        signal = {"soft_adjust": -0.1}
        result = self.choose(jev_signal=signal)
        with tempfile.TemporaryDirectory() as root:
            row = router.record(result, subject="G-router", route=self.route,
                                jev_signal=signal, root=root)
            self.assertEqual(row["kind"], "planner_route")
            self.assertEqual(row["candidates"], ["opus", "fable"])
            self.assertEqual(row["rejected"], ["fable"])
            self.assertEqual(row["deterministic"]["route"], "escalate")
            self.assertEqual(row["jev"], signal)
            self.assertEqual(row["extra"]["decision_type"], "initial_goal_plan")
            self.assertEqual(decision_log.explain("G-router", root=root), [{**row, "outcomes": []}])
            held = self.choose(ctx={"complexity": 9}, availability={})
            hold_row = router.record(held, subject="G-held", route={"name": "escalate"}, root=root)
            self.assertEqual(hold_row["selected"], "hold")
            self.assertEqual(hold_row["rejected"], ["opus", "fable"])

    def test_soft_fallback_hold_and_input_purity(self):
        for evidence, missing, expected in ((self.evidence, "opus", "fable"),
                                            ({}, "fable", "opus")):
            result = self.choose(evidence=evidence, availability={
                **self.availability, missing: {"available": False},
            })
            self.assertEqual(result.tier, expected)
            self.assertIn(f"fallback:{missing}_unavailable", result.reasons)
        held = self.choose(availability={})
        self.assertTrue(held.hold)
        self.assertEqual(held.hold_reason, "no_planner_tier_available")
        inputs = [self.classification, {}, self.route, self.cfg, self.availability, self.evidence,
                  {"soft_adjust": 100}]
        before = copy.deepcopy(inputs)
        result = router.decide(*inputs[:3], cfg=inputs[3], availability=inputs[4],
                               evidence=inputs[5], jev_signal=inputs[6])
        self.assertEqual(inputs, before)
        self.assertIsNot(result.evidence, self.evidence)
        self.assertNotIn("\n", router.explain(result))
        self.assertEqual(router.soft_score({}, {}, {}, {"soft_adjust": 100}), 0.2)
        self.assertEqual(router.soft_score({}, {}, {}, {"soft_adjust": -100}), 0.0)
        self.assertEqual(router.effective_complexity({"band": "7-10"}, {"complexity": "9"}), 7)
        self.assertEqual(router.effective_complexity({"band": "unknown"}, {}), 0)
        custom = self.choose(cfg={"default_tier": "custom", "escalation_tier": "premium"},
                             availability={"custom": {"available": True}})
        self.assertEqual(custom.candidates, ("custom", "premium"))
        self.assertEqual(custom.tier, "custom")

    def test_cheap_kind_gate_red_routes_to_sonnet_by_default(self):
        availability = {**self.availability, "sonnet": {"available": True, "reason": ""}}
        passing = {"sonnet": {"n": 5, "noninferior": True, "reescalation_rate": 0.1},
                   "opus": self.evidence}
        result = self.choose(ctx={"hold_reason": "gate_red", "complexity": 5},
                             availability=availability, tier_evidence=passing)
        self.assertEqual(result.tier, "sonnet")
        self.assertEqual(result.reasons, ("cheap_kind:gate_red:sonnet",))
        self.assertEqual(result.candidates, ("sonnet", "opus", "fable"))
        self.assertFalse(result.hold or result.hard)
        result = self.choose(ctx={"hold_reason": "merge conflict on orchestrator/bus.py"},
                             availability=availability, tier_evidence=passing)
        self.assertEqual(result.reasons, ("cheap_kind:conflict:sonnet",))
        result = self.choose(ctx={"hold_reason": "manual", "failure_kind": "conflict"},
                             availability=availability, tier_evidence=passing)
        self.assertEqual(result.tier, "sonnet")
        plain = self.choose(ctx={"hold_reason": "manual"}, availability=availability,
                            tier_evidence=passing)
        self.assertEqual(plain.tier, "opus")
        self.assertEqual(plain.candidates, ("opus", "fable"))
        disabled = self.choose(ctx={"hold_reason": "gate_red"}, availability=availability,
                               cfg={"cheap_kinds": []}, tier_evidence=passing)
        self.assertEqual(disabled.tier, "opus")
        self.assertEqual(disabled.reasons, ("evidence_noninferior",))

    def test_cheap_kind_not_taken_in_shadow_mode(self):
        availability = {**self.availability, "sonnet": {"available": True, "reason": ""}}
        passing = {"sonnet": {"n": 30, "noninferior": True, "reescalation_rate": 0.0},
                   "opus": self.evidence}
        result = self.choose(mode="shadow", ctx={"hold_reason": "gate_red"},
                             availability=availability, tier_evidence=passing)
        self.assertEqual(result.tier, "fable")
        self.assertEqual(result.shadow_tier, "opus")
        self.assertFalse(any(r.startswith("cheap_kind") for r in result.reasons))

    def test_cheap_kind_ignores_unrelated_hold_containing_conflict(self):
        cfg = self.cfg
        for ctx in ({"hold_reason": "rebase_conflict"},
                    {"hold_reason": "manual: resolve conflicting requirements"},
                    {"hold_reason": "manual", "failure_kind": "merge_conflict"},
                    {"hold_reason": "manual", "failure_kind": "conflict_marker"}):
            with self.subTest(ctx=ctx):
                self.assertIsNone(router.cheap_kind(ctx, cfg))
        self.assertEqual(router.cheap_kind({"hold_reason": "merge conflict in a.py"}, cfg), "conflict")
        self.assertEqual(router.cheap_kind({"failure_kind": "conflict"}, cfg), "conflict")
        availability = {**self.availability, "sonnet": {"available": True, "reason": ""}}
        passing = {"sonnet": {"n": 30, "noninferior": True, "reescalation_rate": 0.0},
                   "opus": self.evidence}
        result = self.choose(ctx={"hold_reason": "manual: conflicting reviews"},
                             availability=availability, tier_evidence=passing)
        self.assertEqual(result.tier, "opus")
        self.assertEqual(result.candidates, ("opus", "fable"))

    def test_cheap_kind_falls_back_when_floor_not_met(self):
        availability = {**self.availability, "sonnet": {"available": True, "reason": ""}}
        ctx = {"hold_reason": "gate_red"}
        good = {"n": 30, "noninferior": True, "reescalation_rate": 0.1}
        result = self.choose(ctx=ctx, availability=availability, tier_evidence={
            "sonnet": {"n": 30, "noninferior": False, "reescalation_rate": 0.0}, "opus": good})
        self.assertEqual((result.tier, result.reasons), ("opus", ("cheap_kind:gate_red:opus",)))
        result = self.choose(ctx=ctx, availability=availability, tier_evidence={
            "sonnet": {"n": 30, "noninferior": True, "reescalation_rate": 0.5}, "opus": good})
        self.assertEqual(result.tier, "opus")
        result = self.choose(ctx=ctx, availability=availability, cfg={"cheap_min_samples": 10},
                             tier_evidence={"sonnet": {"n": 3, "noninferior": True,
                                                       "reescalation_rate": 0.0}, "opus": good})
        self.assertEqual(result.tier, "opus")
        for no_evidence in ({}, {"n": 0, "noninferior": None, "reescalation_rate": None},
                            {"n": 30, "noninferior": True, "reescalation_rate": None},
                            {"n": 4, "noninferior": True, "reescalation_rate": 0.0}):
            with self.subTest(no_evidence=no_evidence):
                result = self.choose(ctx=ctx, availability=availability,
                                     tier_evidence={"sonnet": no_evidence, "opus": good})
                self.assertEqual((result.tier, result.reasons), ("opus", ("cheap_kind:gate_red:opus",)))
        result = self.choose(ctx=ctx, availability=self.availability)
        self.assertEqual((result.tier, result.reasons), ("opus", ("cheap_kind:gate_red:opus",)))
        bad = {"n": 30, "noninferior": False, "reescalation_rate": 0.9}
        result = self.choose(ctx=ctx, availability=availability, evidence=bad,
                             tier_evidence={"sonnet": bad, "opus": bad, "fable": bad})
        self.assertEqual(result.tier, "fable")
        self.assertFalse(result.reasons[0].startswith("cheap_kind"))
        self.assertEqual(result.candidates, ("sonnet", "opus", "fable"))

    def test_hard_escalation_beats_cheap_kind(self):
        availability = {**self.availability, "sonnet": {"available": True, "reason": ""}}
        cases = [
            ({}, {"complexity": 9}, "complexity"),
            ({}, {"security_trigger": True}, "security"),
            ({"decision_type": "ambiguous_requirement"}, {}, "architectural_or_ambiguous"),
            ({}, {"spec_review_request_changes": 2}, "repeated_spec_rejection"),
        ]
        for extra, ctx, reason in cases:
            with self.subTest(reason=reason):
                result = self.choose(classification={**self.classification, **extra},
                                     ctx={"hold_reason": "gate_red", **ctx}, availability=availability)
                self.assertEqual(result.tier, "fable")
                self.assertTrue(result.hard)
                self.assertIn(reason, result.hard_reasons)

    def test_load_cfg_validates_cheap_keys(self):
        cfg = router.load_cfg({})
        self.assertEqual((cfg["cheap_tier"], cfg["cheap_kinds"], cfg["cheap_reescalation_max"],
                          cfg["cheap_min_samples"]), ("sonnet", ["gate_red", "conflict"], 0.3, 5))
        good = router.load_cfg({"planner": {"routing": {
            "cheap_tier": "haiku", "cheap_kinds": [], "cheap_reescalation_max": 0.1,
            "cheap_min_samples": 5}}})
        self.assertEqual((good["cheap_tier"], good["cheap_kinds"], good["cheap_reescalation_max"],
                          good["cheap_min_samples"]), ("haiku", [], 0.1, 5))
        router._WARNED.clear()
        with patch.object(router.notify, "notify") as notify:
            bad = router.load_cfg({"planner": {"routing": {
                "cheap_tier": 3, "cheap_kinds": "gate_red", "cheap_reescalation_max": "x",
                "cheap_min_samples": float("nan")}}})
            router.load_cfg({"planner": {"routing": {"cheap_tier": 3}}})
        self.assertEqual((bad["cheap_tier"], bad["cheap_kinds"], bad["cheap_reescalation_max"],
                          bad["cheap_min_samples"]), ("sonnet", ["gate_red", "conflict"], 0.3, 5))
        self.assertEqual(notify.call_count, 4)
        router._WARNED.clear()
