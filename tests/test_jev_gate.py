"""orchestrator/jev_gate.py: the PreToolUse gate that asks Jev whether a worker's proposed tool call is
needed/redundant/destructive. jev.ask() is monkeypatched throughout (same pattern as test_jev.py) -- never
real network. Covers the transcript reader's error correlation, gate_mode="log" never blocking, gate_mode="block"
blocking a confident redundant call and allowing a needed one, the protected-call allowlist, a missing
transcript, and the shell hook exiting 0 for a Planner session (no task id)."""
import contextlib, io, json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_jev_gate.py` doesn't add this dir itself
from _harness import TMP, hook
from orchestrator import bus, jev, jev_gate

BLOCK_CFG = {"enabled": True, "gate_mode": "block"}
LOG_CFG = {"enabled": True, "gate_mode": "log"}


def answers(needed=(0.9, 0.9), redundant=(0.05, 0.9), destructive=(0.0, 0.9)):
    def a(pair):
        return {"noul": pair[0], "confidence": pair[1]}
    return {"answers": {"needed": a(needed), "redundant": a(redundant), "destructive": a(destructive)}}


class JevGateTests(unittest.TestCase):
    def setUp(self):
        self._orig_cfg = jev_gate._cfg
        self._orig_ask = jev.ask
        self._clean_log()

    def tearDown(self):
        jev_gate._cfg = self._orig_cfg
        jev.ask = self._orig_ask
        self._clean_log()

    @staticmethod
    def _clean_log():
        if jev_gate.GATE_LOG.exists():
            jev_gate.GATE_LOG.unlink()

    def _log_lines(self):
        if not jev_gate.GATE_LOG.exists():
            return []
        return [json.loads(l) for l in jev_gate.GATE_LOG.read_text().splitlines() if l.strip()]

    def _task(self):
        return bus.create_task("do x", "spec text", ["tests pass"], ["src/**"], role="execute")

    def _run(self, payload, task_id):
        import os
        old = os.environ.get("ORCH_TASK_ID")
        os.environ["ORCH_TASK_ID"] = task_id
        try:
            return jev_gate.run(payload)
        finally:
            if old is None:
                os.environ.pop("ORCH_TASK_ID", None)
            else:
                os.environ["ORCH_TASK_ID"] = old

    # -- transcript reader -------------------------------------------------

    def test_transcript_reader_correlates_errors(self):
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a.py"}}]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}]}}),
            "not json at all {{{",
            json.dumps({"type": "other", "no": "message"}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "fail", "is_error": True}]}}),
        ]
        path = TMP / "transcript.jsonl"
        path.write_text("\n".join(lines) + "\n")

        recent = jev_gate.read_transcript(str(path))
        self.assertEqual([r["name"] for r in recent], ["Read", "Bash"])
        self.assertEqual([r["error"] for r in recent], [False, True])
        self.assertIn("/a.py", recent[0]["input"])

    def test_missing_transcript_allows(self):
        self.assertEqual(jev_gate.read_transcript(str(TMP / "no-such-transcript.jsonl")), [])

        jev_gate._cfg = lambda: BLOCK_CFG
        jev.ask = lambda state, questions: answers()  # needed call, no missing-transcript block
        task = self._task()
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/x.py"},
                   "transcript_path": str(TMP / "no-such-transcript.jsonl"), "session_id": "s1"}
        self.assertEqual(self._run(payload, task["id"]), 0)

    # -- gate_mode ----------------------------------------------------------

    def test_log_mode_never_blocks(self):
        jev_gate._cfg = lambda: LOG_CFG
        jev.ask = lambda state, questions: answers(needed=(0.05, 0.9), redundant=(0.95, 0.9))
        task = self._task()
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/x.py"}, "transcript_path": "", "session_id": "s1"}

        rc = self._run(payload, task["id"])
        self.assertEqual(rc, 0)
        entries = self._log_lines()
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["blocked"])
        self.assertTrue(entries[0]["scored"])
        self.assertEqual(entries[0]["mode"], "log")

    def test_block_mode_blocks_redundant_read(self):
        jev_gate._cfg = lambda: BLOCK_CFG
        jev.ask = lambda state, questions: answers(needed=(0.5, 0.9), redundant=(0.95, 0.9))
        task = self._task()
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/x.py"}, "transcript_path": "", "session_id": "s1"}

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run(payload, task["id"])
        self.assertEqual(rc, 2)
        self.assertIn("jev-gate", err.getvalue())
        self.assertIn("Read", err.getvalue())
        entries = self._log_lines()
        self.assertTrue(entries[0]["blocked"])

    def test_block_mode_allows_needed_call(self):
        jev_gate._cfg = lambda: BLOCK_CFG
        jev.ask = lambda state, questions: answers(needed=(0.9, 0.9), redundant=(0.05, 0.9))
        task = self._task()
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/y.py"}, "transcript_path": "", "session_id": "s1"}

        rc = self._run(payload, task["id"])
        self.assertEqual(rc, 0)
        entries = self._log_lines()
        self.assertFalse(entries[0]["blocked"])

    def test_ask_none_never_blocks(self):
        jev_gate._cfg = lambda: BLOCK_CFG
        jev.ask = lambda state, questions: None
        task = self._task()
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/z.py"}, "transcript_path": "", "session_id": "s1"}

        rc = self._run(payload, task["id"])
        self.assertEqual(rc, 0)
        entries = self._log_lines()
        self.assertFalse(entries[0]["scored"])
        self.assertFalse(entries[0]["blocked"])

    # -- protected calls ------------------------------------------------------

    def test_protected_calls_never_block(self):
        def boom(state, questions):
            raise AssertionError("jev.ask must not be called for a protected call")
        jev_gate._cfg = lambda: BLOCK_CFG
        jev.ask = boom
        task = self._task()

        protected_payloads = [
            {"tool_name": "Bash", "tool_input": {"command": "bash .claude/hooks/tests-green.sh"}, "session_id": "s1"},
            {"tool_name": "Bash", "tool_input": {"command": "git commit -m wip"}, "session_id": "s1"},
            {"tool_name": "Bash", "tool_input": {"command": "git add -A"}, "session_id": "s1"},
            {"tool_name": "Bash", "tool_input": {"command": "git status"}, "session_id": "s1"},
            {"tool_name": "bus_post_result", "tool_input": {"task": "T-1"}, "session_id": "s1"},
        ]
        for payload in protected_payloads:
            self.assertEqual(self._run(payload, task["id"]), 0, payload)

    def test_hook_exits_zero_without_task(self):
        out = hook("jev-gate.sh", {"tool_name": "Read", "tool_input": {"file_path": "/x.py"}},
                    cwd=TMP, env={"ORCH_TASK_ID": ""})
        self.assertEqual(out.returncode, 0)


if __name__ == "__main__":
    unittest.main()
