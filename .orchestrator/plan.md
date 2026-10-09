# plan.md: Planner checkpoint, current state only (2026-10-03)
History: .orchestrator/plan-log.md (append-only; do not read by default, grep it). Keep this file under 10k chars (hook cap 12k). The daemon's auto-handover section must stay LAST until T-1593 merges: handover.write erases everything after it.

## Now
- 2026-10-09 GOAL T-1667 closable_goal decision: no new PR. PR 44 (goal/T-1667 -> main) merged 2026-10-07 00:42Z at 1c72a58; origin/goal/T-1667 has 0 commits not in main; every remaining child is superseded (respec lineage, conflict round T-1695). Closed on the bus (status done, result.pr_url = PR 44, goal_closed). Follow-ups live in T-1703 and T-1705 (PRs 46, 47). [ship].enabled still false, ship leaves it alone. Expected next: the same closable_goal packet for T-1705 (its PRs are merged, task still queued); answer the same way.
- 2026-10-09 closable_goal decisions already answered the same way (bus closed with the merged PR as result, no new PR): T-1658 (PR 43, 6cbc778), T-1654 (PR 41), T-1334 (PR 27), T-1391 (PR 33). Details in plan-log.md (2026-10-09 trim block).
- 2026-10-09 GOAL T-1705 decision (T-1725 held: merge conflict): filed T-1739, fix round 1 for held T-1725 (c3, opus, worktree wt/T-1725, branch task/T-1725). Cause: T-1730 (cb59f92, gate post_cmd) and T-1725 (c00a914, gate max_parallel) edit the same lines of gate.settings() and test_settings_defaults_and_override; the round rebases task/T-1725 onto goal/T-1705 89d0978 keeping both. T-1725 was green (295 tests) and review-approved at c00a914. Next: daemon gates and merges T-1739; a replayed packet is a noop while T-1739 is queued.
- 2026-10-06 GOAL T-1667 (human 'do those'): shipped as PR 44, see above. Same day fixes: luna daemon restarted with Homebrew bin on PATH (restart_daemons.sh), ai-apprentice daemon started, T-0599 re-queued, ai-apprentice autonomous=true. PRs 42/43 merged 2026-10-06 (8bc9845, 6cbc778).
- 2026-10-06 Deskmere has its OWN headless Planner: goal T-0593 in luna-inbox (live Gmail sync on staging + AppWidgets version fix), account B, log luna-inbox/.orchestrator/runs/planner-T-0593.log. Merge/redeploy/TestFlight pre-approved in the goal text; escalations: data deletion, OAuth scope changes, billing. ai-apprentice product plan sent (scratchpad/ai-apprentice-product-plan.md); waiting for the human's three decisions.
- 2026-10-06 POLICY (human): merges to main need no approval when checks pass and a rollback exists; use the subscription until provider limits (pool.toml caps lifted, backup in scratchpad); context handover automatic (T-1659, PR 43). TheSearch (ElevenLabs competition, deadline 2026-11-02 08:59 Zurich): round 1 jingles rejected (childish, too long); research in scratchpad/thesearch-jingle-research; repo /Users/k3ntaw/code/thesearch via `orchestrator new` (T-1651/T-1652).
- 2026-10-05 luna-inbox (Deskmere) goals T-0577 and T-0580 DONE (PRs 21, 22; deskmere-staging redeployed from ab6943d). Open: parallel daemon gates ([gate] max_parallel) landed via T-1705.
- Session 0022eb95 continues in place. Account A. Daemons on main (this repo and ORCH_ROOT=luna-inbox); the k3ntaw-portfolio daemon was stopped 2026-10-03.
- Executors: Opus 5.5 only since 2026-10-03 (human); Codex rows and claude:sonnet disabled in pool.toml of orchestrator, luna-inbox, k3ntaw-portfolio. Reviews still sonnet.
- T-1403 wave 3 PAUSED by the human 2026-10-03 ~19:45. Merged: T-1637, T-1641 (T-1638, T-1585 stamped), T-1645 (T-1639, T-1628 stamped). Open: B5b: T-1640 held (spec review T-1649 request_changes); T-1650 B5b v2 queued (respec_for T-1640, depends_on [T-1640], c6). Human 2026-10-03: PushNotification when all of wave 3 is merged into goal/T-1403; stamp T-1585 and T-1628 before pinging.
- ai-apprentice (/Users/k3ntaw/code/ai-apprentice): goal T-0001, PR https://github.com/K3NTAW/ai-apprentice/pull/1. Supabase fsgwnbhkctwhbtbjzvxy (eu-central-2), DB password only in keychain item ai-apprentice-supabase-db-password (human runs anything needing it). Vercel project ai-apprentice (team k3ntaws-projects).

