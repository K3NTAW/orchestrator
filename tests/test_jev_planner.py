import _harness

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import jev_planner


class TestJevPlanner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.goal = {"id": "G-1", "title": "Goal", "spec": "goal spec", "scope": []}
        self.task = {"id": "T-1", "title": "Task", "spec": "task spec", "scope": ["a.py"]}
        self.classification = {"decision_type": "tier", "risk_class": "medium", "architectural": False,
                               "ambiguous": False, "band": "middle", "task_class": "unfamiliar", "signals": []}
        self.availability = {"luna": {"available": True}, "terra": {"available": True},
                             "fable_reserve_ok": True}

    def call(self, ask_fn, **overrides):
        values = {"cfg": {"jev_mode": "shadow"}, "router_mode": "active", "hard_reasons": [],
                  "availability": self.availability, "route": "escalate", "state_version": "v1",
                  "task": self.task, "ask_fn": ask_fn, "root": self.root}
        values.update(overrides)
        return jev_planner.ask(self.classification, {}, self.goal, **values)

    @staticmethod
    def response(questions, values=None, missing_confidence=False):
        values = values or {key: 0.0 for key in questions}
        answers = {key: {"noul": values[key], "confidence": (None if missing_confidence and i == 0 else i / 10)}
                   for i, key in enumerate(questions)}
        return {"answers": answers}

    def test_off_and_hard_and_single_tier_never_ask(self):
        cases = [({"cfg": {"jev_mode": "off"}}, "jev_off"),
                 ({"hard_reasons": ["security"]}, "hard_decision"),
                 ({"availability": {"luna": {"available": True}}}, "single_tier"),
                 ({"route": "routine"}, "deterministic")]
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                ask_fn = mock.Mock()
                result = self.call(ask_fn, **kwargs)
                self.assertEqual((result["asked"], result["reason"]), (False, reason))
                ask_fn.assert_not_called()
        rows = (self.root / "runs" / "sched" / "planner_jev.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 4)

    def test_batched_single_call_soft_adjust_and_confidence(self):
        positive = {key: float(key != "routine_localized") for key in jev_planner.QUESTIONS}
        ask_fn = mock.Mock(side_effect=lambda _state, questions, **_kw: self.response(questions, positive))
        result = self.call(ask_fn)
        self.assertEqual(ask_fn.call_count, 1)
        self.assertEqual(len(ask_fn.call_args.args[1]), 5)
        self.assertEqual(result["soft_adjust"], .2)
        self.assertAlmostEqual(result["confidence"], .2)
        routine = {key: float(key == "routine_localized") for key in jev_planner.QUESTIONS}
        self.assertEqual(self.call(lambda _s, q, **_kw: self.response(q, routine))["soft_adjust"], -.2)
        missing = self.call(lambda _s, q, **_kw: self.response(q, positive, True))
        self.assertIsNone(missing["confidence"])
        self.assertTrue(-.2 <= result["soft_adjust"] <= .2)

    def test_malformed_and_timeout_fail_open(self):
        unavailable = self.call(lambda *_args, **_kw: None)
        malformed = self.call(lambda *_args, **_kw: {"answers": "garbage"})
        self.assertEqual((unavailable["reason"], unavailable["soft_adjust"]), ("jev_unavailable", 0.0))
        self.assertEqual((malformed["reason"], malformed["soft_adjust"]), ("jev_malformed", 0.0))
        self.assertEqual(len((self.root / "runs" / "sched" / "planner_jev.jsonl").read_text().splitlines()), 2)

    def test_state_is_redacted_bounded_and_task_scoped(self):
        secret = "TOKEN=abcdefghijklmnopqrstuvwxyz123456"
        self.task["spec"] = "task " + secret + "x" * 20000
        classification = {**self.classification, "signals": [secret + str(i) for i in range(30)],
                          "risk_class": "high", "ambiguous": True}
        value = jev_planner.state(classification, {"failing_ids": list(range(3))}, self.goal, self.task)
        self.assertLessEqual(len(value["spec_excerpt"]), 300)
        self.assertTrue(value["spec_excerpt"].startswith("task "))
        self.assertLessEqual(len(value["signals"]), 10)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", json.dumps(value))
        self.assertTrue({"diff", "contents", "comments"}.isdisjoint(value))
        self.assertLessEqual(len(json.dumps(value)), 4000)
        for key in ("decision_type", "band", "risk_class", "ambiguous"):
            self.assertEqual(value[key], classification[key])

    def test_cache_key_is_subject_scoped_with_fingerprint(self):
        cache, ask_fn = {}, mock.Mock(side_effect=lambda _s, q, **_kw: self.response(q))
        first = self.call(ask_fn, cache=cache)
        second = self.call(ask_fn, cache=cache)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(ask_fn.call_count, 1)
        other = {**self.task, "id": "T-2"}
        self.call(ask_fn, cache=cache, task=other)
        for change in ({"risk_class": "high"}, {"ambiguous": True}):
            original = self.classification
            self.classification = {**original, **change}
            self.call(ask_fn, cache=cache)
            self.classification = original
        path = self.root / "runs" / "sched" / "planner_jev.jsonl"
        with path.open("a") as stream:
            stream.write("malformed\n")
        report = jev_planner.summary(self.root)
        self.assertEqual((report["n"], report["asked"], report["cached"]), (5, 5, 1))


if __name__ == "__main__":
    unittest.main()
