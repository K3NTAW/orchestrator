import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._harness import REPO  # noqa: F401 - initializes ORCH_ROOT before orchestrator imports
from orchestrator import gate


class GateTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / ".orchestrator"
        self.state.mkdir()
        self.state_patch = mock.patch.object(gate, "STATE", self.state)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.tmp.cleanup()

    def script(self, body, name="gate.sh"):
        path = self.root / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    @staticmethod
    def assert_gone(pid):
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        with unittest.TestCase().assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_timeout_kills_process_tree(self):
        parent_file, child_file = self.root / "parent", self.root / "child"
        script = self.script(f"echo $$ > {parent_file}\nsleep 60 &\necho $! > {child_file}\nsleep 60\n")
        result = gate.run_bounded([str(script)], cwd=self.root, timeout_s=1, kill_grace_s=0.05)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["returncode"])
        self.assert_gone(int(parent_file.read_text()))
        self.assert_gone(int(child_file.read_text()))

    def test_cleanup_command_runs_on_timeout(self):
        marker = self.root / "cleaned"
        script = self.script("sleep 60\n")
        cfg = {"gate": {"timeout_s": 1, "cleanup_cmd": f"echo x >> {marker}",
                         "cleanup_timeout_s": 1}}
        with mock.patch.object(gate.notify, "notify"):
            gate.run_gate(self.root, script=script, cfg=cfg)
        self.assertEqual(marker.read_text().splitlines(), ["x", "x"])
        marker.unlink()
        with mock.patch.object(gate.notify, "notify"):
            gate.run_gate(self.root, script=script, cfg={"gate": {"timeout_s": 1}})
        self.assertFalse(marker.exists())

    def test_timeout_retries_once(self):
        count = self.root / "count"
        always = self.script(f"echo x >> {count}\nsleep 60\n", "always.sh")
        with mock.patch.object(gate.notify, "notify"):
            result = gate.run_gate(self.root, script=always, cfg={"gate": {"timeout_s": 1}})
        self.assertEqual((result["attempts"], result["timeouts"]), (2, 2))
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["returncode"])
        self.assertEqual(len(count.read_text().splitlines()), 2)

        marker = self.root / "once"
        once = self.script(f"if [ ! -e {marker} ]; then touch {marker}; sleep 60; fi\nexit 0\n", "once.sh")
        with mock.patch.object(gate.notify, "notify"):
            result = gate.run_gate(self.root, script=once, cfg={"gate": {"timeout_s": 1}})
        self.assertEqual((result["returncode"], result["attempts"], result["timeouts"]), (0, 2, 1))
        self.assertFalse(result["timed_out"])

    def test_fast_gate_unaffected(self):
        for code in (0, 7):
            script = self.script(f"echo out; echo err >&2; exit {code}\n", f"fast-{code}.sh")
            with mock.patch.object(gate.notify, "notify") as notify:
                result = gate.run_gate(self.root, script=script, task_id="T/1", cfg={})
            self.assertEqual(result["returncode"], code)
            self.assertEqual(result["attempts"], 1)
            self.assertIn("out", result["stdout"])
            self.assertIn("err", result["stderr"])
            notify.assert_not_called()
            self.assertFalse((self.state / "gates" / "T-1.json").exists())

    def test_settings_defaults_and_override(self):
        self.assertEqual(gate.settings({}), {"timeout_s": 2700, "cleanup_cmd": None,
                                             "cleanup_timeout_s": 300})
        cfg = {"gate": {"timeout_s": 9, "cleanup_cmd": "true", "cleanup_timeout_s": 4}}
        self.assertEqual(gate.settings(cfg), {"timeout_s": 9, "cleanup_cmd": "true",
                                              "cleanup_timeout_s": 4})
        for invalid in (0, -1, 1.5, "1", True):
            self.assertEqual(gate.settings({"gate": {"timeout_s": invalid}})["timeout_s"], 2700)

    def test_running_gates_over_threshold(self):
        gates = self.state / "gates"
        gates.mkdir()
        old = {"task_id": "old", "worktree": "/old", "pid": 1,
               "started_at": 100, "timeout_s": 100}
        new = {**old, "task_id": "new", "started_at": 150}
        (gates / "old.json").write_text(json.dumps(old))
        (gates / "new.json").write_text(json.dumps(new))
        (gates / "bad.json").write_text("{")
        result = gate.running_gates(now=181)
        self.assertEqual([entry["task_id"] for entry in result], ["old"])
        self.assertEqual(result[0]["elapsed_s"], 81)


if __name__ == "__main__":
    unittest.main()
