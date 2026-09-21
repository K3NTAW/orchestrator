import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import critical_path, duration


def _task(task_id, seconds=None, *, executor="worker", complexity=2,
          task_class="unfamiliar", merged=True):
    task = {"id": task_id, "role": "execute", "executor": executor,
            "tier": "sonnet", "complexity": complexity,
            "constraints": {"task_class": task_class}}
    if merged:
        task["merged_into"] = "goal/G"
    if seconds is not None:
        task["pipeline"] = {"claimed_at": 100, "gated_at": 100 + seconds}
    return task


def _write(root, *tasks):
    (root / "tasks").mkdir(parents=True)
    for task in tasks:
        (root / "tasks" / f"{task['id']}.json").write_text(json.dumps(task))


class DurationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.addCleanup(self.tempdir.cleanup)

    def test_cold_start_uses_band_prior_when_no_history(self):
        task = _task("new", merged=False, complexity=5)
        result = duration.estimate(task, root=self.root, cfg={})
        self.assertEqual(result["est_s"], critical_path.COLD_START_S["4-6"])
        self.assertEqual(result["source"], "cold_start")
        self.assertEqual(result["n"], 0)

    def test_sufficient_history_uses_class_executor_median(self):
        _write(self.root, *[_task(f"old-{i}", seconds)
                            for i, seconds in enumerate((10, 20, 30, 40, 50, 60))])
        result = duration.estimate(_task("new", merged=False), root=self.root)
        self.assertEqual(result["est_s"], 35)
        self.assertEqual(result["source"], "class_executor")
        self.assertEqual(result["n"], 6)

    def test_sparse_history_falls_back_down_the_ladder(self):
        tasks = [_task(f"same-{i}", 10 + i, executor="target") for i in range(2)]
        tasks += [_task(f"other-{i}", 20 + i, executor="other", task_class="mechanical")
                  for i in range(3)]
        _write(self.root, *tasks)
        result = duration.estimate(_task("new", merged=False, executor="target"), root=self.root)
        self.assertEqual(result["source"], "band")
        self.assertEqual(result["n"], 5)

    def test_trimmed_median_resists_outliers(self):
        clean = [95, 96, 97, 98, 99, 100, 101, 102, 103, 104]
        values = clean + [5000, 5000]
        _write(self.root, *[_task(f"old-{i}", value) for i, value in enumerate(values)])
        result = duration.estimate(_task("new", merged=False), root=self.root)
        self.assertLess(abs(result["est_s"] - 99.5) / 99.5, 0.1)

    def test_explain_for_records_source_and_sample_size(self):
        _write(self.root, *[_task(f"old-{i}", 20 + i) for i in range(5)])
        tasks = [_task("a", merged=False), _task("b", merged=False, complexity=8)]
        explanations = duration.explain_for(tasks, root=self.root)
        self.assertEqual(set(explanations), {"a", "b"})
        self.assertLessEqual({"est_s", "source", "n", "band", "task_class"},
                             explanations["a"].keys())
        self.assertEqual(explanations["a"]["source"], "class_executor")
        self.assertEqual(explanations["a"]["n"], 5)
        self.assertEqual(set(duration.durations_for(tasks, root=self.root)), {"a", "b"})

        cold = duration.explain_for(
            tasks, root=self.root, cfg={"scheduler": {"duration_mode": "cold_start"}})
        self.assertTrue(all(item["source"] == "cold_start" and item["n"] == 0
                            for item in cold.values()))
