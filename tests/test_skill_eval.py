import _harness

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from orchestrator import cli, skill_eval


class SkillEvalTests(unittest.TestCase):
    def test_suite_covers_all_p36_groups_and_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            result = skill_eval.run_all(Path(directory))
        self.assertEqual({r['group'] for r in result['results']}, set(skill_eval.GROUPS))
        self.assertTrue(result['suite_passed'], skill_eval.format_report(result))
        self.assertTrue(all(len(row['checks']) >= 3 for row in result['results']))

    def test_cli_exit_code_and_persisted_result(self):
        original = skill_eval.run_all
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(skill_eval, 'run_all', side_effect=lambda: original(root)), \
                 mock.patch('sys.argv', ['orchestrator', 'skill-eval']), redirect_stdout(StringIO()) as output:
                cli.main()
            result = json.loads((root / 'skill_eval.json').read_text())
            self.assertTrue(result['suite_passed'])
            self.assertIn('Routing\tPASS', output.getvalue())
            with mock.patch.dict(skill_eval.FIXTURES, {'Routing': lambda _: (_ for _ in ()).throw(AssertionError('injected failure'))}), \
                 mock.patch.object(skill_eval, 'run_all', side_effect=lambda: original(root)), \
                 mock.patch('sys.argv', ['orchestrator', 'skill-eval']), redirect_stdout(StringIO()), \
                 self.assertRaises(SystemExit) as error:
                cli.main()
            self.assertEqual(error.exception.code, 1)
            self.assertFalse(json.loads((root / 'skill_eval.json').read_text())['suite_passed'])
