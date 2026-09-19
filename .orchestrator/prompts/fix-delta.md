{{packet}}

Round {{n}}/5. Still failing: {{failing_tests}}
{{assertion_lines}}
Fix within scope and run `.claude/hooks/tests-green.sh .` from the worktree until it exits 0; when it is red, report only the failures-only output. Then run `git add -A && git commit` on the current branch with a message that starts with the task id. Never leave uncommitted changes and never touch files outside scope. Finish with a final message stating the gate result, the commit sha, and the failures-only output (empty when green).
