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
