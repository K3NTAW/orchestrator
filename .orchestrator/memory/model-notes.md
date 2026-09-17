# model-notes

## 2026-09-17 Fallback executors on 2026-09-17: Sonnet 15/15 green first try, Opus 2/2; reviews caught 5 real defects
type: model · goal: T-0005 · provenance: repo
- claude:sonnet executed 15 tasks (complexity 1-5), 0 failed, 11 merged, 4 were fix rounds requested by sonnet reviewers; typical 1-4 min, USD 0.3-1.2 per task
- claude:opus executed 2 tasks (complexity 6), both green first try; opus flagged its own deliberate deviations in the report (legacy bridge, hold_reason)
- sonnet reviewers found: complexity probe bug, running-count ratchet, double-counted verdicts, dead challenge branch, impersonation defaults, hardcoded status — all real; one review was void because its worktree lacked the dependency (fixed by base_for)
- scouts: two of five lost to the 6000-char cap before fit_result; one negative finding wrong (grep pattern); web scout undercounted models (0.6) and a repo-only challenge template could not test it
outcome: Keep sonnet as default fallback up to complexity 5; opus at 6-8 justified. Add review affinity on both accounts (done). Prefer specs that name exact insertion points; reviewers need the dependency present
