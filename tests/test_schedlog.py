import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import schedlog


class Schedlog(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        replacement = patch.object(schedlog, "SCHED_DIR", Path(directory.name) / "runs" / "sched")
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_append_and_read_tolerates_malformed_lines(self):
        original = {"considered": []}
        schedlog.append("dispatch", original)
        path = schedlog.SCHED_DIR / "dispatch.jsonl"
        with path.open("a") as stream:
            stream.write('\nnot json\n[]\n{"partial":\n')
        schedlog.append("dispatch", {"ts": 123, "considered": [{"task": "T-1"}]})
        rows, malformed = schedlog.read_with_malformed("dispatch")
        self.assertEqual(malformed, 3)
        self.assertEqual(len(rows), 2)
        self.assertIsInstance(rows[0]["ts"], float)
        self.assertEqual(rows[1], {"ts": 123, "considered": [{"task": "T-1"}]})
        self.assertEqual(schedlog.read("dispatch"), rows)
        self.assertNotIn("ts", original)

    def test_read_missing_file_is_empty(self):
        self.assertEqual(schedlog.read("dispatch"), [])
        self.assertEqual(schedlog.read_with_malformed("dispatch"), ([], 0))
        self.assertFalse(schedlog.SCHED_DIR.exists())
