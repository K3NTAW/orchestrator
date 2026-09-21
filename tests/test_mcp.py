"""orchestrator.mcp's spawn_* tools: each refuses a task whose role doesn't match what the tool is for, instead of
running it under the wrong role (gotcha 2026-09-18: spawn_spec_review handed an execute task id ran an execute)."""
import json, sys, unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_mcp.py` doesn't add this dir itself
from _harness import TMP  # noqa: F401 -- sets ORCH_ROOT before any `orchestrator` import
from orchestrator import bus, mcp


class SpawnToolsRefuseWrongRole(unittest.TestCase):
    def test_spawn_scout_reports_error_when_worktree_fails(self):
        scout = bus.create_task("failed scout spawn", "s", ["a"], ["x.py"], role="scout")
        self.addCleanup(bus.update, scout["id"], status="done")
        with mock.patch.object(mcp.spawn, "ensure_worktree", side_effect=RuntimeError("worktree add failed")), \
                mock.patch.object(mcp.threading, "Thread") as thread:
            reply = mcp.spawn_scout(scout["id"])
        self.assertEqual(reply, {"status": "error", "reason": "worktree add failed"})
        thread.assert_not_called()
        self.assertEqual(bus.get(scout["id"])["status"], "queued")
        rows = [json.loads(line) for path in bus.RUNS.glob("*.jsonl") for line in path.read_text().splitlines()]
        rows = [row for row in rows if row.get("task") == scout["id"] and row.get("role") != "scout_decision"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "spawn_error")
        self.assertEqual(rows[0]["reason"], "worktree add failed")

    def test_spawn_scout_skips_when_reusable_evidence_is_sufficient(self):
        hits = [{"id": "note-one", "title": "Implementation map", "fresh": True},
                {"id": "note-two", "title": "Test surface", "fresh": True},
                {"id": "old", "title": "Obsolete", "fresh": False}]
        task = bus.create_task("reuse scout", "map implementation", ["a"], ["x.py"], role="scout",
                               constraints={"objective": "implementation-map"})
        self.addCleanup(bus.update, task["id"], status="done")
        check = {"sufficient": True, "hits": hits, "reason": "fresh evidence"}
        with mock.patch.object(mcp.spawn, "git", return_value=mock.Mock(stdout="head\n")), \
                mock.patch.object(mcp.scout_evidence, "reuse_check", return_value=check) as reuse, \
                mock.patch.object(mcp, "_bg", return_value={"status": "spawned"}) as bg:
            reply = mcp.spawn_scout(task["id"])
            self.assertEqual(reply, {"task": task["id"], "status": "skipped: reusable evidence", "hits": hits})
            reuse.assert_called_once_with("map implementation", objective="implementation-map", head_sha="head")
            bg.assert_not_called()
            posted = bus.get(task["id"])
            self.assertEqual(posted["status"], "done")
            self.assertEqual(posted["result"]["reused_evidence"], hits)
            findings = posted["result"]["findings"]
            self.assertEqual(len(findings), 2)
            self.assertEqual(len(mcp.scout_evidence.normalize_findings(posted["result"])), 2)
            for finding in findings:
                self.assertTrue({"finding", "source", "confidence", "relevance"} <= finding.keys())
            bus.update(task["id"], status="queued", constraints={"force": True})
            self.assertEqual(mcp.spawn_scout(task["id"])["status"], "spawned")
            bg.assert_called_once_with(task["id"])
            reuse.side_effect = RuntimeError("recall failed")
            mcp.spawn_scout(task["id"])
            self.assertEqual(bg.call_count, 2)

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

    def test_codex_reply_replaces_previous_round_result(self):
        task_id = self._running()
        original = {"summary": "round one", "commit": "1111111", "thread": "thread-one", "rounds": 1}
        bus.post_result(task_id, original, "done")
        result = {"status": "done", "message": "round two; commit 2222222", "usage": {}, "thread": "thread-one"}
        with mock.patch.object(mcp.executor, "reply", return_value=result):
            reply = mcp.codex_reply(task_id, "fix")
        posted = bus.get(task_id)["result"]
        self.assertTrue(reply["posted"])
        self.assertEqual((posted["summary"], posted["commit"], posted["rounds"], posted["previous_commits"]),
                         ("round two; commit 2222222", "2222222", 2, ["1111111"]))

    def test_codex_fresh_thread_does_not_overwrite_done_task(self):
        task_id = self._running()
        bus.post_result(task_id, {"summary": "old", "commit": "1111111", "thread": "thread-one",
                                  "rounds": 2, "provenance": ["repo"]}, "done")
        bus.update(task_id, codex_thread="thread-one", rounds=1)
        before = bus.get(task_id)
        result = {"status": "done", "message": "new; commit 2222222", "usage": {}, "thread": "thread-two"}
        with mock.patch.object(mcp.executor, "_run", return_value=result) as run:
            reply = mcp.codex(task_id, "go")
        run.assert_not_called()
        self.assertFalse(reply["posted"])
        self.assertEqual(reply["status"], "refused")
        self.assertEqual(reply["reason"],
                         f"task {task_id} already has a result from thread thread-one; use codex_reply for a fix round")
        self.assertEqual(bus.get(task_id), before)

    def test_codex_reply_on_merged_task_is_refused(self):
        task_id = self._running()
        bus.post_result(task_id, {"commit": "1111111", "thread": "thread-one", "rounds": 1}, "done")
        bus.update(task_id, codex_thread="thread-one", merged_into="goal/test")
        before = bus.get(task_id)
        result = {"status": "done", "message": "commit 2222222", "thread": "thread-one"}
        with mock.patch.object(mcp.executor, "_run", return_value=result) as run:
            reply = mcp.codex_reply(task_id, "fix")
        run.assert_not_called()
        self.assertFalse(reply["posted"])
        self.assertEqual(reply["status"], "refused")
        self.assertIn("merged into goal/test", reply["reason"])
        self.assertEqual(bus.get(task_id), before)

    def test_post_tool_result_protects_existing_and_merged_results(self):
        task_id = self._running()
        bus.post_result(task_id, {"commit": "1111111", "thread": "thread-one"}, "done")
        result = {"status": "done", "message": "commit 2222222", "thread": "thread-two"}
        for merged_into, replace_result in ((None, False), ("goal/test", False), ("goal/test", True)):
            with self.subTest(merged_into=merged_into, replace_result=replace_result):
                bus.update(task_id, status="running", merged_into=merged_into)
                before = bus.get(task_id)
                posted, reason = mcp.executor.post_tool_result(task_id, result, replace_result=replace_result)
                self.assertFalse(posted)
                self.assertIn("merged into" if merged_into else "result exists", reason)
                self.assertEqual(bus.get(task_id), before)


if __name__ == "__main__":
    unittest.main()
