"""Frozen Phase I efficiency measurements and None-safe baseline comparisons."""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import ROOT, STATE, scorecard

GROUPS = (None, 'goal', 'executor', 'band', 'class', 'role')
PRIMARY = ('tokens_per_accepted_task', 'usd_per_accepted_task',
           'tokens_per_accepted_goal', 'usd_per_accepted_goal', 'tokens_to_first_green',
           'calls_per_accepted_task', 'turns_per_accepted_task', 'pipeline_amplification')
NON_INFERIORITY = ('first_pass_rate', 'fix_round_rate', 'avg_fix_rounds',
                  'gate_success_share', 'review_request_changes_rate')
HIGHER_BETTER = {'first_pass_rate', 'gate_success_share'}


def _path(label, root):
    if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', label):
        raise ValueError('label must match [A-Za-z0-9._-]{1,64}')
    return Path(root) / 'baselines' / (label + '.json')


def load(label, root=STATE):
    return json.loads(_path(label, root).read_text())


def list_labels(root=STATE):
    return sorted(p.stem for p in (Path(root) / 'baselines').glob('*.json')
                  if re.fullmatch(r'[A-Za-z0-9._-]{1,64}', p.stem))


def _tasks(root):
    result = {}
    for path in (root / 'tasks').glob('*.json'):
        try:
            row = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        result[row.get('id', path.stem)] = row
    return result


def _fingerprint(root, rows):
    pool = root / 'pool.toml'
    raw = pool.read_bytes() if pool.exists() else None
    cfg = tomllib.loads(raw.decode()) if raw is not None else {}
    jev = dict(cfg.get('jev', {}))
    if 'mode' not in jev and 'gate_mode' in jev:
        jev['mode'] = jev['gate_mode']
    if 'sample' not in jev and 'sample_rate' in jev:
        jev['sample'] = jev['sample_rate']
    review = dict(cfg.get('review', {}))
    review['security_paths_count'] = len(review.get('security_paths', []))
    try:
        from .spawn import packet_meta
        packet_version = packet_meta({'id': 'baseline', 'spec': '', 'acceptance': [],
                                      'scope': [], 'constraints': {}}, ROOT)['hash']
    except Exception:
        packet_version = None
    try:
        package_version = version('orchestrator')
    except PackageNotFoundError:
        package_version = None
    try:
        head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        head = None
    def recorded(key):
        values = sorted({r[key] for r in rows if r.get(key) is not None})
        return values[0] if len(values) == 1 else values or None
    return {'pool_toml_sha256': hashlib.sha256(raw).hexdigest() if raw is not None else None,
            'jev': {k: jev.get(k, False if k == 'enabled' else None)
                    for k in ('enabled', 'model', 'votes', 'sample', *(['mode'] if 'mode' in jev else []))},
            'review': review,
            'executors': {r['id']: {'model': r.get('model'),
                                   'complexity_min': r.get('complexity_min', 1),
                                   'complexity_max': r.get('complexity_max', 10)}
                          for r in cfg.get('executors', []) if r.get('enabled', True)},
            'planner_routes': cfg.get('planner_routes', cfg.get('planner', {}).get('routes', {})),
            'packet_version': packet_version, 'client_version': recorded('client_version'),
            'policy_version': recorded('policy_version'), 'git_head': head,
            'orchestrator_version': package_version}


def _metrics(card, tasks):
    accepted = list(card.get('tasks', {}).values())
    def mean(key):
        values = [t.get(key) for t in accepted]
        return sum(values) / len(values) if values and all(v is not None for v in values) else None
    result = {key: card.get(key) for key in (*PRIMARY, *NON_INFERIORITY)}
    for key in ('tokens', 'usd', 'calls', 'turns'):
        result[key + '_per_accepted_task'] = mean(key)
    result['tokens_to_first_green'] = mean('tokens_to_first_green')
    gates = []
    verdicts = []
    for task in tasks.values():
        pipeline = task.get('pipeline') or {}
        attempts = pipeline.get('gate_attempts', task.get('gate_attempts'))
        reds = pipeline.get('gate_reds', task.get('gate_reds'))
        if attempts is not None and reds is not None:
            gates.append((attempts, reds))
        verdict = task.get('review_verdict')
        if task.get('role') == 'review':
            verdict = (task.get('result') or {}).get('verdict', verdict)
            if verdict in ('approve', 'request_changes'):
                verdicts.append(verdict)
    attempts = sum(a for a, _ in gates)
    result['gate_success_share'] = sum(a - r for a, r in gates) / attempts if attempts else None
    result['review_request_changes_rate'] = verdicts.count('request_changes') / len(verdicts) if verdicts else None
    return result


