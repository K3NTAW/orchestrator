import subprocess
import tempfile
import unittest
from pathlib import Path


HOOK = Path(__file__).parents[1] / ".claude/hooks/tests-green.sh"


class TestsGreenHook(unittest.TestCase):
    def run_hook(self, source):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tests").mkdir()
            (root / "tests/test_sample.py").write_text(source)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "test"], cwd=root, check=True)
            result = subprocess.run([str(HOOK), str(root)], text=True, capture_output=True)
            log_path = Path(result.stderr.strip().splitlines()[-1]) if result.returncode else None
            return result, root, bool(log_path and log_path.is_file())

    def test_pass_prints_ok_shape(self):
        result, _, _ = self.run_hook("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n")
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stderr.strip(), r"^tests-green: OK [0-9-]+ [0-9a-f]{7} [0-9]+s$")

    def test_fail_prints_log_path(self):
        result, root, log_exists = self.run_hook("import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail('bad')\n")
        self.assertEqual(result.returncode, 2)
        log_path = result.stderr.strip().splitlines()[-1]
        self.assertTrue(log_path.endswith(".log"))
        self.assertTrue(log_exists)
        self.assertTrue(str(root / ".orchestrator/runs/tests") in log_path)


if __name__ == "__main__":
    unittest.main()
