{{packet}}

Round {{n}}/5. Still failing: {{failing_tests}}
{{assertion_lines}}
Tests: this repo runs unittest, not pytest. A failure "missing: test not defined" or "not collected by unittest" means the named test must be a `def test_name(self)` method inside a `unittest.TestCase` subclass in that file; module-level functions and pytest fixtures (tmp_path) are not collected. Use tempfile.TemporaryDirectory.
Fix within scope and run `.claude/hooks/tests-green.sh .` from the worktree until it exits 0; when it is red, report only the failures-only output. Then run `git add -A && git commit` on the current branch with a message that starts with the task id. Never leave uncommitted changes and never touch files outside scope. Finish with a final message stating the gate result, the commit sha, and the failures-only output (empty when green).