def save(label, root=STATE, since=None):
    """Save a usage window; --since excludes undated rows and out-of-window acceptances."""
    root = Path(root)
    path = _path(label, root)
    now = datetime.now(timezone.utc)
    start = None
    if since is not None:
        parsed = datetime.fromisoformat(since.replace('Z', '+00:00'))
        start = parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
        if start > now.timestamp():
            raise ValueError('since must not be in the future')
    with tempfile.TemporaryDirectory(prefix='orchestrator-baseline-') as directory:
        measured = root
        if start is not None:
            measured = Path(directory)
            # Retain task/goal metadata for legacy attribution; filter usage without mutating live logs.
            for name in ('tasks', 'goals', 'runs'):
                if (root / name).exists():
                    shutil.copytree(root / name, measured / name)
            for source in root.glob('*.json'):
                shutil.copy2(source, measured / source.name)
            for log in (measured / 'runs').rglob('*.jsonl'):
                kept = []
                for line in log.read_text().splitlines():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    stamp = scorecard._stamp(row.get('ts'))
                    if stamp is not None and start <= stamp <= now.timestamp():
                        kept.append(line)
                log.write_text('\n'.join(kept) + '\n')
            for file in (measured / 'tasks').glob('*.json'):
                try:
                    task = json.loads(file.read_text())
                except json.JSONDecodeError:
                    continue
                stamp = scorecard._stamp(task.get('accepted_at'))
                if stamp is None or not start <= stamp <= now.timestamp():
                    task.pop('merged_into', None)
                    file.write_text(json.dumps(task))
        tasks = _tasks(measured)
        rows, malformed = scorecard._efficiency_rows(measured, tasks)
        cards = {by or 'all': scorecard.efficiency(measured, by=by) for by in GROUPS}
        counts = dict.fromkeys(scorecard.EFFICIENCY_BUCKETS, 0)
        for row in rows:
            bucket = row.get('bucket') or 'other'
            counts[bucket] = counts.get(bucket, 0) + 1
        cards['routing'] = scorecard.routing_eval(measured)
        snapshot = {'saved_at': now.isoformat(),
                    'window': {'since': since, 'until': now.isoformat(), 'rows_by_bucket': counts,
                               'row_count': len(rows), 'malformed_lines': malformed},
                    'efficiency': cards, 'metrics': _metrics(cards['all'], tasks),
                    'fingerprint': _fingerprint(root, rows)}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement prevents readers from seeing a partially written snapshot.
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(snapshot, handle, indent=2, default=str)
        handle.write('\n')
    temporary.replace(path)
    return path


def _changed(a, b, prefix=''):
    if isinstance(a, dict) and isinstance(b, dict):
        keys = []
        for key in sorted(a.keys() | b.keys()):
            name = f'{prefix}.{key}' if prefix else key
            if key not in a or key not in b:
                keys.append(name)
            else:
                keys.extend(_changed(a[key], b[key], name))
        return keys
    return [prefix] if a != b else []


def compare(a, b, root=STATE):
    """Compare snapshots (or labels), with positive deltas meaning b minus a."""
    a = load(a, root) if isinstance(a, str) else a
    b = load(b, root) if isinstance(b, str) else b
    before = a.get('metrics') or _metrics(a['efficiency']['all'], {})
    after = b.get('metrics') or _metrics(b['efficiency']['all'], {})
    metrics = {}
    for key in (*PRIMARY, *NON_INFERIORITY):
        x, y = before.get(key), after.get(key)
        delta = y - x if x is not None and y is not None else None
        flag = ('undefined' if delta is None else 'equal' if delta == 0 else
                'better' if (delta > 0) == (key in HIGHER_BETTER) else 'worse')
        metrics[key] = {'before': x, 'after': y, 'absolute': delta,
                        'percent': delta / abs(x) * 100 if delta is not None and x else None,
                        'flag': flag, 'non_inferiority': key in NON_INFERIORITY}
    flags = [metrics[k]['flag'] for k in NON_INFERIORITY]
    routing_before = (a.get('efficiency') or {}).get('routing')
    routing_after = (b.get('efficiency') or {}).get('routing')
    routing = {'before_verdict': routing_before.get('evidence_verdict') if routing_before else None,
               'after_verdict': routing_after.get('evidence_verdict') if routing_after else None,
               'groups': {}}
    for name in ('agree', 'disagree'):
        before_group = ((routing_before or {}).get('groups') or {}).get(name, {})
        after_group = ((routing_after or {}).get('groups') or {}).get(name, {})
        routing['groups'][name] = {}
        for key in ('accepted_share', 'first_pass_rate', 'fix_round_rate', 'avg_fix_rounds',
                    'median_tokens_to_accepted', 'median_cost_to_accepted', 'gate_red_share',
                    'review_request_changes_share'):
            x, y = before_group.get(key), after_group.get(key)
            routing['groups'][name][key] = {'before': x, 'after': y,
                                            'absolute': y - x if x is not None and y is not None else None}
    return {'metrics': metrics, 'routing': routing,
            'fingerprint_diff': _changed(a.get('fingerprint', {}), b.get('fingerprint', {})),
            'non_inferior': 'no' if 'worse' in flags else 'undefined' if 'undefined' in flags else 'yes'}


def format_comparison(result):
    def cell(value):
        return 'n/a' if value is None else f'{value:.4g}'
    lines = ['metric\tbefore\tafter\tdelta\tpercent\tflag']
    for key, row in result['metrics'].items():
        mark = '*' if row['non_inferiority'] and row['flag'] == 'worse' else ''
        lines.append(key + mark + '\t' + '\t'.join(cell(row[k]) for k in ('before', 'after', 'absolute', 'percent')) + '\t' + row['flag'])
    lines.append('fingerprint changed: ' + (', '.join(result['fingerprint_diff']) or 'none'))
    routing = result.get('routing', {})
    for name, metrics in routing.get('groups', {}).items():
        values = ', '.join(f"{key}={cell(row['absolute'])}" for key, row in metrics.items())
        lines.append(f"routing {name} deltas: {values}")
    if routing:
        lines.append('routing evidence verdict: ' + str(routing.get('after_verdict')))
    lines.append('non-inferior: ' + result['non_inferior'])
    return '\n'.join(lines)
