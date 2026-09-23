import _harness
import json
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest import mock

from orchestrator import harness_depth as depth, spawn


class HarnessDepth(unittest.TestCase):
    def setUp(self):
        self.cfg = {"review": {"security_paths": ["secure/**"],
                    "areas": {"migrations": {"paths": ["migrations/**"]}}}}
        self.task = {"id": "T-small", "role": "execute", "complexity": 2, "scope": ["a.py"],
                     "acceptance": ["tests/test_small.py::test_ok", ".claude/hooks/tests-green.sh ."],
                     "constraints": {"task_class": "mechanical"}}
        self.history = {"mechanical": {"first_pass_defined_count": 5, "first_pass_rate": .8}}

    def classify(self, task=None, history=None):
        return depth.level(task or self.task, tasks=[], cfg=self.cfg,
                           history=self.history if history is None else history)

    def test_levels_from_rules(self):
        for patch, expected in [({"scope": ["secure/a.py"]}, 4), ({"complexity": 6}, 3),
                                ({"complexity": 5}, 2), ({"acceptance": ["works"]}, 1), ({}, 0),
                                ({"scope": ["migrations/a.py"]}, 4),
                                ({"constraints": {"architectural": True}}, 4),
                                ({"constraints": {"goal": True}}, 4),
                                ({"constraints": {"route": "spec_review"}}, 3),
                                ({"scope": ["a.py", "b.py", "c.py", "d.py"]}, 2),
                                ({"scope": ["src/**"]}, 2), ({"complexity": 8}, 4)]:
            with self.subTest(patch=patch):
                result = self.classify({**self.task, **patch})
                self.assertEqual(result["level"], expected)
                self.assertEqual(result["eligible_fast_path"], expected <= 1)
        with mock.patch.object(depth.planner_taxonomy, "classify", return_value={"architectural": True}):
            self.assertEqual(self.classify()["level"], 4)

    def test_missing_history_never_yields_level_zero(self):
        self.assertEqual(self.classify(history={})["level"], 1)
        self.assertEqual(self.classify(history={"mechanical": {
            "first_pass_defined_count": 4, "accepted_tasks": 50, "first_pass_rate": 1}})["level"], 1)
        self.assertEqual(self.classify({**self.task, "acceptance": ["works", ".claude/hooks/tests-green.sh ."]})["level"], 1)

    def test_history_uses_defined_first_pass_count_and_is_computed_once_per_tick(self):
        pool = SimpleNamespace(cfg={"harness": {"depth_mode": "shadow"}})
        with mock.patch.object(depth.scorecard, "efficiency", return_value={"groups": self.history}) as card:
            tick = depth.begin_tick(pool, mock.Mock())
            for _ in range(3):
                self.assertEqual(self.classify(history=tick["history"])["level"], 0)
            card.assert_called_once()
            depth.begin_tick(pool, mock.Mock())
            self.assertEqual(card.call_count, 2)

    def test_active_refuses_to_shadow_when_promotion_criteria_unmet(self):
        pool = SimpleNamespace(cfg={"harness": {"depth_mode": "active"}})
        notify = mock.Mock()
        with mock.patch.object(depth, "history_table", return_value={}), \
             mock.patch.object(depth.promotion, "collect", return_value={"n": 19}):
            tick = depth.begin_tick(pool, notify)
        self.assertEqual(tick["mode"], "shadow")
        notify.assert_called_once()
        self.assertEqual(pool.cfg["harness"]["depth_mode"], "active")

    def test_fast_path_records_empty_skill_selection_row(self):
        with mock.patch.object(depth.decision_log, "record") as record, \
             mock.patch("orchestrator.bus.log_run") as run:
            depth.record_skips(self.task)
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["selected"], [])
        self.assertEqual(record.call_args.kwargs["reason"], "fast_path")
        self.assertEqual(run.call_args.kwargs["reason"], "fast_path")
        task = {**self.task, "pipeline": {"harness_level": 1, "harness_mode": "active"}}
        with mock.patch.object(spawn, "_prepare_skills") as prepare:
            self.assertEqual(spawn._skill_routing(task, "execute", {}, {})["skills_selected"], [])
        prepare.assert_not_called()

    def test_promotion_collection_compares_lineages_and_detects_unmerged_repairs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tasks").mkdir()
            rows = [{"kind": "harness_depth", "subject": "T-shadow", "selected": 1, "mode": "shadow"},
                    {"kind": "harness_depth", "subject": "T-active", "selected": 1, "mode": "active"}]
            tasks = [{"id": "T-active", "pipeline": {"harness_level": 1, "harness_mode": "active"}},
                     {"id": "T-fix-one", "constraints": {"fix_round_for": "T-active"}},
                     {"id": "T-fix-two", "constraints": {"fix_round_for": "T-fix-one"}}]
            for task in tasks:
                (root / "tasks" / (task["id"] + ".json")).write_text(json.dumps(task))
            card = {"tasks": {"T-shadow": {"first_pass": True, "tokens": 100},
                              "T-active": {"first_pass": True, "tokens": 80}}}
            with mock.patch.object(depth.decision_log, "read_all", return_value=rows), \
                 mock.patch.object(depth.scorecard, "efficiency", return_value=card):
                result = depth.promotion_evidence(root)
            self.assertEqual(result["n"], 1)
            self.assertEqual(result["first_pass_delta"], 0)
            self.assertEqual(result["accepted_tokens_delta"], -20)
            self.assertTrue(result["two_fix_rounds"])
