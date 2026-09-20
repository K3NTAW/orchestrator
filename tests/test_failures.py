"""Shared failure evidence must be usable without the pipeline driver."""
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO
from orchestrator import failures, gitutil


class FailureEvidence(unittest.TestCase):
    def test_failures_module_has_no_daemon_import(self):
        source = (REPO / "orchestrator/failures.py").read_text()
        self.assertIsNone(re.search(r"^\s*(?:from\s+[^\n]*\bdaemon\b\s+import|from\s+\.\s+import\s+[^\n]*\bdaemon\b|import\s+[^\n]*\bdaemon\b)", source, re.M))

    def test_touch_areas_fail_closed_without_diff(self):
        for paths, lines in ((None, []), ([], None), (None, None)):
            with patch.object(gitutil, "changed_paths", return_value=paths), patch.object(gitutil, "_added_diff_lines", return_value=lines):
                self.assertEqual(failures.touch_areas({}, {}), {area: True for area in failures.DEFAULT_AREAS})

    def test_touch_areas_uses_configured_paths_and_patterns(self):
        cfg = {"review": {"areas": {"auth": {"paths": ["security/**"], "patterns": []},
                                     "data_deletion": {"paths": [], "patterns": ["erase"]}}}}
        with patch.object(gitutil, "changed_paths", return_value=["security/check.py"]), patch.object(gitutil, "_added_diff_lines", return_value=["erase(records)"]):
            self.assertEqual(failures.touch_areas({}, cfg), {"auth": True, "migrations": False, "interfaces": False, "data_deletion": True})

    def test_test_ids_reject_shell_and_traversal(self):
        self.assertEqual(failures.test_ids("FAILED tests/test_x.py::test_ok\nFAILED ../../evil.py\nFAILED $(id)"),
                         ["tests/test_x.py::test_ok"])
