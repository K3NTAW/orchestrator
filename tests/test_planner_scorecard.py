import _harness
import builtins
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orchestrator import cli, planner_scorecard as card, planner_telemetry as telemetry, scorecard


class PlannerScorecardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'tasks').mkdir()
        self.interactive = mock.patch.object(telemetry, 'interactive_by_goal', return_value=[])
        self.interactive.start()
        self.addCleanup(self.interactive.stop)

    def launch(self, lid, model='fable', decision_type='initial', goal=None, started=10, **changes):
        values = dict(launch_id=lid, goal_id=goal or lid, event='goal', decision_type=decision_type,
                      kind='goal', payload_keys=[], model=model, tier=model, account='A', complexity=5,
                      band='medium', task_class='architectural', architectural=True, route='direct',
                      route_reason='test', mode='shadow', state_version='1', packet_chars=0,
                      started_at=started)
        values.update(changes)
        telemetry.record_launch(root=self.root, **values)
        telemetry.record_usage(lid, input_tokens=100, usd=2, latency_s=3, outcome='done', root=self.root)
        telemetry.record_materiality(lid, {'tasks': {}}, {'tasks': {'new': {}}}, root=self.root)
        return values

    def task(self, tid, goal, created=11, **changes):
        value = dict(id=tid, parent=goal, role='execute', status='done', created_at=created,
                     constraints={}, pipeline={}, merged_into='main')
        value.update(changes)
        (self.root / 'tasks' / f'{tid}.json').write_text(json.dumps(value))
        return value

    def evidence(self, **changes):
        kwargs = dict(root=self.root, cfg={'models': {'planner': 'fable'}}, min_samples=10)
        kwargs.update(changes)
        return card.class_evidence('opus', 'initial', 'medium', 'architectural', True, **kwargs)

    def population(self):
        for model in ('fable', 'opus'):
            for i in range(10):
                lid = f'{model}-{i}'
                self.launch(lid, model=model)
                self.task(lid, lid)
                self.task('review-' + lid, lid, role='review', inputs=[lid],
                          result={'verdict': 'approve'})

    def test_groups_by_model_type_band_class_arch(self):
        for model in ('fable', 'opus'):
            for decision in ('initial', 'architectural_replan'):
                lid = model + decision
                row = self.launch(lid, model=model, decision_type=decision)
                self.task(lid, lid, merged_into='main' if model == 'fable' else None)
                self.assertEqual(card.group_key(row), f'{model}|{decision}|medium|architectural|arch')
        result = card.build(self.root, cfg={}, min_samples=1)
        self.assertEqual((result['n'], len(result['groups'])), (4, 4))
        for key, metrics in result['groups'].items():
            self.assertEqual(metrics['n'], 1)
            self.assertEqual(metrics['material_rate'], 1)
            self.assertEqual(metrics['downstream_accepted_rate'], float(key.startswith('fable|')))
            self.assertEqual((metrics['tokens_mean'], metrics['usd_mean'], metrics['latency_mean']), (100, 2, 3))
            self.assertIsNone(metrics['human_intervention_rate'])
        plain = self.launch('plain', architectural=False, band='hard', task_class='bugfix')
        self.assertEqual(card.group_key(plain), 'fable|initial|hard|bugfix|plain')

    def test_ownership_is_exclusive_terminal_roots_and_fix_rounds(self):
        a = self.launch('A', goal='G', started=10)
        b = self.launch('B', model='opus', goal='G', started=20, kind='held', payload_keys=['T1'])
        tasks = {}
        tasks['T1'] = self.task('T1', 'G', created=11)
        tasks['T2'] = self.task('T2', 'G', created=21, constraints={'fix_round_for': 'T1'})
        tasks['T3'] = self.task('T3', 'G', created=22, constraints={'fix_round_for': 'T2'})
        tasks['flight'] = self.task('flight', 'G', created=12, merged_into=None, status='running')
        tasks['early'] = self.task('early', 'G', created=1)
        owned = card._ownership([a, b], tasks)
        self.assertEqual(owned, {'A': {'T1', 'T3', 'flight'}, 'B': {'T2'}})
        self.assertFalse(owned['A'] & owned['B'])
        metrics = card.build(self.root, cfg={})['groups'][card.group_key(a)]
        self.assertEqual(metrics['terminal_roots'], 1)
        self.assertEqual(metrics['downstream_accepted_rate'], 0.5)
        self.assertEqual(metrics['first_pass_rate'], 0)
        self.assertEqual(metrics['fix_round_rate'], 1)
        self.assertEqual(metrics['avg_fix_rounds'], 2)
        self.task('failed', 'G', created=13, status='failed', merged_into=None)
        self.task('green', 'G', created=14, merged_into=None, pipeline={'first_green_at': 15})
        metrics = card.build(self.root, cfg={})['groups'][card.group_key(a)]
        self.assertEqual(metrics['terminal_roots'], 3)
        self.assertAlmostEqual(metrics['first_pass_rate'], 2 / 3)
        self.assertAlmostEqual(metrics['fix_round_rate'], 1 / 3)

    def test_class_evidence_noninferior_equivalent_worse_and_insufficient(self):
        self.population()
        equal = self.evidence()
        self.assertIs(equal['noninferior'], True)
        self.assertEqual(equal['failed'], [])
        self.assertTrue(all(delta == 0 for delta in equal['deltas'].values()))
        self.assertIsNone(self.evidence(min_samples=11)['noninferior'])
        telemetry.record_usage('opus-0', input_tokens=10**9, usd=0, outcome='done', root=self.root)
        self.assertEqual(self.evidence(), equal)
        for i in range(3):
            self.task(f'opus-{i}', f'opus-{i}', pipeline={'lineage_fix_rounds': 1})
        worse = self.evidence()
        self.assertIs(worse['noninferior'], False)
        self.assertIn('first_pass_rate', worse['failed'])
        self.assertAlmostEqual(worse['deltas']['first_pass_rate'], -0.3)
        for i in range(3):
            self.task(f'opus-{i}', f'opus-{i}')
            self.task(f'review-opus-{i}', f'opus-{i}', role='review', inputs=[f'opus-{i}'],
                      review_verdict='request_changes')
        worse = self.evidence()
        self.assertIs(worse['noninferior'], False)
        self.assertEqual(worse['failed'], ['review_request_changes_rate'])
        self.assertAlmostEqual(worse['deltas']['review_request_changes_rate'], 0.3)
        # Tier fallback selects the baseline within the requested class.
        self.assertEqual(self.evidence(cfg={})['baseline_model'], 'fable')

    def test_reescalation_penalises_class(self):
        self.population()
        for i in range(5):
            self.launch(f'opus-{i}', model='opus', reescalation=True)
        evidence = self.evidence()
        self.assertEqual(evidence['reescalation_rate'], 0.5)
        self.assertIs(evidence['noninferior'], False)
        self.assertEqual(evidence['failed'], ['reescalation'])

    def test_cli_flag_min_samples_source_and_malformed_tolerance(self):
        self.launch('A')
        ledger = self.root / 'runs' / 'sched' / 'planner_invocations.jsonl'
        with ledger.open('a') as stream:
            stream.write('broken json\n')
        configs = [({}, 20), ({'promotion': {'min_samples': 8}}, 8),
                   ({'planner': {'routing': {'min_samples': 3}}, 'promotion': {'min_samples': 8}}, 3),
                   ({'planner': {'routing': {'min_samples': 'bad'}}, 'promotion': {'min_samples': 8}}, 8)]
        for cfg, expected in configs:
            self.assertEqual(card.build(self.root, cfg=cfg)['min_samples'], expected)
            self.assertEqual(card.build(self.root, cfg=cfg, min_samples=2)['min_samples'], 2)
        (self.root / 'pool.toml').write_text('[planner.routing]\nmin_samples = 7\n')
        with mock.patch.object(scorecard, 'STATE', self.root), mock.patch('sys.argv', ['orchestrator', 'scorecard', '--planner-routing', '--json']):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli.main()
        result = json.loads(output.getvalue())
        self.assertTrue(result['read_side_only'])
        self.assertTrue(result['groups'])
        self.assertEqual((result['malformed'], result['min_samples']), (1, 7))
        for flag in ('--planner', '--parallelism'):
            with mock.patch('sys.argv', ['orchestrator', 'scorecard', '--planner-routing', flag]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cli.main()
                self.assertEqual(error.exception.code, 2)
        original = builtins.__import__

        def unavailable(name, globals=None, locals=None, fromlist=(), level=0):
            if 'planner_telemetry' in fromlist:
                raise ImportError('test missing producer')
            return original(name, globals, locals, fromlist, level)

        with mock.patch('builtins.__import__', side_effect=unavailable):
            empty = card.build(self.root, cfg={})
        self.assertEqual(empty['error'], 'planner_telemetry unavailable')
        self.assertEqual(empty['groups'], {})
        self.assertTrue(card.format(empty).startswith('read-side only: rows appear once planner_runs emits invocations'))

    def test_economics_and_optional_signals(self):
        a = self.launch('A', goal='G', started=10)
        self.launch('B', goal='G', started=20, decision_type='architectural_replan')
        self.task('T1', 'G', created=11, pipeline={'gate_reds': 1})
        self.task('T2', 'G', created=21)
        self.task('spec', 'G', role='spec_review', inputs=['T1'], result={'verdict': 'approve'})
        with mock.patch.object(scorecard, 'accepted_goals', return_value=['G']), mock.patch.object(scorecard, 'by_task', return_value={'T1': {'usd': 4}, 'T2': {'usd': 8}}):
            economics = card.economics(self.root)['fable']
        self.assertEqual(economics, {'planner_usd_per_accepted_goal': 4,
                                    'planner_tokens_per_accepted_goal': 200,
                                    'pipeline_usd_after_planning': 6,
                                    'expected_total_cost_after_planning': {'initial': 6, 'architectural_replan': 10}})
        with mock.patch.object(telemetry, 'interactive_by_goal', return_value=[{'goal_id': 'G', 'days': ['1970-01-02']}]):
            metrics = card.build(self.root, cfg={})['groups'][card.group_key(a)]
        self.assertEqual(metrics['human_intervention_rate'], 1)
        self.assertEqual(metrics['replanning_rate'], 1)
        self.assertEqual(metrics['spec_review_approval_rate'], 1)
        self.assertEqual(metrics['gate_red_rate'], 1)
        with mock.patch.object(telemetry, 'interactive_by_goal', side_effect=RuntimeError('offline')):
            self.assertIsNone(card.build(self.root, cfg={})['groups'][card.group_key(a)]['human_intervention_rate'])

    def test_thresholds_and_missing_outcomes(self):
        self.population()
        for i in range(3):
            self.task(f'opus-{i}', f'opus-{i}', pipeline={'gate_reds': 1})
        self.assertEqual(self.evidence()['failed'], ['gate_success'])
        cfg = {'models': {'planner': 'fable'}, 'promotion': {'gate_success_delta': -0.3}}
        self.assertIs(self.evidence(cfg=cfg)['noninferior'], True)
        for i in range(10):
            self.task(f'opus-{i}', f'opus-{i}', merged_into=None, status='running')
        self.assertIsNone(self.evidence()['noninferior'])
        self.assertIn('noninferior=None', card.format_evidence(self.evidence()))


if __name__ == '__main__':
    unittest.main()
