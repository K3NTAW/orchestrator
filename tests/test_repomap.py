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
