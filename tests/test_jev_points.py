import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import jev_points


class JevPointsTest(unittest.TestCase):
    def setUp(self):
        self.task = {"id": "T-1", "title": "point", "spec": "small", "acceptance": ["ok"],
                     "scope": ["orchestrator/x.py"], "complexity": 4,
                     "constraints": {"task_class": "unfamiliar"}}

    @staticmethod
    def answer(first, confidence=.8):
        second = 1 - first
        return {"answers": {"launch": {"noul": first, "confidence": confidence},
                            "skip": {"noul": second, "confidence": confidence}}}

    def test_default_shadow_records_suggestion_without_applying(self):
        with mock.patch.object(jev_points.decision_log, "record") as record:
            result = jev_points.ask("scout_necessity", self.task, {}, cfg={},
                                    ask_fn=mock.Mock(return_value=self.answer(.2)))
        self.assertEqual(result["suggestion"], "skip")
        self.assertFalse(result["applied"])
        self.assertEqual(record.call_args.kwargs["extra"]["point"], "scout_necessity")

    def test_off_mode_makes_no_call(self):
        ask_fn = mock.Mock()
        with mock.patch.object(jev_points.decision_log, "record"):
            result = jev_points.ask("planner_relaunch", self.task, {},
                                    cfg={"jev": {"points": {"planner_relaunch": "off"}}}, ask_fn=ask_fn)
        ask_fn.assert_not_called()
        self.assertIsNone(result["suggestion"])

    def test_active_applies_only_escalations(self):
        cfg = {"jev": {"points": {point: "active" for point in jev_points.POINTS}}}
        answers = {
            "review_escalation": {"add_review": .9, "keep": .1},
            "scout_necessity": {"launch": .1, "skip": .9},
            "context_escalation": {"expand": .1, "keep": .9},
        }
        with mock.patch.object(jev_points.decision_log, "record"):
            results = {point: jev_points.ask(point, self.task, {}, cfg=cfg, ask_fn=lambda state, questions, **kw:
                       {"answers": {key: {"noul": value, "confidence": .7} for key, value in answers[point].items()}})
                       for point in answers}
        self.assertTrue(results["review_escalation"]["applied"])
        self.assertFalse(results["scout_necessity"]["applied"])
        self.assertFalse(results["context_escalation"]["applied"])

    def test_review_escalation_never_reduces_or_replaces_security_review(self):
        evidence = {"review_count": 2, "reviews": ["security", "correctness"]}
        cfg = {"jev": {"points": {"review_escalation": "active"}}}
        response = {"answers": {"add_review": {"noul": .1, "confidence": .9},
                                "keep": {"noul": .9, "confidence": .9}}}
        with mock.patch.object(jev_points.decision_log, "record") as record:
            result = jev_points.ask("review_escalation", self.task, evidence, cfg=cfg,
                                    ask_fn=mock.Mock(return_value=response))
        self.assertEqual(result["suggestion"], "keep")
        self.assertEqual(record.call_args.kwargs["hard_constraints"],
                         ["security_review_deterministic", "required_reviews_floor"])
        self.assertEqual(record.call_args.kwargs["deterministic"]["review_count"], 2)

    def test_fail_open_and_privacy(self):
        self.task.update(spec="TOKEN=abcdefghijklmnopqrstuvwxyz123456 secret text")
        evidence = {"file_contents": "private source", "packet_size": 12}
        captured = {}

        def unavailable(state, questions, **kwargs):
            captured.update(state)
            return None

        with mock.patch.object(jev_points.decision_log, "record"):
            result = jev_points.ask("context_escalation", self.task, evidence, cfg={}, ask_fn=unavailable)
        self.assertIsNotNone(result["error"])
        self.assertIsNone(result["suggestion"])
        self.assertFalse(result["applied"])
        self.assertNotIn("private source", str(captured))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", str(captured))


if __name__ == "__main__":
    unittest.main()
