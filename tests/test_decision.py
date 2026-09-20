"""Pure routing policy coverage, runnable with the repository's unittest gate."""

from copy import deepcopy
import unittest

from orchestrator.decision import route


def point(kind):
    return {"kind": kind, "goal_id": "G-1", "task_ids": ["T-1"], "payload_key": "key"}


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.ctx = {
            "infra_failure_kind": None, "task_class": "bug_fix",
            "hold_reason": "tests_failed", "failing_ids": ["tests/test_a.py::test_a"],
            "in_scope_review_comments": False, "auto_fix_rounds_used": 1,
            "auto_fix_rounds": 2, "failure_signature": "assertion:42",
            "signature_repeated": False, "spec_review_request_changes": 0,
            "spec_review_rounds": 1, "blocked_scouts": [],
            "touches_auth": False, "touches_migrations": False,
            "touches_interfaces": False, "touches_data_deletion": False,
            "goal_complexity": 4, "scout_count": 2, "all_scouts_posted": True,
            "security_trigger": False, "semantic_trigger": False,
            "all_children_merged": True, "gate_state": "green", "reruns": 1,
        }

    def test_infra_failure_routes_to_hold_without_model(self):
        for kind in ("held", "scouts_done", "closable"):
            for failure in ("auth", "quota", "unavailable"):
                with self.subTest(kind=kind, failure=failure):
                    decision = route(point(kind), {"infra_failure_kind": failure})
                    self.assertEqual((decision.name, decision.reason, decision.tier),
                                     ("none", f"infra:{failure}", None))

    def test_routine_hold_when_auto_fix_possible(self):
        for review_fix in (False, True):
            with self.subTest(review_fix=review_fix):
                ctx = dict(self.ctx)
                if review_fix:
                    ctx.update(failing_ids=[], failure_signature=None, in_scope_review_comments=True)
                decision = route(point("held"), ctx)
                self.assertEqual((decision.name, decision.reason, decision.tier),
                                 ("routine", "auto_fix_possible", None))

    def test_repeated_signature_escalates(self):
        self.ctx["signature_repeated"] = True
        decision = route(point("held"), self.ctx)
        self.assertEqual((decision.name, decision.reason, decision.tier),
                         ("escalate", "repeated_signature", "fable"))

    def test_scouts_done_small_goal_investigates(self):
        decision = route(point("scouts_done"), self.ctx)
        self.assertEqual((decision.name, decision.reason, decision.tier),
                         ("investigate", "small_goal_no_review_triggers", "sonnet"))

    def test_scouts_done_security_trigger_escalates(self):
        self.ctx["security_trigger"] = True
        decision = route(point("scouts_done"), self.ctx)
        self.assertEqual((decision.name, decision.reason), ("escalate", "security_trigger"))

    def test_closable_all_merged_routine(self):
        decision = route(point("closable"), self.ctx)
        self.assertEqual((decision.name, decision.reason, decision.tier),
                         ("routine", "children_merged_goal_head_green", None))

    def test_unknown_risk_escalates(self):
        cases = [
            ("held", "infra_failure_kind failing_ids in_scope_review_comments auto_fix_rounds_used "
             "auto_fix_rounds failure_signature signature_repeated spec_review_request_changes "
             "blocked_scouts touches_auth touches_migrations touches_interfaces touches_data_deletion"),
            ("scouts_done", "infra_failure_kind goal_complexity security_trigger semantic_trigger "
             "scout_count all_scouts_posted blocked_scouts"),
            ("closable", "infra_failure_kind all_children_merged gate_state"),
        ]
        for kind, fields in cases:
            for field in fields.split():
                with self.subTest(kind=kind, field=field):
                    state = {key: value for key, value in self.ctx.items() if key != field}
                    decision = route(point(kind), state)
                    self.assertEqual((decision.name, decision.reason), ("escalate", f"unknown:{field}"))

    def test_disabled_routes_everything_to_escalate(self):
        for kind in ("held", "scouts_done", "closable", "other"):
            with self.subTest(kind=kind):
                decision = route(point(kind), {
                    "routes": {"enabled": False}, "infra_failure_kind": "quota"})
                self.assertEqual((decision.name, decision.reason, decision.tier),
                                 ("escalate", "routes_disabled", "fable"))

    def test_route_carries_evidence_and_cheaper_steps(self):
        original = deepcopy(self.ctx)
        request = point("held")
        original_point = deepcopy(request)
        decision = route(request, self.ctx)
        self.assertLessEqual({
            "task_id:T-1", "hold_reason:tests_failed", "failure_signature:assertion:42",
            "failing_ids:tests/test_a.py::test_a", "signature_repeated:False"}, set(decision.evidence))
        self.assertEqual(decision.cheaper_steps, [
            "auto_fix_rounds_used:1", "scout_count:2", "spec_review_rounds:1", "reruns:1"])
        self.assertEqual(self.ctx, original)
        self.assertEqual(request, original_point)
        self.assertEqual(route(request, self.ctx), decision)

    def test_held_risks_prevent_auto_fix(self):
        cases = [
            ("auto_fix_rounds_used", 2, "lineage_cap_reached"),
            ("spec_review_request_changes", 2, "spec_review_request_changes_twice"),
            ("blocked_scouts", ["T-scout"], "scout_blocked"),
            *[(f"touches_{area}", True, f"hold_touches_{area}")
              for area in ("auth", "migrations", "interfaces", "data_deletion")],
        ]
        for field, value, reason in cases:
            with self.subTest(field=field):
                decision = route(point("held"), {**self.ctx, field: value})
                self.assertEqual((decision.name, decision.reason), ("escalate", reason))

    def test_scouts_require_escalation(self):
        for field, value, reason in [
            ("goal_complexity", 5, "goal_complexity"),
            ("semantic_trigger", True, "semantic_trigger"),
            ("all_scouts_posted", False, "scouts_incomplete"),
            ("blocked_scouts", ["T-scout"], "scout_blocked"),
        ]:
            with self.subTest(field=field):
                decision = route(point("scouts_done"), {**self.ctx, field: value})
                self.assertEqual((decision.name, decision.reason), ("escalate", reason))

    def test_unsafe_close_escalates(self):
        for merged, gate, reason in [
            (False, "green", "children_unmerged"), (True, "red", "goal_head_gate_red"),
            (True, "pending", "unknown:gate_state"), (True, None, "unknown:gate_state"),
        ]:
            with self.subTest(merged=merged, gate=gate):
                decision = route(point("closable"), {
                    **self.ctx, "all_children_merged": merged, "gate_state": gate})
                self.assertEqual((decision.name, decision.reason), ("escalate", reason))

    def test_configured_tiers_threshold_and_soft_exception(self):
        self.ctx["routes"] = {
            "escalate_tier": "premium", "investigate_tier": "cheap",
            "investigate_max_complexity": 5, "premium_launches_soft_per_goal": 1}
        self.ctx["goal_complexity"] = 5
        self.assertEqual(route(point("scouts_done"), self.ctx).tier, "cheap")
        self.ctx.update(goal_complexity=6, premium_launches=1)
        decision = route(point("scouts_done"), self.ctx)
        self.assertEqual((decision.name, decision.tier), ("escalate", "premium"))
        self.assertIn("premium_soft_limit_exception:2>1;goal_complexity", decision.evidence)