## Open human decisions and human-applied edits
- Needs the human: local main is 17+ planner commits ahead of origin/main (bus closes for T-1334, T-1391, T-1658, memory entries); guardrails block the Planner from pushing main. Push it, or say the Planner may open a plan PR instead.
- T-1590 human edits applied 2026-10-03 (settings.json SessionStart compact hook; launcher autocompact 350000). Effective from the next f orch launch.
- PR 38 (goal/T-1403 -> main, https://github.com/K3NTAW/orchestrator/pull/38): MERGEABLE/CLEAN, waiting for the human. After merge: pull main, restart both daemons (this repo and ORCH_ROOT=/Users/k3ntaw/code/luna-inbox), record.sh draft T-1403, delete branch task/T-1580 and wt/T-1580 (human approved the deletion).
- E7's committed change edits .claude/hooks/scope-guard.sh (protected path): it reaches main only through the human PR merge; point it out in the PR.

## GOAL T-1590: merged as PR 39 (b4678a6, 2026-10-03); details and the unfiled LS3/LS4 ideas in plan-log.md.

## GOAL T-1403 (human 2026-09-30): full-Claude orchestrator
Landed on goal/T-1403 (wave 2, PR 38): B4b T-1563, base_for T-1494, D-fixkind T-1566, B6 v3 T-1569, B9 T-1578, M-sync T-1583 (merge commit 1827be6).
Landed on goal/T-1403 after PR 38 opened: B5 v6 (T-1629 via T-1634, f010804; T-1554 and T-1629 stamped).
Open:
- Wave 3: T-1637 D-fixdelta, T-1638 D-reviewhint-finish, T-1639 E7-finish, T-1640 B5b (see Now).
- B8 T-1509 parked 2026-10-03 (human): the session fix round must share the owner worktree; unpark needs a worktree-state design including merge.py. Findings: .orchestrator/pending/v4-reviews.md (T-1625).
- B2b T-1506 parked (scheduler-path design read needed; plan-log.md backlog B2b-redesign).
- Unverified: the daemon skipped an automatic round for T-1629's unittest-form gate failures (auto_fix_skipped); check _test_ids against 'ERROR: test_x (module.Class.test_x)' lines.
- Until T-1637 merges: never rely on a Codex resume fix round to read the fix spec; file a fresh finish task on a pre-created task/<id> branch instead.
Known workarounds: the Codex fix-round resume commits on the parent's branch, so the review diff is unreachable; create ref task/<fix id> at the commit, fail the bad review, reset the task to done, file a review naming `git diff <base>..<sha> -- <scope>`. The merge queue flattens merge commits; for a main-into-goal sync move the ref with `git branch -f`.

## Backlog
Carried items from the T-1334 plan live in plan-log.md, section Backlog.

2026-10-03: no learnings — goal: T-0988

## Auto-handover 2026-10-09T04:00:00+02:00 — trimmed by the Planner (size cap)

[planner].handover_context_tokens is the configured handover threshold.
The daemon rewrites this section on every handover; the previous snapshot (bus state per goal) was derivable from bus_read and was dropped on 2026-10-09 to bring plan.md under the 12k hook limit. Resume from ## Now plus bus_read(status_not="done").

<!-- end auto-handover -->
