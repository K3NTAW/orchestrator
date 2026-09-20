import json
import os
import subprocess
import sys
from copy import deepcopy

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from orchestrator import baseline, scorecard


@contextmanager
def state():
    with tempfile.TemporaryDirectory() as directory:
        yield make_state(Path(directory))


def make_state(tmp_path):
    root = tmp_path / '.orchestrator'
    (root / 'tasks').mkdir(parents=True)
    (root / 'runs').mkdir()
    task = {'id': 'T-0001', 'role': 'execute', 'parent': 'G-0001', 'executor': 'test',
            'complexity': 2, 'merged_into': 'main', 'created_at': 100,
            'accepted_at': 200, 'pipeline': {'first_green_at': 150, 'gate_attempts': 1, 'gate_reds': 0}}
    (root / 'tasks' / 'T-0001.json').write_text(json.dumps(task))
    rows = [{'task': 'T-0001', 'role': 'execute', 'ts': 140, 'input_tokens': 100,
             'output_tokens': 20, 'usd': 2, 'turns': 3, 'client_version': 'client-1',
             'policy_version': 'policy-1'},
            {'role': 'memory', 'ts': 160, 'input_tokens': 10}]
    (root / 'runs' / 'usage.jsonl').write_text('\n'.join(map(json.dumps, rows)) + '\n')
    (root / 'pool.toml').write_text('''[jev]
enabled = false
mode = "shadow"
model = "test"
votes = 2
sample = 0.1
[review]
direct_merge_max = 3
security_paths = ["auth/*"]
[planner_routes]
enabled = true
tiers = ["sonnet", "opus"]
[[executors]]
id = "test"
model = "test-model"
complexity_min = 1
complexity_max = 3
''')
    return root


def test_save_writes_metrics_and_fingerprint():
    with state() as root:
        path = baseline.save('phase-h-code', root)
        saved = baseline.load('phase-h-code', root)
        assert path == root / 'baselines' / 'phase-h-code.json'
        assert set(saved['efficiency']) == {'all', 'goal', 'executor', 'band', 'class', 'role', 'routing', 'reviews'}
        for by in baseline.GROUPS:
            assert saved['efficiency'][by or 'all'] == scorecard.efficiency(root, by=by)
        assert saved['metrics']['tokens_per_accepted_task'] == 120
        assert saved['window']['row_count'] == 2
        fp = saved['fingerprint']
        assert len(fp['pool_toml_sha256']) == 64
        assert fp['jev']['mode'] == 'shadow'
        assert fp['review']['security_paths_count'] == 1
        assert fp['executors']['test']['model'] == 'test-model'
        assert fp['planner_routes']['enabled'] is True
        assert fp['client_version'] == 'client-1'
        assert fp['policy_version'] == 'policy-1'
        assert fp['orchestrator_version']
        assert baseline.list_labels(root) == ['phase-h-code']
        baseline.save('filtered', root, since='1970-01-01T00:02:30Z')
        filtered = baseline.load('filtered', root)
        assert filtered['window']['row_count'] == 1
        assert filtered['efficiency']['all']['tokens'] == 10
        assert scorecard.efficiency(root)['tokens'] == 130


def test_label_validation_rejects_paths():
    with state() as root:
        for label in ('', '../escape', '/tmp/escape', 'a/b', 'a\\b', 'a' * 65, 'a\n', 'é'):
            with unittest.TestCase().assertRaises(ValueError):
                baseline.save(label, root)
            with unittest.TestCase().assertRaises(ValueError):
                baseline.load(label, root)
        for label in ('.', '..', 'phase-H_1.0', 'a' * 64):
            assert baseline.save(label, root).parent == root / 'baselines'


def snapshots():
    metrics = dict.fromkeys((*baseline.PRIMARY, *baseline.NON_INFERIORITY), 1.0)
    a = {'metrics': metrics, 'fingerprint': {'jev': {'mode': 'shadow'}}}
    return a, deepcopy(a)


def test_compare_deltas_and_non_inferiority_flags():
    a, b = snapshots()
    b['metrics']['tokens_per_accepted_task'] = 0.75
    result = baseline.compare(a, b)
    metric = result['metrics']['tokens_per_accepted_task']
    assert (metric['absolute'], metric['percent'], metric['flag']) == (-0.25, -25, 'better')
    assert result['non_inferior'] == 'yes'
    b['metrics']['first_pass_rate'] = 0.9
    b['metrics']['fix_round_rate'] = 1.1
    result = baseline.compare(a, b)
    assert result['metrics']['first_pass_rate']['flag'] == 'worse'
    assert result['metrics']['fix_round_rate']['flag'] == 'worse'
    assert result['non_inferior'] == 'no'
    text = baseline.format_comparison(result)
    assert 'first_pass_rate*' in text
    assert text.endswith('non-inferior: no')


