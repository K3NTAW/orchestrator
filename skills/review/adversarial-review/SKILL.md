---
name: adversarial-review
description: Review a scoped diff you did not write against acceptance criteria; complexity ≥7 adds the security checklist. Use for review tasks from the orchestrator.
---
# Adversarial review
A human reviews merges. You see -U3 hunks only; Read a single path if a hunk is ambiguous, never the whole repo.
Look for: acceptance not actually met, behavior changes outside scope, missing error paths, tests that assert nothing. Complexity ≥7: apply `references/security-checklist.md`.
Return ONLY `{"verdict":"approve|request_changes","comments":[{"path":"","line":0,"issue":"","severity":"low|med|high"}]}` via bus_post_result.
