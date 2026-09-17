"""orchestrator.executor: event-stream parsing, reply round caps, fallback-to-Claude routing, run logging."""
import json, sys, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_executor.py` doesn't add this dir itself
from _harness import REPO, TMP, FakeProc, codex_stream  # noqa: F401
from orchestrator import bus, executor, pool as P, spawn


class Executor(unittest.TestCase):
    LIVE = {"astra", "luna", "terra", "sol"}

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
        self.assertIn("tests_green", spawn.render("execute", spec="s", acceptance=["a"], scope=["x"]))

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


if __name__ == "__main__":
    unittest.main()
