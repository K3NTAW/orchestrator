"""orchestrator.executor: event-stream parsing, reply round caps, fallback-to-Claude routing, run logging."""
import json, sys, time, unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_executor.py` doesn't add this dir itself
from _harness import REPO, TMP, FakeProc, codex_stream  # noqa: F401
from orchestrator import bus, executor, pool as P, spawn


class Executor(unittest.TestCase):
    LIVE = {"astra", "luna", "terra", "sol"}

    def reply_checkout(self, head, dirty="", ancestor=True):
        """Mock git and Codex independently, exercising the real reply and event parser."""
        P.PERSIST.unlink(missing_ok=True)
        self.addCleanup(P.PERSIST.unlink, True)
        tid = self.exec_task(title="resume-check")
        bus.update(tid, codex_thread="old-thread", codex_thread_head="a" * 40,
                   executor="astra", rounds=0)
        calls = []
        def run(cmd, **kwargs):
            if cmd[0] == "git":
                self.assertEqual(kwargs["cwd"], str(TMP))
                if cmd[1:] == ["status", "--porcelain"]:
                    return FakeProc(dirty, 0)
                if cmd[1:] == ["rev-parse", "HEAD"]:
                    return FakeProc(head + "\n", 0)
                if cmd[1:] == ["merge-base", "--is-ancestor", "a" * 40, "HEAD"]:
                    return FakeProc("", 0 if ancestor else 1)
                self.fail(f"Unexpected git command: {cmd}")
            self.assertEqual(cmd[:2], ["codex", "exec"])
            calls.append(cmd)
            thread = "old-thread" if cmd[2] == "resume" else "fresh-thread"
            return FakeProc(codex_stream({"type": "thread.started", "thread_id": thread},
                {"type": "turn.completed", "usage": {}}), 0)
        with patch.object(executor.subprocess, "run", side_effect=run), \
                patch.object(spawn, "packet", return_value="compact task packet") as packet:
            result = executor.reply(tid, "repair failing assertion")
        self.assertEqual(result["status"], "done")
        self.assertEqual(bus.get(tid)["codex_thread_head"], head)
        self.assertEqual(len(calls), 1)
        return bus.get(tid), calls[0], packet

    def test_codex_reply_fresh_thread_when_worktree_moved(self):
        for head, dirty, ancestor, reason in (
                ("b" * 40, "", False, "worktree head moved"),
                ("a" * 40, " M x.py\n", True, "worktree is dirty")):
            with self.subTest(reason=reason):
                task, cmd, packet = self.reply_checkout(head, dirty, ancestor)
                self.assertEqual(cmd[2], "-m")
                self.assertNotIn("old-thread", cmd)
                self.assertIn("compact task packet\n\nRepair delta:\nrepair failing assertion", cmd)
                self.assertEqual(task["resume_incompatible"], reason)
                self.assertEqual(task["codex_thread"], "fresh-thread")
                packet.assert_called_once()

    def test_codex_reply_resumes_when_compatible(self):
        for head in ("a" * 40, "b" * 40):
            with self.subTest(head=head):
                task, cmd, packet = self.reply_checkout(head)
                self.assertEqual(cmd[:5], ["codex", "exec", "resume", "old-thread", "repair failing assertion"])
                self.assertNotIn("resume_incompatible", task)
                self.assertEqual(task["codex_thread"], "old-thread")
                packet.assert_not_called()

    def exec_task(self, complexity=3, title="exec"):
        """An execute task with a worktree already set, so start() never has to create one."""
        t = bus.create_task(title, "s", ["a"], ["x.py"], role="execute", complexity=complexity)
        bus.update(t["id"], worktree=str(TMP))
        return t["id"]

    def fake_codex(self, stdout, returncode=0):
        P.PERSIST.unlink(missing_ok=True)
        orig = executor.subprocess.run
        executor.subprocess.run = lambda *a, **k: FakeProc(stdout, returncode)
        self.addCleanup(lambda: setattr(executor.subprocess, "run", orig))
        self.addCleanup(P.PERSIST.unlink, True)

    def test_parse_observed_stream(self):
        lines = ['{"type":"thread.started","thread_id":"01a0ab11-77b3-7431-a9f1-1527ef937b5c"}', '{"type":"turn.started"}',
                 '{"type":"error","message":"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage or try again at Sep 19th, 2026 2:00 PM."}',
                 '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}', "garbage line"]
        ev = executor.parse_events(lines)
        self.assertEqual(ev["thread_id"], "01a0ab11-77b3-7431-a9f1-1527ef937b5c"); self.assertTrue(P.is_rate_limited(ev["error"]))
        ok = executor.parse_events(['{"type":"thread.started","thread_id":"t1"}', '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
                                    '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}'])
        self.assertEqual((ok["message"], ok["usage"]["input_tokens"], ok["error"]), ("done", 10, None))

    def test_resume_argv_has_no_C_flag_and_uses_cwd(self):
        access = ["-s", "workspace-write"]
        self.assertEqual(executor.argv_for("resume", ["thread-1", "fix it"], TMP, access),
                         ["codex", "exec", "resume", "thread-1", "fix it", "--json",
                          "--config", 'sandbox_mode="workspace-write"'])
        self.assertNotIn("-C", executor.argv_for("resume", ["thread-1", "fix it"], TMP, access))

    def test_resume_argv_translates_sandbox_to_config(self):
        cmd = executor.argv_for("resume", ["thread-1", "fix it"], TMP, ["-s", "workspace-write"])
        self.assertIn("--config", cmd)
        self.assertIn('sandbox_mode="workspace-write"', cmd)
        self.assertNotIn("-s", cmd)
        self.assertNotIn("--sandbox", cmd)

    def test_resume_argv_keeps_bypass_flag(self):
        access = ["--dangerously-bypass-approvals-and-sandbox"]
        cmd = executor.argv_for("resume", ["thread-1", "fix it"], TMP, access)
        self.assertIn(access[0], cmd)
        self.assertNotIn("-s", cmd)
        self.assertNotIn("--sandbox", cmd)

    def test_exec_argv_unchanged(self):
        access = ["-s", "workspace-write"]
        args = ["-m", "gpt-6-astra", "do it"]
        self.assertEqual(executor.argv_for("exec", args, TMP, access),
                         ["codex", "exec", *args, "--json", "-C", str(TMP), *access])

    def test_commit_sha_parse_requires_label_or_full_sha(self):
        self.assertIsNone(executor._commit_from_message("Completed 1234567-row migration."))
        self.assertEqual(executor._commit_from_message("commit: deadbeef"), "deadbeef")
        self.assertEqual(executor._commit_from_message("HEAD is now at cafe123"), "cafe123")
        self.assertEqual(executor._commit_from_message("Commit `abcdef0`: implement fix"), "abcdef0")

    def test_usage_error_does_not_count_round(self):
        P.PERSIST.unlink(missing_ok=True); self.addCleanup(P.PERSIST.unlink, True)
        tid = self.exec_task(title="argv-error")
        bus.update(tid, codex_thread="thread-1", rounds=0, executor="astra")
        seen = {}
        orig = executor.subprocess.run

        def usage_error(cmd, **kwargs):
            seen.update(cmd=cmd, kwargs=kwargs)
            return type("Proc", (), {"stdout": "", "stderr": "error: unexpected argument '-C' found\nUsage: codex exec resume",
                                      "returncode": 2})()

        executor.subprocess.run = usage_error
        self.addCleanup(lambda: setattr(executor.subprocess, "run", orig))
        result = executor.reply(tid, "fix it")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["reason"].startswith("codex argv error:"))
        self.assertEqual(bus.get(tid).get("rounds"), 0)
        self.assertEqual(seen["kwargs"]["cwd"], str(TMP))
        self.assertNotIn("-C", seen["cmd"])

    def test_argv_error_logs_run_and_hint(self):
        P.PERSIST.unlink(missing_ok=True); self.addCleanup(P.PERSIST.unlink, True)
        tid = self.exec_task(title="argv-error-visible")
        bus.update(tid, codex_thread="thread-1", rounds=0, executor="astra")
        orig = executor.subprocess.run

        def argv_error(cmd, **kwargs):
            return type("Proc", (), {"stdout": "", "stderr": "error: unexpected argument '-s' found\nUsage: codex exec resume",
                                      "returncode": 2})()

        executor.subprocess.run = argv_error
        self.addCleanup(lambda: setattr(executor.subprocess, "run", orig))
        result = executor.reply(tid, "fix it")
        reason = result["reason"]
        task = bus.get(tid)
        line = json.loads((bus.RUNS / f"{time.strftime('%Y-%m-%d')}.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["task"], line["outcome"], line["reason"]), (tid, "failed", reason))
        self.assertEqual(task["resume_hint"], {"argv_error": reason[:300]})
        self.assertEqual(task.get("rounds"), 0)

    def test_reply_requires_thread_and_caps_rounds(self):
        t = bus.create_task("exec", "s", ["a"], ["x.py"], role="execute")
        self.assertEqual(executor.reply(t["id"], "d")["status"], "failed")
        bus.update(t["id"], codex_thread="t1", worktree=str(TMP), rounds=executor.MAX_ROUNDS)
        self.assertEqual(executor.reply(t["id"], "d")["reason"], "round budget exhausted")

    def test_fallback_claude_when_codex_cooling(self):
        pool = P.Pool(); pool.codex.cooldown_until = time.time() + 3600; pool.save()
        ran = []
        t5 = bus.create_task("small", "s", ["a"], ["x.py"], role="execute", complexity=5)
        t7 = bus.create_task("mid", "s", ["a"], ["x.py"], role="execute", complexity=7)
        t9 = bus.create_task("big", "s", ["a"], ["x.py"], role="execute", complexity=9)
        r5 = executor._exhausted(pool, bus.get(t5["id"]), run=lambda tid: ran.append(tid))
        r7 = executor._exhausted(pool, bus.get(t7["id"]), run=lambda tid: ran.append(tid))
        r9 = executor._exhausted(pool, bus.get(t9["id"]), run=lambda tid: ran.append(tid))
        self.assertEqual((r5["status"], r5["tier"], r7["tier"], r9["status"]), ("fallback", "sonnet", "opus", "held"))
        self.assertEqual(bus.get(t5["id"])["tier"], "sonnet")
        time.sleep(0.2); self.assertEqual(sorted(ran), sorted([t5["id"], t7["id"]]))
        pool.codex.cooldown_until = 0; pool.save()
        self.assertIn(".claude/hooks/tests-green.sh .", spawn.render("execute", spec="s", acceptance=["a"], scope=["x"]))

    def test_start_routes_through_pick_executor(self):
        P.PERSIST.unlink(missing_ok=True); self.addCleanup(P.PERSIST.unlink, True)
        tid = self.exec_task(complexity=3, title="route")
        seen = {}
        orig = executor._run
        executor._run = lambda pool, task, args, cwd, timeout, ex=None: seen.update(args=args, ex=ex) or {"status": "done"}
        self.addCleanup(lambda: setattr(executor, "_run", orig))
        executor.start(tid, "do it")
        models = {e.model for e in P.Pool().executors.values() if e.id in self.LIVE}
        self.assertIn(seen["args"][seen["args"].index("-m") + 1], models)     # the picked row's model, not [codex].model
        t = bus.get(tid)
        self.assertIn(t["executor"], self.LIVE); self.assertEqual(t["tier"], t["executor"])
        self.assertEqual(seen["ex"].id, t["executor"])
        self.assertEqual(P.Pool().executors[t["executor"]].day_tasks, 1)

    def test_start_routes_with_task(self):
        P.PERSIST.unlink(missing_ok=True); self.addCleanup(P.PERSIST.unlink, True)
        tid = self.exec_task(complexity=3, title="route-with-task")
        task = bus.get(tid)
        seen = {}
        orig_pick = P.Pool.pick_executor
        orig_run = executor._run

        def capture_pick(pool, *args, **kwargs):
            seen.update(kwargs)
            return orig_pick(pool, *args, **kwargs)

        P.Pool.pick_executor = capture_pick
        executor._run = lambda *args, **kwargs: {"status": "done"}
        self.addCleanup(lambda: setattr(P.Pool, "pick_executor", orig_pick))
        self.addCleanup(lambda: setattr(executor, "_run", orig_run))

        executor.start(tid, "do it")

        self.assertEqual(seen["task"], task)

    def test_usage_limit_cools_the_whole_quota_group(self):
        self.fake_codex(codex_stream({"type": "thread.started", "thread_id": "th-limit"},
                                     {"type": "error", "message": "You've hit your usage limit. Try again in 30 minutes."}), 1)
        tid = self.exec_task(complexity=3, title="limit")
        r = executor.start(tid, "do it")
        self.assertEqual((r["status"], r["resets_in_s"]), ("held", 1800))
        fresh = P.Pool()                                                     # cooldown survives the MCP restart
        self.assertTrue(all(fresh.executors[i].cooling() for i in self.LIVE))
        self.assertFalse(fresh.codex_available()); self.assertFalse(fresh.codex_available(3))
        self.assertEqual(bus.get(tid)["status"], "held")

    def test_run_log_carries_executor_and_complexity(self):
        self.fake_codex(codex_stream({"type": "thread.started", "thread_id": "th-log"},
                                     {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
                                     {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1,
                                                                          "cached_input_tokens": 7}}))
        tid = self.exec_task(complexity=4, title="log")
        self.assertEqual(executor.start(tid, "do it")["status"], "done")
        line = json.loads((bus.RUNS / f"{time.strftime('%Y-%m-%d')}.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["task"], line["complexity"], line["outcome"]), (tid, 4, "done"))
        self.assertIn(line["executor"], self.LIVE); self.assertEqual(line["tier"], line["executor"])
        self.assertEqual((line["cache_read_input_tokens"], line["cached_input_tokens"]), (7, 7))  # normalized + legacy key
        self.assertEqual(line["goal_id"], tid)
        self.assertEqual(line["provider"], "codex")
        self.assertEqual({key: line[key] for key in ("input_uncached_tokens", "cache_read_tokens",
                         "cache_write_tokens", "output_tokens", "reasoning_tokens", "total_tokens")},
                         {"input_uncached_tokens": 0, "cache_read_tokens": 7, "cache_write_tokens": 0,
                          "output_tokens": 1, "reasoning_tokens": 0, "total_tokens": 8})


if __name__ == "__main__":
    unittest.main()
