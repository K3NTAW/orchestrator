import _harness
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import memory_store


class MemoryStore(unittest.TestCase):
    def _root(self, source=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        memory = root / ".orchestrator" / "memory"
        memory.mkdir(parents=True)
        if source:
            real = Path(__file__).resolve().parents[1] / ".orchestrator" / "memory"
            for name in ("decisions.md", "gotchas.md", "architecture.md", "model-notes.md"):
                shutil.copy2(real / name, memory / name)
            (root / "orchestrator").mkdir()
            for path in (Path(__file__).resolve().parents[1] / "orchestrator").glob("*.py"):
                (root / "orchestrator" / path.name).touch()
        return root

    def test_migrate_parses_real_memory_files_idempotently(self):
        root = self._root()
        memory = root / ".orchestrator" / "memory"
        before = {p.name: p.read_bytes() for p in memory.glob("*.md")}
        first = memory_store.migrate(root)
        second = memory_store.migrate(root)
        self.assertEqual(first, second)
        self.assertTrue(first["records"])
        self.assertIn("architecture", first["counts"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in memory.glob("*.md")})

    def test_search_fts_and_filters(self):
        root = self._root(False)
        record = memory_store.Record(kind="decision", title="Secure cache gate", date="2026-09-20",
            repo="repo", components=["daemon"], files=["orchestrator/daemon.py"],
            tags=["security", "class:feature", "model:astra"], body="A distinctive sentinel phrase.")
        memory_store.add(record, root)
        result = memory_store.search("sentinel", kind="decision", component="daemon",
            file="orchestrator/daemon.py", tag="security", date_from="2026-09-01",
            date_to="2026-09-30", tier="warm", root=root)
        self.assertEqual([record.id], [row["id"] for row in result["records"]])
        filtered = memory_store.search("", task_class="class:feature", model="model:astra", root=root)
        self.assertEqual(1, len(filtered["records"]))
        self.assertFalse(memory_store.search("", component="dae", root=root)["records"])

    def test_rebuild_is_reversible(self):
        root = self._root()
        migrated = memory_store.migrate(root)
        added = memory_store.add(memory_store.Record(kind="strategy", title="Temporary", date="2026-09-23",
                                                       body="Only in sqlite"), root)
        self.assertIsNotNone(memory_store.get(added.id, root))
        rebuilt = memory_store.rebuild(root)
        self.assertEqual(migrated["ids"], rebuilt["ids"])
        self.assertIsNone(memory_store.get(added.id, root))

    def test_fts_fallback_when_unavailable(self):
        root = self._root(False)
        with mock.patch.object(memory_store, "_fts5_available", return_value=False):
            memory_store.add(memory_store.Record(kind="gotcha", title="Fallback needle", date="2026-09-23",
                                                  body="searchable fallback body"), root)
            result = memory_store.search("fallback", root=root)
        self.assertEqual(1, len(result["records"]))
        self.assertTrue(result["metadata"]["warnings"])

    def test_migration_keeps_every_task_id_and_goal_label(self):
        root = self._root(False)
        path = root / ".orchestrator" / "memory" / "decisions.md"
        path.write_text("# decisions\n\n## 2026-09-23 Complete task links\n"
                        "type: decision · goal: T-0043 · tasks: T-0044, T-9999\n"
                        "T-0045 was also involved.\n")
        result = memory_store.migrate(root)
        record = memory_store.get(result["ids"][0], root)
        self.assertEqual("T-0043", record["source_goal"])
        self.assertEqual(["T-0044", "T-9999", "T-0045"], record["source_tasks"])

    def test_add_is_visible_to_fts_search_and_rebuild_resyncs(self):
        root = self._root(False)
        record = memory_store.add(memory_store.Record(kind="strategy", title="Trigger insertion",
            date="2026-09-23", body="quasar trigger text"), root)
        self.assertEqual(record.id, memory_store.search("quasar", root=root)["records"][0]["id"])
        memory_store.rebuild(root)
        self.assertFalse(memory_store.search("quasar", root=root)["records"])
        db = sqlite3.connect(root / ".orchestrator" / "memory" / "index.sqlite")
        try:
            triggers = db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'records_a%'").fetchone()[0]
        finally:
            db.close()
        if memory_store._fts5_available():
            self.assertEqual(3, triggers)

    def test_architecture_bullets_become_records_and_reference_kind_maps(self):
        root = self._root(False)
        (root / "orchestrator").mkdir()
        (root / "orchestrator" / "alpha.py").touch()
        memory = root / ".orchestrator" / "memory"
        (memory / "architecture.md").write_text("repo map abc 2026-09-22\n\n"
            "## alpha.py — First module. More detail.\n\n## absent.py — Missing module.\n")
        (memory / "gotchas.md").write_text("# gotchas\n\n## 2026-09-21 Useful reference\n"
            "type: reference · tasks: T-9999\n- body\n")
        result = memory_store.migrate(root)
        rows = memory_store.search("", root=root, limit=10)["records"]
        architecture = [row for row in rows if row["kind"] == "architecture"]
        self.assertEqual(2, len(architecture))
        alpha = next(row for row in architecture if row["title"].startswith("alpha.py:"))
        self.assertEqual(["orchestrator/alpha.py"], alpha["files"])
        self.assertEqual(["alpha"], alpha["components"])
        self.assertIn("reference", result["counts"])


if __name__ == "__main__":
    unittest.main()
