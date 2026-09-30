import _harness
import unittest

from orchestrator import instructions
from orchestrator import spawn


class InstructionSelection(unittest.TestCase):
    def test_select_python_scope_loads_unittest_module(self):
        choice = instructions.select({"scope": ["orchestrator/x.py"]}, "execute")
        self.assertIn("python-unittest", choice["modules"])

    def test_docs_only_scope_loads_docs_module_and_not_unittest(self):
        choice = instructions.select({"scope": ["docs/a.md", "README.md"]}, "execute")
        self.assertIn("docs-task", choice["modules"])
        self.assertNotIn("python-unittest", choice["modules"])

    def test_migration_scope_loads_migration_module(self):
        choice = instructions.select({"scope": ["db/migrations/001.py"]}, "execute")
        self.assertIn("migrations", choice["modules"])

    def test_mandatory_modules_never_dropped(self):
        choice = instructions.select({"scope": []}, "security_review")
        self.assertEqual(choice["mandatory"], ["review-security"])
        self.assertIn("review-security", choice["modules"])

    def test_execute_prompt_commits_before_focused_tests(self):
        prompts = (
            spawn.render("execute", packet="brief"),
            spawn.render("fix-delta", packet="brief", n=1,
                         failing_tests="tests/test_instructions.py::test_execute_prompt_commits_before_focused_tests",
                         assertion_lines=""),
        )
        for prompt in prompts:
            self.assertLess(prompt.lower().index("commit"), prompt.index("Run only the acceptance-named tests"))
            self.assertIn("foreground", prompt)
            self.assertIn("never background a command", prompt)
            self.assertIn("under 10 minutes", prompt)
            self.assertIn("daemon runs the full gate externally", prompt)
            self.assertNotIn("Run `.claude/hooks/tests-green.sh .`", prompt)


if __name__ == "__main__":
    unittest.main()
