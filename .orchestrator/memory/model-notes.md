# model-notes

## 2026-09-17 Fallback executors on 2026-09-17: Sonnet 15/15 green first try, Opus 2/2; reviews caught 5 real defects
type: model · goal: T-0005 · provenance: repo
- claude:sonnet executed 15 tasks (complexity 1-5), 0 failed, 11 merged, 4 were fix rounds requested by sonnet reviewers; typical 1-4 min, USD 0.3-1.2 per task
- claude:opus executed 2 tasks (complexity 6), both green first try; opus flagged its own deliberate deviations in the report (legacy bridge, hold_reason)
- sonnet reviewers found: complexity probe bug, running-count ratchet, double-counted verdicts, dead challenge branch, impersonation defaults, hardcoded status — all real; one review was void because its worktree lacked the dependency (fixed by base_for)
- scouts: two of five lost to the 6000-char cap before fit_result; one negative finding wrong (grep pattern); web scout undercounted models (0.6) and a repo-only challenge template could not test it
outcome: Keep sonnet as default fallback up to complexity 5; opus at 6-8 justified. Add review affinity on both accounts (done). Prefer specs that name exact insertion points; reviewers need the dependency present

## 2026-09-17 Goal C executors 2026-09-18: sonnet 6/6 green, opus 1/1; three review-driven fix rounds
type: model · goal: T-0043 · provenance: repo
- sonnet executed the test split (4), depends_on (3), spec_review (4), two fix rounds (2, 4) and docs (2): all green first try, 2-4 min each
- opus executed the daemon (7): green first try; the security review then found two highs (blocking dispatch, stamp-before-effect) — design-level issues a spec review would likely have caught, which is the feature this goal adds
outcome: Keep: sonnet ≤5, opus 6-8. With spec review live, expect fewer design-level fix rounds; measure via scorecard review_request_changes
