{{packet}}

Round {{n}}/5. Still failing: {{failing_tests}}
{{assertion_lines}}
Fix within scope, then commit the completed changes on the current branch with a message that starts with the task id before testing. Never leave uncommitted changes and never touch files outside scope.
Run only the acceptance-named tests in the foreground; never background a command. Each command must finish in under 10 minutes. The daemon runs the full gate externally.
