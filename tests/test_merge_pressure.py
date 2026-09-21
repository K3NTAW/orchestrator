import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import merge_pressure, schedlog


class MergePressure(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "tasks").mkdir()
        replacement = patch.object(schedlog, "SCHED_DIR", self.root / "runs" / "sched")
        replacement.start()
        self.addCleanup(replacement.stop)

    def task(self, task_id, scope=("shared.py",), **fields):
        task = {"id": task_id, "role": "execute", "status": "done", "scope": list(scope),
                "pipeline": {"gated_at": 90, "first_green_at": 80}, "events": [], **fields}
        (self.root / "tasks" / f"{task_id}.json").write_text(json.dumps(task))
        return task

    def test_single_waiting_task_is_no_pressure(self):
        waiting = self.task("T-1")
        candidate = {"id": "T-2", "scope": ["shared.py"]}
        result = merge_pressure.assess(self.root, now=100)
        throttled = merge_pressure.throttle(["T-2"], [waiting, candidate], ["T-1"], result,
                                             cfg={"merge_pressure_mode": "throttle"})
        self.assertEqual(result["pressure"], "none")
        self.assertEqual(throttled["keep"], ["T-2"])

    def test_saturated_queue_defers_only_coupled_candidates(self):
        tasks = [self.task(f"T-{index}", scope=(f"file-{index}.py",)) for index in range(1, 7)]
        coupled = {"id": "C-1", "scope": ["file-1.py"]}
        independent = {"id": "C-2", "scope": ["elsewhere/unique.py"]}
        assessment = merge_pressure.assess(self.root, now=100)
        result = merge_pressure.throttle(["C-1", "C-2"], tasks + [coupled, independent],
                                         assessment["waiting"], assessment,
                                         cfg={"merge_pressure_mode": "throttle"})
        self.assertEqual(result["deferred"], {"C-1": "merge_pressure:T-1"})
        self.assertEqual(result["keep"], ["C-2"])
        self.assertTrue(result["applied"])

    def test_conflicts_plus_elevated_queue_is_saturated(self):
        for index in range(1, 4):
            self.task(f"T-{index}", scope=(f"file-{index}.py",))
        for index in range(4, 6):
            self.task(f"T-{index}", status="failed", reason="rebase_conflict", created_at=95,
                      pipeline={})
        result = merge_pressure.assess(self.root, now=100)
        self.assertEqual(result["pressure"], "saturated")
        self.assertIn("queue depth 3", result["reason"])
        self.assertIn("2 recent conflicts", result["reason"])

    def test_observe_mode_records_would_defer_without_applying(self):
        tasks = [self.task(f"T-{index}") for index in range(1, 7)]
        candidate = {"id": "C-1", "scope": ["shared.py"]}
        assessment = merge_pressure.assess(self.root, now=100)
        result = merge_pressure.throttle(["C-1"], tasks + [candidate], assessment["waiting"], assessment)
        self.assertEqual(result["keep"], ["C-1"])
        self.assertEqual(result["would_defer"], {"C-1": "merge_pressure:T-1"})
        self.assertFalse(result["applied"])

    def test_malformed_stale_rows_are_tolerated(self):
        path = schedlog.SCHED_DIR / "stale.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('broken\n{"ts": 99, "action": "rebased", "risk": "high"}\n')
        result = merge_pressure.assess(self.root, now=100)
        self.assertEqual(result["recent_rebases"], 1)
        self.assertEqual(result["stale_events"], 1)


if __name__ == "__main__":
    unittest.main()
