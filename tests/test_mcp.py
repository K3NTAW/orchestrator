"""orchestrator.mcp's spawn_* tools: each refuses a task whose role doesn't match what the tool is for, instead of
running it under the wrong role (gotcha 2026-09-18: spawn_spec_review handed an execute task id ran an execute)."""
import sys, unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_mcp.py` doesn't add this dir itself
from _harness import TMP  # noqa: F401 -- sets ORCH_ROOT before any `orchestrator` import
from orchestrator import bus, mcp


class SpawnToolsRefuseWrongRole(unittest.TestCase):
    def test_spawn_tools_refuse_wrong_role(self):
        execute = bus.create_task("do the thing", "s", ["a"], ["x.py"], role="execute")

        for tool, allowed in (
            (mcp.spawn_scout, ("scout", "triage")),
            (mcp.spawn_review, ("review",)),
            (mcp.spawn_challenge, ("challenge",)),
            (mcp.spawn_spec_review, ("spec_review",)),
        ):
            result = tool(execute["id"])
            expect = f"task {execute['id']} has role execute; this tool takes " + " or ".join(allowed)
            self.assertEqual(result, {"error": expect}, f"{tool.__name__}: {result}")
            self.assertEqual(bus.get(execute["id"])["status"], "queued")  # nothing was run


class WrongRoleHelper(unittest.TestCase):
    def test_accepts_matching_role(self):
        scout = bus.create_task("scout it", "s", ["a"], ["x.py"], role="scout")
        triage = bus.create_task("triage it", "s", ["a"], ["x.py"], role="triage")
        review = bus.create_task("review it", "s", ["a"], ["x.py"], role="review", inputs=["T-does-not-matter"])
        challenge = bus.create_task("challenge it", "s", ["a"], ["x.py"], role="challenge",
                                     inputs=[{"claim": "c", "evidence": "e", "confidence": 0.5}])
        spec_review = bus.create_task("spec review it", "s", ["a"], ["x.py"], role="spec_review",
                                       inputs=["T-does-not-matter"])

        self.assertIsNone(mcp._wrong_role(scout["id"], "scout", "triage"))
        self.assertIsNone(mcp._wrong_role(triage["id"], "scout", "triage"))
        self.assertIsNone(mcp._wrong_role(review["id"], "review"))
        self.assertIsNone(mcp._wrong_role(challenge["id"], "challenge"))
        self.assertIsNone(mcp._wrong_role(spec_review["id"], "spec_review"))


class CodexResultPosting(unittest.TestCase):
    def _running(self):
        task = bus.create_task("execute", "s", ["a"], ["x.py"], role="execute")
        bus.claim(task["id"], "codex", str(TMP))
        bus.update(task["id"], executor="astra")
        self.addCleanup(bus.update, task["id"], status="done")
        return task["id"]

    def test_codex_tool_posts_result_for_running_codex_task(self):
        task_id = self._running()
        result = {"status": "done", "message": "Implemented it; commit deadbeef", "usage": {"output_tokens": 3}}
        with mock.patch.object(mcp.executor, "start", return_value=result):
            reply = mcp.codex(task_id, "go")
        posted = bus.get(task_id)["result"]
        self.assertTrue(reply["posted"])
        self.assertEqual((posted["commit"], posted["executed_by"]), ("deadbeef", "codex:astra"))

    def test_codex_reply_does_not_overwrite_existing_result(self):
        task_id = self._running()
        original = {"summary": "already posted"}
        bus.post_result(task_id, original)
        bus.update(task_id, status="running")
        result = {"status": "done", "message": "new result", "usage": {}}
        with mock.patch.object(mcp.executor, "reply", return_value=result):
            reply = mcp.codex_reply(task_id, "fix")
        self.assertFalse(reply["posted"])
        self.assertEqual(reply["posted_reason"], "result already exists")
        self.assertEqual(bus.get(task_id)["result"]["summary"], "already posted")


if __name__ == "__main__":
    unittest.main()
