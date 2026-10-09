# plan.md: Planner checkpoint, current state only (2026-10-09)
History: .orchestrator/plan-log.md (append-only; do not read by default, grep it). Keep this file under 10k chars. The daemon's auto-handover section must stay LAST: handover.write erases everything after it.

## Now
- 2026-10-09 03:50 GOAL T-1705 closable_goal decision: no new PR. Shipped as PR 46 (efa6689, 2026-10-08) and PR 47 (0d67556, merged 2026-10-09 03:15); origin/goal/T-1705 is an ancestor of main with 0 commits ahead. Execute children all merged except superseded T-1712/T-1713/T-1715/T-1723; every review done. Closed on the bus (done, result.pr_url = PR 47, goal_closed). Ship re-enabled per the goal acceptance ("ship re-enabled only after merge"): pool.toml [ship].enabled = true. Checked first: daemon pid 72699 started 03:15 from this checkout (editable install, HEAD includes goal/T-1705, Pool() reloads each tick), ship_state.json absent so the window opens at the next tick and goals closed before it (T-1705 included) are ignored; PR 38's goal T-1403 is open, untouched. Revert: enabled = false. Verify next session: .orchestrator/ship_state.json exists; no unexpected revert PR.
- 2026-10-09 T-1334, T-1391, T-1654, T-1658 closed on the bus with their PR urls (27, 33, 41, 43), no new PRs; T-1667 shipped as PR 44.
- 2026-10-06 GOAL T-1667 (human 'do those'): ship v2, next_goal from roadmap.md, watchdog + claude CLI resolver; shipped as PR 44 (1c72a58). PRs 42/43 merged 2026-10-06. luna daemon restarted with Homebrew bin on PATH (restart_daemons.sh); ai-apprentice daemon autonomous=true.
- 2026-10-06 POLICY (human): merges to main need no approval when checks pass and a rollback exists; use the subscription until provider limits (pool.toml caps lifted, backup in scratchpad); context handover automatic (T-1659, PR 43). TheSearch: round 2 jingles (6 directions + 2 logos in scratchpad/thesearch-audio/round2) wait for the human's pick; then rewrite pack sections 2-5 and 8 for 16-20 s. Details in plan-log.md.
- Deskmere (luna-inbox): own headless Planner for goal T-0593 (account B); PRs 21/22 merged, staging redeployed. ai-apprentice: product plan sent, waiting for the human's three decisions (position/vertical, Teach in beta + frame policy, entity/name/EU hosting). Details in plan-log.md.
- Session 0022eb95 continues in place. Account A. Executors: Opus 5.5 only since 2026-10-03 (human); Codex rows and claude:sonnet disabled in orchestrator, luna-inbox, k3ntaw-portfolio pool.toml. Reviews still sonnet.
- T-1403 wave 3 PAUSED by the human 2026-10-03. Merged: T-1637, T-1641, T-1645. Open: T-1650 B5b v2 (depends_on T-1640, held after spec review T-1649). Next: if T-1650 never becomes ready, supersede T-1640 (needs the pause lifted). PushNotification only after T-1650 merges.

## Open human decisions and human-applied edits
- Needs the human: local main is ahead of origin/main by planner-only commits (bus closes for T-1334, T-1391, T-1654, T-1658, T-1705, memory entries, the ship re-enable); guardrails block the Planner from pushing main. Push it, or say the Planner may open a plan PR instead.
- PR 38 (goal/T-1403 -> main, https://github.com/K3NTAW/orchestrator/pull/38): MERGEABLE/CLEAN, waiting for the human. After merge: pull main, restart both daemons (this repo and ORCH_ROOT=/Users/k3ntaw/code/luna-inbox), record.sh draft T-1403, delete branch task/T-1580 and wt/T-1580 (human approved the deletion). E7's change edits .claude/hooks/scope-guard.sh (protected path): point it out in the PR.
- T-1590 human edits applied 2026-10-03 (settings.json SessionStart compact hook; launcher autocompact 350000).

## GOAL T-1590: merged as PR 39 (b4678a6, 2026-10-03); details and the unfiled LS3/LS4 ideas in plan-log.md.

## GOAL T-1403 (human 2026-09-30): full-Claude orchestrator
Landed on goal/T-1403 (PR 38): B4b T-1563, base_for T-1494, D-fixkind T-1566, B6 v3 T-1569, B9 T-1578, M-sync T-1583 (1827be6); after PR 38 opened: B5 v6 (T-1629 via T-1634, f010804).
Open: wave 3 T-1640 B5b -> T-1650 (see Now). B8 T-1509 parked (session fix round must share the owner worktree; findings .orchestrator/pending/v4-reviews.md). B2b T-1506 parked (plan-log.md backlog B2b-redesign). Unverified: auto_fix_skipped for unittest-form gate failures; check _test_ids against 'ERROR: test_x (module.Class.test_x)' lines.
Known workarounds: a Codex fix-round resume commits on the parent's branch; create ref task/<fix id> at the commit, fail the bad review, reset the task to done, file a review naming `git diff <base>..<sha> -- <scope>`. The merge queue flattens merge commits; for a main-into-goal sync move the ref with `git branch -f`.

## Backlog
Carried items from the T-1334 plan live in plan-log.md, section Backlog.

## Auto-handover 2026-10-09T04:00:00+02:00 — trimmed by the Planner (size cap)

[planner].handover_context_tokens is the configured handover threshold.
The daemon rewrites this section on every handover; the previous snapshot (bus state per goal) was derivable from bus_read and was dropped on 2026-10-09 to bring plan.md under the 12k hook limit. Resume from ## Now plus bus_read(status_not="done").

<!-- end auto-handover -->
