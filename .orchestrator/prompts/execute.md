{{packet}}

Implement this single task in the current worktree. Run `.claude/hooks/tests-green.sh .` from the worktree until it exits 0; when it is red, report only the failures-only output. Then run `git add -A && git commit` on the current branch with a message that starts with the task id. Never leave uncommitted changes and never touch files outside scope. Finish with a final message stating the gate result, the commit sha, and the failures-only output (empty when green).
Spec: {{spec}}
Acceptance: {{acceptance}}
Scope (only these paths): {{scope}}
No new dependencies without stating why. Finish with: summary, files changed, failures-only test output.
Tool-call budget: read only files inside Scope first, then edit; do not re-read a file you already read; run the test script at most twice.
