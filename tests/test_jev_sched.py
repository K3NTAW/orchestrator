import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import decision_log, jev_sched


class TestJevSched(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tasks = {
            "a": {"id": "a", "parent": "G-1", "title": "A", "scope": ["src/a.py"], "spec": "alpha"},
            "b": {"id": "b", "parent": "G-1", "title": "B", "scope": ["src/b.py"], "spec": "beta"},
            "c": {"id": "c", "parent": "G-1", "title": "C", "scope": ["src/c.py"], "spec": "gamma"},
        }
        self.wave = {"wave": ["a", "b", "c"], "deferred": [{"task": "x", "reason": "hard:y"}]}

    def answer(self, questions, semantic=.9):
        answers = {}
        for key in questions:
            value = semantic if key.endswith(":semantic_interference") else .2
            confidence = .8 if key.endswith(":semantic_interference") else .6
            answers[key] = {"noul": value, "confidence": confidence}
        return {"answers": answers, "usage": {}}

    def test_off_mode_never_calls_jev_and_returns_wave_unchanged(self):
        ask = mock.Mock()
        result = jev_sched.annotate(self.wave, [{"a": "a", "b": "b", "level": "soft"}], self.tasks,
                                    goal_head="h", cfg={"jev_mode": "off"}, root=self.root, ask=ask)
        ask.assert_not_called()
        self.assertEqual(result["asked"], 0)
        self.assertEqual((result["wave"], result["deferred"]), (self.wave["wave"], self.wave["deferred"]))

    def test_shadow_mode_records_would_defer_without_changing_wave(self):
        ask = mock.Mock(side_effect=lambda state, questions: self.answer(questions))
        result = jev_sched.annotate(self.wave, [{"a": "a", "b": "b", "level": "soft"}], self.tasks,
                                    goal_head="h", cfg={"jev_mode": "shadow"}, root=self.root, ask=ask)
        self.assertEqual(result["would_defer"], ["b"])
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["wave"], self.wave["wave"])
        rows = [row for row in decision_log.read_all(root=self.root) if row["kind"] == "jev_sched"]
        self.assertEqual(rows[0]["confidence"], .7)

    def test_active_mode_defers_soft_pair_but_never_hard_or_dependency(self):
        pairs = [{"a": "a", "b": "b", "level": "soft", "reasons": ["same_dir"]},
                 {"a": "a", "b": "c", "level": "hard", "reasons": ["overlap"]},
                 {"a": "b", "b": "c", "level": "soft", "reasons": ["dependency:b->c"]}]
        captured = {}
        def ask(state, questions):
            captured.update(questions)
            return self.answer(questions)
        result = jev_sched.annotate(self.wave, pairs, self.tasks, goal_head="h",
                                    cfg={"jev_mode": "active"}, root=self.root, ask=ask)
        self.assertEqual(result["wave"], ["a", "c"])
        self.assertIn({"task": "b", "reason": "jev:a|b"}, result["deferred"])
        self.assertTrue(all(key.startswith("a|b:") for key in captured))

    def test_fail_open_and_single_batched_call_without_n_squared(self):
        pairs = [{"a": "a", "b": f"p{i}", "level": "soft", "reasons": []} for i in range(12)]
        tasks = {**self.tasks, **{f"p{i}": {"id": f"p{i}", "scope": []} for i in range(12)}}
        failed = jev_sched.annotate(self.wave, pairs, tasks, goal_head="fail",
                                    cfg={"jev_mode": "active"}, root=self.root, ask=lambda *_: None)
        self.assertIsNotNone(failed["error"])
        self.assertEqual(failed["wave"], self.wave["wave"])
        ask = mock.Mock(side_effect=lambda state, questions: self.answer(questions, semantic=.1))
        result = jev_sched.annotate(self.wave, pairs + [{"a": "a", "b": "c", "level": "hard"}], tasks,
                                    goal_head="ok", cfg={"jev_mode": "shadow", "jev_max_pairs": 8},
                                    root=self.root, ask=ask)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(result["asked"], 8)
        self.assertEqual(len(ask.call_args.args[1]), 16)

    def test_cache_locking_invalidation_and_privacy(self):
        marker = "TOKEN=abcdefghijklmnopqrstuvwxyz123456"
        self.tasks["a"]["spec"] = marker
        captured = []
        def ask(state, questions):
            captured.append(state)
            return self.answer(questions, semantic=.1)
        pair = [{"a": "a", "b": "b", "level": "soft", "reasons": ["same_dir"]}]
        first = jev_sched.annotate(self.wave, pair, self.tasks, goal_head="h1", root=self.root, ask=ask)
        second = jev_sched.annotate(self.wave, pair, self.tasks, goal_head="h1", root=self.root, ask=ask)
        third = jev_sched.annotate(self.wave, pair, self.tasks, goal_head="h2", root=self.root, ask=ask)
        self.assertEqual([first["cache"], second["cache"], third["cache"]], ["miss", "hit", "miss"])
        self.assertEqual(len(captured), 2)
        self.assertTrue((self.root / "runs" / "jev" / "sched_cache.lock").exists())
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", json.dumps(captured))
        self.assertEqual(captured[0]["pairs"][0]["scope"], [["src/a.py"], ["src/b.py"]])
