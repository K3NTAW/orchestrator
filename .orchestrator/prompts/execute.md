Implement this single task in the current worktree. Commit the completed changes on the current branch with a message that starts with the task id before testing. Never leave uncommitted changes and never touch files outside scope.
The packet above is the task contract: implement the ## objective, satisfy every line of ## acceptance, and edit only paths listed in ## write_scope.
Run only the acceptance-named tests in the foreground; never background a command. Each command must finish in under 10 minutes. The daemon runs the full gate externally.
No new dependencies without stating why. Finish with: summary, files changed, failures-only test output.
Tool-call budget: read only files inside Scope first, then edit; do not re-read a file you already read.

{{packet}}
