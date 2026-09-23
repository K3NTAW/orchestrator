import _harness
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from _harness import g, scratch_repo
from orchestrator import bus, worker_control as control, worker_registry as registry


class WorkerControl(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = scratch_repo(Path(temporary.name) / 'repo')
        self.task = bus.create_task('cancel worker', 'synthetic task', ['pass'], ['change.txt'], role='execute')
        self.tid = self.task['id']
        self.addCleanup(registry._path(self.tid).unlink, missing_ok=True)
        self.addCleanup(registry._path(self.tid, events=True).unlink, missing_ok=True)
        self.addCleanup(bus.update, self.tid, status='held')
        g('branch', 'goal/example', cwd=self.repo, check=True)
        g('checkout', '-qb', 'task/example', cwd=self.repo, check=True)
        (self.repo / 'change.txt').write_text('committed\n')
        g('add', '.', cwd=self.repo, check=True)
        g('commit', '-qm', 'partial work', cwd=self.repo, check=True)
        self.sha = g('rev-parse', 'HEAD', cwd=self.repo).stdout.strip()
        (self.repo / 'change.txt').write_text('unfinished\n')
        self.task = bus.update(self.tid, status='running', worktree=str(self.repo), parent='example', pid=43210)
        registry.upsert(self.tid, status='running', provider='codex', pid=43210, worktree=str(self.repo))

    def test_cancel_signals_process_preserves_worktree_and_holds_task(self):
        before = g('status', '--porcelain', cwd=self.repo).stdout
        running = [True]
        def stop(pid, sig):
            self.assertEqual(registry.get(self.tid)['status'], 'cancelling')
            self.assertEqual((pid, sig), (43210, signal.SIGTERM))
            running[0] = False
        with mock.patch.object(control.pool, 'Pool') as pool:
            partial = control.cancel(self.tid, 'Planner changed direction', alive=lambda pid: running[0], signal_fn=stop)
            pool.return_value.release.assert_called_once_with(self.tid, {})
        self.assertEqual(g('status', '--porcelain', cwd=self.repo).stdout, before)
        self.assertEqual((self.repo / 'change.txt').read_text(), 'unfinished\n')
        self.assertEqual(g('rev-parse', 'HEAD', cwd=self.repo).stdout.strip(), self.sha)
        self.assertIn('change.txt', partial['files_changed'])
        self.assertIn({'sha': self.sha, 'subject': 'partial work'}, partial['commits'])
        self.assertIn('change.txt', partial['diff_stat'])
        task = bus.get(self.tid)
        self.assertEqual((task['status'], task['hold_reason'], task['pid']), ('held', 'cancelled', None))
        self.assertEqual(task['result']['partial'], partial)
        self.assertEqual(task['result']['provenance'], ['worker_partial'])
        self.assertEqual(registry.get(self.tid)['status'], 'cancelled')
        self.assertEqual(registry.events(self.tid)[0]['data']['cancel_reason'], 'Planner changed direction')
        registry.finish(self.tid, 'failed', 'process_error')
        self.assertEqual(registry.get(self.tid)['status'], 'cancelled')

    def test_cancel_and_preserve_partial_create_evidence_objects(self):
        self.task.update(prompt="not evidence", transcript="not evidence")
        with mock.patch.object(control.pool, 'Pool') as configured:
            configured.return_value.cfg = {"context_router": {"partial_max_tokens": 800}}
            configured.return_value.release.return_value = None
            control.cancel(self.tid, 'replace', alive=lambda pid: False)
        item = control.evidence.EvidencePool("example").by_type("worker_partial")[-1]
        self.assertEqual((item.commit, item.scope), (self.sha, ["change.txt"]))
        self.assertIn("file: change.txt", item.content)
        self.assertIn("partial work", item.content)
        self.assertNotIn("not evidence", item.content)

    def test_partial_result_contains_only_observable_evidence(self):
        self.task.update(prompt='private input', packet='private input', transcript='private output', reasoning='private output')
        (self.repo / 'tests-green.log').write_text('tests-green: OK 3\n')
        (self.repo / 'conversation.log').write_text('private input\n')
        partial = control.partial_result(self.task)
        self.assertEqual(set(partial), {'files_changed', 'diff_stat', 'commits', 'tests_run', 'errors', 'files_inspected', 'unresolved'})
        self.assertEqual(partial['tests_run'], ['tests-green: OK 3'])
        self.assertEqual(partial['errors'], [])
        self.assertEqual(partial['files_inspected'], [])
        for word in ('prompt', 'packet', 'transcript', 'reasoning', 'private input', 'private output'):
            self.assertNotIn(word, json.dumps(partial))

    def test_cancel_escalates_to_kill_after_grace(self):
        elapsed = [0.0]
        running = [True]
        signals = []
        def sleep(seconds):
            elapsed[0] += seconds
        def send(pid, sig):
            signals.append(sig)
            if sig == signal.SIGKILL:
                self.assertGreaterEqual(elapsed[0], 0.3)
                running[0] = False
        with mock.patch.object(control.pool, 'Pool'):
            control.cancel(self.tid, 'stop', grace_s=0.3, sleep=sleep,
                           alive=lambda pid: running[0], signal_fn=send)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertAlmostEqual(elapsed[0], 0.3)

    def test_partial_result_truncates_to_bus_cap(self):
        partial = {name: ['large observation ' * 500] * 100 for name in ('files_changed', 'tests_run', 'errors', 'files_inspected', 'commits', 'unresolved')}
        partial['diff_stat'] = 'diff ' * 3000
        result = control._bounded_result(partial, 'stop', 'planner')
        self.assertLessEqual(len(json.dumps(result)), bus.MAX_RESULT_CHARS)

    def test_missing_process_and_signal_race_are_safe(self):
        with mock.patch.object(control.pool, 'Pool'), mock.patch.object(control, 'partial_result', return_value={'unresolved': []}):
            control.cancel(self.tid, 'already exited', alive=lambda pid: True,
                           signal_fn=mock.Mock(side_effect=ProcessLookupError))
        self.assertEqual(bus.get(self.tid)['hold_reason'], 'cancelled')

    def test_signal_permission_failure_does_not_release_capacity(self):
        with mock.patch.object(control.pool, 'Pool') as pool, self.assertRaises(PermissionError):
            control.cancel(self.tid, 'stop', alive=lambda pid: True,
                           signal_fn=mock.Mock(side_effect=PermissionError))
        pool.assert_not_called()
        self.assertEqual(registry.get(self.tid)['status'], 'cancelling')


class Steering(unittest.TestCase):
    def setUp(self):
        WorkerControl.setUp(self)
        registry.upsert(self.tid, thread="thread-example")
        self.path = registry._path(self.tid).with_suffix(".steering.jsonl")
        self.addCleanup(self.path.unlink, missing_ok=True)

    def test_steer_records_before_delivery_and_never_edits_spec(self):
        from orchestrator import executor
        before = json.dumps({k: bus.get(self.tid)[k] for k in ("spec", "acceptance", "scope")}).encode()
        bus.update(self.tid, reason="existing reason")
        def deliver(task, thread, prompt):
            row = json.loads(self.path.read_text())
            self.assertEqual(row["message"], "Inspect the failing check")
            self.assertEqual(row["reason"], "new evidence")
            self.assertEqual(row["source"], "planner")
            self.assertEqual(row["task"], self.tid)
            event = registry.events(self.tid)[-1]
            self.assertEqual(event["kind"], "steer")
            self.assertEqual(event["data"]["message_chars"], len(row["message"]))
            self.assertNotIn(row["message"], registry._path(self.tid, events=True).read_text())
            self.assertEqual(task["packet_meta"]["steering_count"], 1)
            self.assertEqual(task["pipeline"]["steer_epoch"], 2)
            return {"status": "running"}
        with mock.patch.object(executor, "steer_resume", side_effect=deliver), \
                mock.patch.object(control, "_terminate") as terminate:
            control.steer(self.tid, "Inspect the failing check", reason="new evidence")
        terminate.assert_called_once()
        after = bus.get(self.tid)
        self.assertEqual(before, json.dumps({k: after[k] for k in ("spec", "acceptance", "scope")}).encode())
        self.assertEqual(after["reason"], "existing reason")
        self.assertEqual(after["status"], "running")
        self.assertTrue(any(e.get("pipeline", {}).get("steer_epoch") == 2 for e in after["events"]))
        self.assertEqual(registry.events(self.tid)[-1]["kind"], "steered")

    def test_steer_codex_resumes_thread_with_message(self):
        from orchestrator import executor
        with mock.patch.object(executor, "steer_resume", return_value={}) as resume:
            control.steer(self.tid, "Check evidence", reason="new evidence", alive=lambda pid: False)
        task, thread, prompt = resume.call_args.args
        self.assertEqual(thread, "thread-example")
        self.assertEqual(prompt, "Steering from planner (new evidence):\nCheck evidence\nThe original task contract is unchanged.")
        self.assertEqual(task["_launch_epoch"], 2)

    def test_steer_claude_resumes_session(self):
        from orchestrator import spawn
        registry.upsert(self.tid, provider="claude", account="A", model="model", tools=["Read", "Bash(rg *)"])
        bus.update(self.tid, packet_meta={"session_id": "session-example"})
        process = mock.Mock(pid=43211, returncode=0)
        def communicate(timeout):
            self.assertEqual(registry.get(self.tid)["status"], "running")
            self.assertEqual(registry.events(self.tid)[-1]["kind"], "steered")
            return json.dumps({"result": "done", "usage": {}}), ""
        process.communicate.side_effect = communicate
        with mock.patch.object(spawn.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(spawn, "trust_workspace"), \
                mock.patch.object(spawn, "secrets_for_role", return_value={}), \
                mock.patch.object(spawn.shutil, "which", return_value="claude"):
            control.steer(self.tid, "Check evidence", reason="new evidence", alive=lambda pid: False)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--resume") + 1], "session-example")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Bash(rg *)")
        self.assertEqual(registry.get(self.tid)["epoch"], 2)
        self.assertEqual(bus.get(self.tid)["status"], "done")

    def test_steer_refuses_without_session_or_thread(self):
        for provider, error in (("codex", "thread"), ("claude", "session")):
            registry.upsert(self.tid, provider=provider, thread=None)
            with mock.patch.object(control, "_terminate") as terminate, self.assertRaisesRegex(ValueError, error):
                control.steer(self.tid, "Check evidence", reason="new evidence")
            terminate.assert_not_called()
            self.assertEqual(registry.get(self.tid)["status"], "running")
            self.assertFalse(self.path.exists())

    def test_steer_codex_uses_steer_resume_not_reply(self):
        from orchestrator import executor
        bus.update(self.tid, rounds=3)
        process = mock.Mock(pid=43211, returncode=0)
        process.communicate.return_value = ('{"type":"item.completed","item":{"type":"agent_message","text":"done"}}', '')
        with mock.patch.object(executor, "reply") as reply, \
                mock.patch.object(executor, "resume_plan") as plan, \
                mock.patch.object(executor.subprocess, "Popen", return_value=process) as popen:
            control.steer(self.tid, "Check evidence", reason="new evidence", alive=lambda pid: False)
        reply.assert_not_called()
        plan.assert_not_called()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:4], ["codex", "exec", "resume", "thread-example"])
        self.assertIn("Steering from planner (new evidence):\nCheck evidence\nThe original task contract is unchanged.", argv)
        self.assertNotIn("-C", argv)
        self.assertEqual(str(popen.call_args.kwargs["cwd"]), str(self.repo))
        self.assertEqual(bus.get(self.tid)["rounds"], 3)

    def test_release_if_current_and_stale_thread_bus_writes_skipped(self):
        from orchestrator import daemon
        pool = mock.Mock()
        def interrupted(*args, **kwargs):
            registry.event(self.tid, "steer", epoch=2, status="running")
            bus.update(self.tid, pipeline={"steer_epoch": 2})
            return {"status": "failed", "reason": "interrupted"}
        with mock.patch.object(daemon.executor, "start", side_effect=interrupted), \
                mock.patch.object(daemon.jev_route, "shadow_context", return_value=None), \
                mock.patch.object(bus, "post_result") as post:
            daemon._dispatch_worker(self.tid, "prompt")
        post.assert_not_called()
        self.assertEqual(bus.get(self.tid)["status"], "running")
        self.assertFalse(control.release_if_current(self.tid, 1, pool))
        pool.release.assert_not_called()
        self.assertTrue(control.release_if_current(self.tid, 2, pool))
        pool.release.assert_called_once_with(self.tid, {})


    def test_codex_cleanup_and_result_publication_use_launch_epoch(self):
        from orchestrator import executor
        bus.update(self.tid, assigned_to="codex", executor="astra")
        task = bus.get(self.tid)
        process = mock.Mock(pid=43211, returncode=0)
        def communicate(timeout):
            registry.event(self.tid, "steer", epoch=2, status="running")
            bus.update(self.tid, pipeline={"steer_epoch": 2})
            return '{"type":"thread.started","thread_id":"old-thread"}', ''
        process.communicate.side_effect = communicate
        with mock.patch.object(executor.subprocess, "Popen", return_value=process):
            result = executor._run(executor.Pool(), task, ["prompt"], self.repo, 10)
        self.assertEqual(result["status"], "superseded")
        self.assertEqual(registry.get(self.tid)["epoch"], 2)
        self.assertEqual(registry.get(self.tid)["status"], "running")
        with mock.patch.object(bus, "post_result") as post:
            posted, reason = executor.post_tool_result(self.tid,
                {"status": "done", "message": "commit " + self.sha, "epoch": 1})
        self.assertFalse(posted)
        self.assertIn("superseded", reason)
        post.assert_not_called()
