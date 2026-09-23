import _harness

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from orchestrator import decision_log, skill_promotion as promotion, skill_scorecard, skills_registry as registry


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class SkillPromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'
        self.skills = Path(self.temp.name) / 'skills'
        self.source = self.skills / 'scout/example/SKILL.md'
        self.source.parent.mkdir(parents=True)
        self.source.write_text('---\nroles: [scout]\ntriggers: [who calls]\ndepends_on: [dep.py]\n---\nTrace callers.\n')
        self.dep = Path(self.temp.name) / 'dep.py'
        self.dep.write_text('one')
        self.now = time.time()
        with mock.patch.object(registry, '_now', return_value=stamp(self.now - 1000)):
            registry.sync(self.root, self.skills)
        self.id = 'scout/example'
        self.fields(validation={'tests': ['tests/test_skill_scorecard.py'], 'status': 'tested'})
        registry.transition(self.id, 'shadow', 'fixture', self.root)
        self.suite()

    def fields(self, **fields):
        registry._update_record(self.id, fields, self.root)

    def suite(self, age=0, passed=True):
        registry._write_json(self.root / 'skill_eval.json', {'ran_at': stamp(self.now - age), 'suite_passed': passed})

    def evaluate(self, **values):
        row = {'n_with': 20, 'n_without': 20, 'insufficient': False, 'verdict': 'valuable',
               'accepted_tokens_delta': -10, **values}
        with mock.patch.object(skill_scorecard, 'marginal', return_value=[row]):
            return promotion.evaluate_skill(self.id, self.root, {})

    def test_new_version_starts_without_inherited_score(self):
        runs = self.root / 'runs'; runs.mkdir()
        rows = [{'ts': self.now - 900 + i, 'context': {'skills_used': [self.id]}} for i in range(20)]
        rows += [{'ts': self.now - 10, 'context': {'skills_used': [self.id]}}]
        (runs / 'runs.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
        self.source.write_text(self.source.read_text() + 'New version.\n')
        with mock.patch.object(registry, '_now', return_value=stamp(self.now - 20)):
            registry.sync(self.root, self.skills)
        result = promotion.evaluate_skill(self.id, self.root, {})
        self.assertEqual(result['evidence']['marginal'][0]['n_with'], 1)
        self.assertIsNone(result['to'])
        self.source.write_text(self.source.read_text() + 'Third version.\n')
        registry.sync(self.root, self.skills)
        self.assertEqual(promotion.evaluate_skill(self.id, self.root, {})['evidence']['marginal'], [])

    def test_shadow_to_active_requires_samples_and_efficiency(self):
        self.assertEqual(self.evaluate()['to'], 'active')
        self.assertIsNone(self.evaluate(n_with=1)['to'])
        self.assertIsNone(self.evaluate(accepted_tokens_delta=0)['to'])
        self.assertEqual(self.evaluate(verdict='neutral', accepted_tokens_delta=0, fix_rounds_delta=-1)['to'], 'active')
        self.fields(stale=True)
        self.assertIsNone(self.evaluate()['to'])

    def test_demotion_on_harmful_or_costly_or_irrelevant_activation(self):
        registry.transition(self.id, 'active', 'fixture', self.root)
        for verdict in ('harmful', 'costly'):
            self.assertEqual(self.evaluate(verdict=verdict)['to'], 'demoted')
        self.assertIsNone(self.evaluate(verdict='costly', n_with=19)['to'])
        for i in range(20):
            decision_log.record('skill_selection', f'T-{i}', root=self.root, candidates=[self.id],
                                hard_constraints=[], deterministic={}, selected=[self.id], reason='fixture')
            decision_log.outcome(f'T-{i}', 'skill_selection', root=self.root,
                                 skills_used=[] if i < 14 else [self.id])
        result = self.evaluate(verdict='neutral', accepted_tokens_delta=0)
        self.assertEqual(result['to'], 'demoted')
        self.assertEqual(result['evidence']['irrelevant']['rate'], .7)

    def test_stale_dependency_marks_and_demotes_until_revalidated(self):
        registry.transition(self.id, 'active', 'fixture', self.root)
        self.dep.write_text('two')
        registry.sync(self.root, self.skills)
        record = registry.load(self.root)['skills'][self.id]
        self.assertTrue(record['stale'])
        self.assertIn(str(self.dep.resolve()), registry.state(self.id, self.root)['history'][-1]['changed_dependencies'])
        self.assertIsNone(self.evaluate()['to'])
        with mock.patch.object(registry.subprocess, 'run', return_value=mock.Mock(returncode=1)):
            self.assertEqual(registry.revalidate(self.id, self.root)['status'], 'failed')
        self.assertEqual(self.evaluate()['to'], 'shadow')
        self.fields(validation={'status': 'untested', 'tests': ['tests/test_skill_scorecard.py']}, stale_since=stamp(self.now - 86401))
        result = self.evaluate()
        self.assertEqual(result['to'], 'shadow')
        registry.transition(self.id, result['to'], result['reason'], self.root)
        with mock.patch.object(registry.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            self.assertEqual(registry.revalidate(self.id, self.root)['status'], 'tested')
        self.assertFalse(registry.load(self.root)['skills'][self.id]['stale'])
        self.assertEqual(self.evaluate()['to'], 'active')

    def test_promotion_refused_without_recent_passing_suite(self):
        (self.root / 'skill_eval.json').unlink()
        self.assertIsNone(self.evaluate()['to'])
        self.suite(age=7 * 86400 + 1)
        self.assertIsNone(self.evaluate()['to'])
        self.suite(passed=False)
        self.assertIsNone(self.evaluate()['to'])
        self.suite()
        self.assertEqual(self.evaluate()['to'], 'active')

    def test_rollback_restores_previous_state(self):
        result = self.evaluate()
        registry.transition(self.id, result['to'], result['reason'], self.root)
        self.assertEqual(registry.rollback(self.id, self.root)['state'], 'shadow')
        self.assertIn('version', registry.state(self.id, self.root)['history'][-1]['reason'])

    def test_testing_needs_validation_and_demoted_needs_later_version(self):
        registry.transition(self.id, 'testing', 'fixture', self.root)
        self.fields(validation={'status': 'untested'})
        self.assertIsNone(self.evaluate()['to'])
        self.fields(validation={'status': 'tested'})
        self.assertEqual(self.evaluate()['to'], 'shadow')
        registry.transition(self.id, 'shadow', 'fixture', self.root)
        registry.transition(self.id, 'active', 'fixture', self.root)
        registry.transition(self.id, 'demoted', 'fixture', self.root)
        self.assertIsNone(self.evaluate()['to'])
        self.source.write_text(self.source.read_text() + 'Improved version.\n')
        registry.sync(self.root, self.skills)
        self.fields(validation={'status': 'tested'})
        self.assertEqual(self.evaluate()['to'], 'shadow')

    def test_newer_active_specialist_conflict_demotes(self):
        registry.transition(self.id, 'active', 'fixture', self.root)
        other = self.skills / 'scout/new/SKILL.md'
        other.parent.mkdir(); other.write_text('---\nroles: [scout]\n---\nNew skill.\n')
        registry.sync(self.root, self.skills)
        decision_log.record('skill_selection', 'T-conflict', root=self.root,
                            candidates=[self.id, 'scout/new'], selected=['scout/new'],
                            deterministic={}, hard_constraints=[], reason='fixture',
                            extra={'specialist': {'conflicts': [{'kept': 'scout/new', 'dropped': self.id}]}})
        self.assertEqual(self.evaluate()['to'], 'demoted')
