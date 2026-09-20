"""Bus rules: acceptance is required, immutable fields, oversize results rejected, events, id sequencing."""
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_bus.py` doesn't add this dir itself
from _harness import REPO, TMP  # noqa: F401  (TMP/ORCH_ROOT must exist before the orchestrator import below)
from orchestrator import bus


class Bus(unittest.TestCase):
    def test_lifecycle_and_rules(self):
        with self.assertRaises(ValueError):
            bus.create_task("x", "spec", [], ["src/**"])            # no acceptance
        n0 = bus.next_id()
        t = bus.create_task("Map auth", "where are tokens read", ["cites path:line"], ["src/auth/**"], parent=None)
        self.assertEqual(t["status"], "queued"); self.assertTrue(t["constraints"]["read_only"])
        bus.claim(t["id"], "claude:B", "wt/x")
        self.assertEqual(bus.read(status="running")[0]["id"], t["id"])
        with self.assertRaises(PermissionError):
            bus.update(t["id"], scope=["everything"])            # only Planner fields are immutable
        with self.assertRaises(ValueError):
            bus.post_result(t["id"], {"summary": "x" * 7000})    # oversize rejected
        bus.post_result(t["id"], {"summary": "ok", "findings": []})
        done = bus.read(t["id"]); self.assertEqual(done["status"], "done"); self.assertIn("provenance", done["result"])
        ev = [e for e in bus.events(0) if e["task"] == t["id"]]
        self.assertEqual([e["kind"] for e in ev], ["created", "update", "update"])
        self.assertEqual(t["id"], n0)
        self.assertEqual(bus.next_id(), f"T-{int(n0.split('-')[1]) + 1:04d}")

    def test_depends_on(self):
        a = bus.create_task("A", "spec a", ["ok"], ["src/**"])
        b = bus.create_task("B", "spec b", ["ok"], ["src/**"], depends_on=[a["id"]])
        self.assertFalse(bus.ready(b))
        bus.update(a["id"], merged_into="goal/x", sha="abc")
        self.assertTrue(bus.ready(b))
        with self.assertRaises(ValueError):
            bus.create_task("C", "spec c", ["ok"], ["src/**"], depends_on=["T-9999"])
        with self.assertRaises(PermissionError):
            bus.update(b["id"], depends_on=[])
        self.assertEqual(bus.dependents(a["id"]), [b])

    def test_read_compact_rows(self):
        t = bus.create_task("A" * 200, "spec " * 1000, ["ok"], ["src/**"])  # 200-char title, ~5,000-char spec
        bus.claim(t["id"], "claude:B")
        bus.update(t["id"], status="held", hold_reason="waiting", reason="x" * 300)
        bus.post_result(t["id"], {"summary": "y" * 300, "findings": []}, status="held")
        rows = bus.read(status_not="done", compact=True)
        row = next(r for r in rows if r["id"] == t["id"])
        self.assertEqual(set(row), {"id", "parent", "role", "status", "complexity", "tier", "title", "depends_on",
                                     "hold_reason", "reason", "merged_into", "assigned_to", "has_result",
                                     "result_summary"})
        self.assertEqual(len(row["title"]), 90)
        self.assertEqual(len(row["reason"]), 120)
        self.assertTrue(row["has_result"])
        self.assertEqual(len(row["result_summary"]), 160)
        self.assertNotIn("spec", row); self.assertNotIn("events", row); self.assertNotIn("acceptance", row)

        modest = bus.create_task("Modest title", "spec " * 1000, ["ok"], ["src/**"])
        modest_row = next(r for r in bus.read(status_not="done", compact=True) if r["id"] == modest["id"])
        self.assertLess(len(json.dumps(modest_row)), 600)

    def test_read_full_shape_unchanged(self):
        t = bus.create_task("Full shape", "a spec", ["ok"], ["src/**"])
        rows = bus.read(status="queued")
        row = next(r for r in rows if r["id"] == t["id"])
        self.assertIn("spec", row); self.assertIn("acceptance", row); self.assertIn("events", row)
        self.assertEqual(row, t)

    def test_read_single_task_full(self):
        t = bus.create_task("Single", "a spec", ["ok"], ["src/**"])
        self.assertEqual(bus.read(t["id"]), t)
        self.assertEqual(bus.read(t["id"], compact=True), t)  # compact never applies to a single-task read


if __name__ == "__main__":
    unittest.main()