def test_compare_handles_none_metrics():
    a, b = snapshots()
    for key in a['metrics']:
        a['metrics'][key] = None
    result = baseline.compare(a, b)
    assert result['non_inferior'] == 'undefined'
    assert all(r['flag'] == 'undefined' and r['absolute'] is None and r['percent'] is None
               for r in result['metrics'].values())
    a['metrics']['tokens_per_accepted_task'] = 0
    assert baseline.compare(a, b)['metrics']['tokens_per_accepted_task']['percent'] is None


def test_fingerprint_diff_lists_changed_keys():
    a, b = snapshots()
    b['fingerprint']['jev']['mode'] = 'active'
    assert baseline.compare(a, b)['fingerprint_diff'] == ['jev.mode']
    b['fingerprint']['new'] = None
    assert baseline.compare(a, b)['fingerprint_diff'] == ['jev.mode', 'new']


def test_baseline_includes_routing_eval_and_compare_reports_it():
    with state() as root:
        with (root / 'runs' / 'usage.jsonl').open('a') as handle:
            handle.write(json.dumps({'task': 'T-0001', 'role': 'jev_route', 'baseline': 'test',
                                     'hypothetical': 'other', 'signals': {'fit': .8}}) + '\n')
        baseline.save('before', root)
        baseline.save('after', root)
        saved = baseline.load('after', root)
        assert saved['efficiency']['routing']['groups']['disagree']['n'] == 1
        result = baseline.compare('before', 'after', root)
        assert result['routing']['after_verdict'] == 'insufficient'
        assert 'routing disagree deltas:' in baseline.format_comparison(result)


def test_baseline_includes_review_quality_and_compare_flags_drops():
    with state() as root:
        review = {'id': 'T-review', 'role': 'review', 'status': 'done', 'inputs': ['T-0001'],
                  'result': {'verdict': 'request_changes', 'comments': [
                      {'path': 'x.py', 'line': 1, 'issue': 'defect', 'severity': 'high'}]}}
        (root / 'tasks' / 'T-review.json').write_text(json.dumps(review))
        baseline.save('review-before', root)
        baseline.save('review-after', root)
        after = baseline.load('review-after', root)
        assert set(after['efficiency']['reviews']) == {'role', 'packet_version'}
        before = baseline.load('review-before', root)
        before['efficiency']['reviews']['role']['general']['findings_per_review']['mean'] = 2
        result = baseline.compare(before, after)
        assert result['reviews']['role']['general']['findings_per_review']['flag'] == 'worse'
        assert result['non_inferior'] == 'no'


def test_cli_baseline_save_show_list_compare():
    with state() as root:
        def cli(*args):
            result = subprocess.run([sys.executable, '-m', 'orchestrator.cli', 'baseline', *args],
                                    env={**os.environ, 'ORCH_ROOT': str(root.parent)},
                                    text=True, capture_output=True)
            assert result.returncode == 0, result.stderr
            return result.stdout
        assert 'before.json' in cli('save', 'before')
        assert 'after.json' in cli('save', 'after', '--since', '1970-01-01T00:00:00Z')
        assert json.loads(cli('show', 'before', '--json'))['efficiency']['all']['tokens'] == 130
        assert 'group\t' in cli('show', 'before')
        assert 'before\t' in cli('list') and 'after\t' in cli('list')
        assert 'non-inferior: undefined' in cli('compare', 'before', 'after')
        assert json.loads(cli('compare', 'before', 'after', '--json'))['metrics']


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(value) for name, value in globals().items()
                              if name.startswith("test_") and callable(value))


def test_baseline_carries_parallelism_block():
    with state() as root:
        baseline.save('parallel', root)
        saved = baseline.load('parallel', root)
        card = scorecard.parallelism(root)
        assert saved['parallelism'] == {k: card[k] for k in ('totals', 'skip_reasons', 'waves')}
        after = deepcopy(saved)
        after['parallelism']['totals']['merge_conflicts'] += 2
        result = baseline.compare(saved, after)
        assert result['parallelism']['totals.merge_conflicts']['absolute'] == 2
        assert 'parallelism totals.merge_conflicts' in baseline.format_comparison(result)
        del saved['parallelism']
        result = baseline.compare(saved, after)
        assert result['parallelism']['totals.merge_conflicts']['absolute'] is None
        assert 'undefined' in baseline.format_comparison(result)
