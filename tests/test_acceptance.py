import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import TMP  # noqa: F401  (sets ORCH_ROOT before the orchestrator import below)

from orchestrator.acceptance import missing_tests, named_tests


class AcceptanceTests(unittest.TestCase):
    def test_named_tests_parses_full_and_shorthand_ids(self):
        criteria = [
            "tests/test_one.py::test_first and ::test_second pass",
            "prose only",
            "tests/test_two.py::test_third passes",
        ]
        self.assertEqual(named_tests(criteria), [
            ("tests/test_one.py", "test_first"),
            ("tests/test_one.py", "test_second"),
            ("tests/test_two.py", "test_third"),
        ])

    def test_missing_tests_detects_absent_definitions(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests/test_one.py").write_text(
                "def test_present():\n    pass\n\nclass TestCase:\n    def test_method(self):\n        pass\n"
            )
            acceptance = [
                "tests/test_one.py::test_present, ::test_method and ::test_absent pass",
                "tests/test_missing.py::test_nowhere passes",
            ]
            self.assertEqual(missing_tests(root, acceptance, runner="pytest"), [
                ("tests/test_one.py", "test_absent"),
                ("tests/test_missing.py", "test_nowhere"),
            ])

    def test_missing_tests_flags_module_level_pytest_style_function_under_unittest(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests/test_one.py").write_text("def test_x():\n    pass\n")

            result = missing_tests(
                root, ["tests/test_one.py::test_x passes"], runner="unittest"
            )

            self.assertEqual(result, [("tests/test_one.py", "test_x")])
            self.assertEqual(result[0].reason, "not_collected")
            path, name = result[0]
            self.assertEqual((path, name), ("tests/test_one.py", "test_x"))

    def test_missing_tests_accepts_testcase_method(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests/test_one.py").write_text(
                "import unittest\n\nclass Tests(unittest.TestCase):\n"
                "    def test_x(self):\n        pass\n"
            )

            self.assertEqual(missing_tests(
                root, ["tests/test_one.py::test_x passes"], runner="unittest"
            ), [])

    def test_named_tests_ignores_bare_names(self):
        self.assertEqual(named_tests(["test_one and test_two pass"]), [])


if __name__ == "__main__":
    unittest.main()
