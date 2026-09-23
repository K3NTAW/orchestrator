import _harness
import tempfile, unittest
from pathlib import Path

from orchestrator import memory_eval


class MemoryEval(unittest.TestCase):
    def test_all_five_cases_pass_on_fixture_root(self):
        with tempfile.TemporaryDirectory() as directory:
            result = memory_eval.run(Path(directory))
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["passed"], 5, result["cases"])
        self.assertTrue(all(case["passed"] for case in result["cases"]))


if __name__ == "__main__":
    unittest.main()
