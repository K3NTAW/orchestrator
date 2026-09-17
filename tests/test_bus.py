"""Bus rules: acceptance is required, immutable fields, oversize results rejected, events, id sequencing."""
import sys, unittest
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


if __name__ == "__main__":
    unittest.main()
