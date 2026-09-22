import _harness
import ast
import importlib
import inspect
import json
import unittest
from unittest.mock import patch

from orchestrator import jev, jev_rank
from test_jev import ENABLED_CFG, FakeResp


class JevBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.payloads = []
        for target, kwargs in (
            ('_cfg', {'return_value': ENABLED_CFG}),
            ('_api_key', {'return_value': 'fake-key'}),
            ('_day_tokens_used', {'return_value': 0}),
            ('_add_day_tokens', {}), ('_log_usage', {}),
            ('undeclared_calls', {'new': 0}), ('refused_calls', {'new': 0}),
        ):
            patcher = patch.object(jev, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(jev.notify, 'notify')
        self.warning = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(jev.urllib.request, 'urlopen', side_effect=self.capture)
        self.transport = patcher.start()
        self.addCleanup(patcher.stop)

    def capture(self, req, timeout=None):
        self.payloads.append(json.loads(req.data.decode()))
        return FakeResp({'answers': {}, 'usage': {'input_tokens': 0}})

    def test_every_call_site_declares_boundary(self):
        for site in ('gate', 'route', 'planner', 'sched', 'points', 'rank'):
            module = importlib.import_module('orchestrator.jev_' + site)
            self.assertIn(site, jev.BOUNDARIES)
            self.assertFalse(jev.BOUNDARIES[site]['raw_source_allowed'])
            source = inspect.getsource(module)
            tree = ast.parse(source)
            checked = 0
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = ast.unparse(node.func)
                if func not in ('jev.ask', 'ask_fn', 'ask'):
                    continue
                checked += 1
                explicit = any(k.arg == 'site' and isinstance(k.value, ast.Constant)
                               and k.value.value == site for k in node.keywords)
                if not explicit and func == 'ask_fn':
                    bindings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                                and any(isinstance(t, ast.Name) and t.id == 'ask_fn' for t in n.targets)]
                    self.assertTrue(any(isinstance(b, ast.Call) and ast.unparse(b.func) == 'jev.bind_site'
                                        and len(b.args) == 1 and isinstance(b.args[0], ast.Constant)
                                        and b.args[0].value == site for b in bindings))
                elif not explicit:
                    # Scheduler preserves injectable two-argument transports by binding
                    # the boundary to its production default instead.
                    self.assertEqual(func, 'ask')
                    default = inspect.signature(module.annotate).parameters['ask'].default
                    self.assertIs(default.func, jev.ask)
                    self.assertEqual(default.keywords['site'], site)
            self.assertGreater(checked, 0, source)

    def test_ask_drops_undeclared_fields(self):
        importlib.import_module('orchestrator.jev_gate')
        state = {'task': {'title': 'ok'}, 'private_payload': 'never send this'}
        self.assertIsNotNone(jev.ask(state, {}, site='gate'))
        self.assertEqual(json.loads(self.payloads[0]['state']), {'task': {'title': 'ok'}})
        self.assertIn('private_payload', state)  # filtering does not mutate callers
        self.assertEqual(jev.undeclared_calls, 1)
        self.warning.assert_called_once()
        self.assertNotIn('private_payload', str(self.warning.call_args))
        self.assertNotIn('never send this', str(self.warning.call_args))

    def test_ask_refuses_sensitive_class(self):
        for data_class in ('SENSITIVE', 'SECRET', 'unknown'):
            self.assertFalse(jev.allowed_for_class(data_class))
            self.assertIsNone(jev.ask('private', {}, site='rank', data_class=data_class))
        self.transport.assert_not_called()
        self.assertEqual(jev.refused_calls, 3)
        self.assertEqual(self.warning.call_count, 3)
        for data_class in ('PUBLIC', 'INTERNAL'):
            self.assertTrue(jev.allowed_for_class(data_class))
        with patch.object(jev, '_cfg', return_value={**ENABLED_CFG, 'max_data_class': 'PUBLIC'}):
            self.assertFalse(jev.allowed_for_class('INTERNAL'))
            self.assertTrue(jev.allowed_for_class('PUBLIC'))
        with patch.object(jev, '_cfg', return_value={**ENABLED_CFG, 'max_data_class': 'invalid'}):
            self.assertFalse(jev.allowed_for_class('PUBLIC'))

    def test_rank_redacts_goal_text(self):
        secret = 'abc123supersecret'
        goal = 'Goal API_KEY=' + secret
        items = [{'id': 'item', 'text': 'Entry API_KEY=' + secret}]
        # Spy before central redaction to verify rank owns both redactions.
        with patch.object(jev, 'ask', wraps=jev.ask) as ask:
            jev_rank.rank(items, goal)
        self.assertNotIn(secret, ask.call_args.args[0])
        payload = self.payloads[0]
        self.assertIn('[REDACTED]', payload['state'])
        self.assertIn('[REDACTED]', payload['questions']['item']['criteria'])
        self.assertNotIn(secret, json.dumps(payload))
        self.assertIn(secret, items[0]['text'])

    def test_legacy_and_unknown_sites_warn_once_per_call(self):
        for kwargs in ({}, {'site': 'unknown'}):
            self.assertIsNotNone(jev.ask('legacy state', {}, **kwargs))
        self.assertEqual(jev.undeclared_calls, 2)
        self.assertEqual(self.warning.call_count, 2)

    def test_declared_and_global_caps_both_apply(self):
        importlib.import_module('orchestrator.jev_planner')
        for global_cap, expected in ((100_000, 4000), (100, 100)):
            with patch.object(jev, '_cfg', return_value={**ENABLED_CFG, 'max_state_chars': global_cap}):
                jev.ask({'subject_title': 'word ' * 2000}, {}, site='planner')
            self.assertEqual(len(self.payloads[-1]['state']), expected)


if __name__ == '__main__':
    unittest.main()
