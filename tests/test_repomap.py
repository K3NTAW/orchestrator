import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.repomap import build


class RepoMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        package = self.root / "orchestrator"
        package.mkdir()
        (package / "one.py").write_text('"""One leaf. More words."""\ndef one(value): pass\n')
        (package / "two.py").write_text('"""Two leaf."""\ndef two(value): pass\n')
        (package / "hub.py").write_text('"""Central hub."""\nfrom . import one, two\ndef hub(value): pass\n')
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"], cwd=self.root, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_fits_budget(self):
        result = build(self.root, budget_chars=150)
        self.assertLessEqual(len(result), 150)
        self.assertIn("def one(value)", result)
        self.assertIn("def two(value)", result)
        self.assertNotIn("def hub(value)", result)

    def test_build_deterministic(self):
        self.assertEqual(build(self.root), build(self.root))

    def test_header_has_sha(self):
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.root, capture_output=True, text=True, check=True).stdout.strip()
        self.assertTrue(build(self.root).startswith(f"repo map {sha} "))

    def test_build_from_rev_ignores_working_tree(self):
        committed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True,
                                   text=True, check=True).stdout.strip()
        (self.root / "orchestrator" / "one.py").write_text("def working_tree_only(): pass\n")
        result = build(self.root, rev=committed)
        short_sha = subprocess.run(["git", "rev-parse", "--short", committed], cwd=self.root,
                                   capture_output=True, text=True, check=True).stdout.strip()
        self.assertIn("def one(value)", result)
        self.assertNotIn("def working_tree_only()", result)
        self.assertTrue(result.startswith(f"repo map {short_sha} "))

    def test_trim_spreads_across_modules(self):
        for name, count in (("alpha", 12), ("beta", 12), ("gamma", 3), ("delta", 3)):
            symbols = "\n".join(f"def {name}_{index}(): pass" for index in range(count))
            (self.root / "orchestrator" / f"{name}.py").write_text(symbols + "\n")
        result = build(self.root, budget_chars=500)
        sections = {
            name: result.split(f"## {name}.py", 1)[1].split("## ", 1)[0]
            for name in ("alpha", "beta", "gamma", "delta")
        }
        counts = {name: section.count("- def ") for name, section in sections.items()}
        self.assertTrue(all(count >= 3 for count in counts.values()))
        self.assertLess(counts["alpha"], 12)
        self.assertLess(counts["beta"], 12)
        self.assertEqual(counts["gamma"], 3)
        self.assertEqual(counts["delta"], 3)
