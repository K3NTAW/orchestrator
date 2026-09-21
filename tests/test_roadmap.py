"""Machine-readable roadmap coverage and status derivation."""
import _harness
import tempfile, unittest
from pathlib import Path

from _harness import REPO
from orchestrator import roadmap


class Roadmap(unittest.TestCase):
    def test_every_brief_priority_is_covered(self):
        covered = {(item["program"], item["priority"]) for item in roadmap.REQUIREMENTS}
        self.assertTrue({("phase-i", f"P{i}") for i in range(8)} <= covered)
        self.assertTrue({("adaptive-parallelism", f"P{i}") for i in range(19)} <= covered)
        titles = {item["title"].lower() for item in roadmap.REQUIREMENTS}
        self.assertIn("decision log", titles)
        self.assertIn("promotion", titles)

    def test_status_derivation_missing_partial_and_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "orchestrator/example.py"
            tests = root / "tests/test_example.py"
            source.parent.mkdir()
            tests.parent.mkdir()
            source.write_text("pass\n")
            tests.write_text(
                "import unittest\nclass Example(unittest.TestCase):\n"
                "    def test_exists(self):\n        pass\n"
            )
            base = {
                "files": ["orchestrator/example.py"],
                "tests": ["tests/test_example.py::test_exists"],
            }
            self.assertEqual(roadmap.status({**base, "files": ["missing.py"]}, root, {}), "missing")
            self.assertEqual(roadmap.status({**base, "tests": ["tests/test_example.py::test_absent"]}, root, {}), "partial")
            self.assertEqual(roadmap.status({**base, "mode_key": "feature.mode"}, root, {"feature": {"mode": "shadow"}}), "implemented_shadow")
            self.assertEqual(roadmap.status({**base, "mode_key": "feature.mode"}, root, {"feature": {"mode": "off"}}), "implemented_off")
            self.assertEqual(roadmap.status(base, root, {}), "implemented_active")

    def test_build_on_repo_marks_goal_complete(self):
        report = roadmap.build(REPO)
        incomplete = [
            item for item in report["requirements"]
            if not item["experimental"] and item["status"] in ("partial", "missing")
        ]
        self.assertEqual(incomplete, [])
        self.assertTrue(report["complete"])
        self.assertEqual(report["summary"]["partial"] + report["summary"]["missing"], 0)


if __name__ == "__main__":
    unittest.main()
