import _harness
import tempfile, unittest
from pathlib import Path
from unittest import mock

from orchestrator import memory_hot, memory_store


class MemoryHot(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / ".orchestrator/memory").mkdir(parents=True)
        (root / ".orchestrator/tasks").mkdir(parents=True)
        (root / ".orchestrator/pool.toml").write_text(
            '[memory]\nhot_budget_tokens=3000\nhot_recent_days=14\n'
            'hot_never_compact=["gotcha:open","architecture","decision:constraint","decision:security"]\n')
        return root

    def add(self, root, record_id, kind, title, **values):
        data = dict(id=record_id, kind=kind, title=title, date="2026-09-23", body=title,
                    tags=[], components=[], source_tasks=[], tier="warm")
        data.update(values)
        memory_store.add(memory_store.Record(**data), root)

    def test_pinned_classes_never_dropped(self):
        root = self.fixture()
        self.add(root, "arch", "architecture", "Architecture")
        self.add(root, "constraint", "decision", "Constraint", tags=["constraint"])
        self.add(root, "security", "decision", "Security", tags=["security"])
        self.add(root, "empty", "gotcha", "Empty outcome", outcome="")
        self.add(root, "open", "gotcha", "Open outcome", outcome="still open")
        self.add(root, "unresolved", "gotcha", "Unresolved", outcome="unresolved")
        self.add(root, "task", "reference", "Task", source_tasks=["T-1000"])
        (root / ".orchestrator/tasks/T-1000.json").write_text(
            '{"id":"T-1000","status":"queued","title":"work","scope":[]}')
        result = memory_hot.build(root, budget_tokens=1)
        self.assertEqual(set(result["pinned"]), {"arch", "constraint", "security", "empty", "open", "unresolved", "task"})
        self.assertTrue(result["over_budget"])
        self.assertFalse(result["dropped"])

    def test_budget_fill_by_score_and_tier_marks(self):
        root = self.fixture()
        self.add(root, "high", "gotcha", "High score", outcome="fixed")
        self.add(root, "low", "reference", "Low score")
        result = memory_hot.build(root, budget_tokens=30)
        self.assertIn("high", [record["id"] for record in result["records"]])
        self.assertEqual(memory_store.get("high", root)["tier"], "hot")
        self.assertEqual(memory_store.get("low", root)["tier"], "warm")
        self.assertTrue((root / ".orchestrator/memory/HOT.md").read_text().startswith(memory_hot.HEADER))

    def test_compact_is_idempotent(self):
        root = self.fixture(); self.add(root, "one", "reference", "One")
        first = memory_hot.compact(root)
        content = (root / ".orchestrator/memory/HOT.md").read_bytes()
        second = memory_hot.compact(root)
        self.assertEqual(first["tokens"], second["tokens"])
        self.assertEqual(content, (root / ".orchestrator/memory/HOT.md").read_bytes())

    def test_pinning_uses_open_status_allowlist_on_any_source_task(self):
        root = self.fixture()
        for task_id, status in (("T-1", "superseded"), ("T-2", "done"), ("T-3", "running")):
            (root / f".orchestrator/tasks/{task_id}.json").write_text(
                '{"id":"%s","status":"%s","title":"x","scope":[]}' % (task_id, status))
        self.add(root, "closed", "reference", "Closed", source_tasks=["T-1", "T-404"])
        self.add(root, "mixed", "reference", "Mixed", source_tasks=["T-2", "T-3"])
        result = memory_hot.build(root, budget_tokens=1)
        self.assertEqual(result["pinned"], ["mixed"])
        self.assertIn("closed", result["dropped"])

    def test_hot_header_static_and_second_run_byte_identical(self):
        root = self.fixture(); self.add(root, "one", "reference", "One")
        memory_hot.build(root)
        first = (root / ".orchestrator/memory/HOT.md").read_bytes()
        memory_hot.build(root)
        self.assertEqual(first, (root / ".orchestrator/memory/HOT.md").read_bytes())
        self.assertEqual(first.splitlines()[0].decode(), memory_hot.HEADER)


if __name__ == "__main__":
    unittest.main()
