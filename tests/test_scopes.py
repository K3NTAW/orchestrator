import _harness
import tempfile
import unittest
from pathlib import Path
from orchestrator import scopes, stale, steering_policy


class ScopeTests(unittest.TestCase):
    def test_shared_import_walk_and_acceptance_id_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pkg").mkdir()
            (root / "pkg/main.py").write_text("def run():\n    from . import api\n    import helper\n")
            (root / "pkg/api.py").write_text("value = 1\n")
            (root / "helper.py").write_text("value = 2\n")
            task = {"scope": ["pkg/main.py"], "worktree": directory,
                    "acceptance": ["Run tests/test_scopes.py::test_example and tests/test_other.py::Case::test_ok"]}
            self.assertEqual(scopes.imported_paths(task), ["helper.py", "pkg/api.py"])
            self.assertEqual(scopes.acceptance_paths(task), ["tests/test_other.py", "tests/test_scopes.py"])
            self.assertIn("helper.py", steering_policy.read_scope(task))
            self.assertEqual(stale.severity(task, {"stale_paths": ["helper.py"]})[0], "medium")
            self.assertEqual(stale.severity(task, {"stale_paths": ["pkg/other.py"]})[0], "medium")
