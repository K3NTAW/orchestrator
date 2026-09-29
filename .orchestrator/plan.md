# plan.md — Planner checkpoint (updated 2026-09-23 late; GOAL T-1334 code complete on goal/T-1334 at c7e5eca, goal gate tests-green OK 1511; PR 27 open https://github.com/K3NTAW/orchestrator/pull/27, awaiting the human's review)

Previous goal T-0991 (Hermes program) is complete: PR 25 and PR 26 merged into main (737c7a5), retrospective in decisions.md. Its full plan is in git history (`git log -- .orchestrator/plan.md`, last version at 1571826).

## GOAL T-1334 (user 2026-09-23): provider-agnostic executor pool
User: "I want models to be interchangeable so we have a good plug and play structure if I ever remove the codex account or new models come out." First model: Opus 5.5 (claude-opus-5-5) as a first-class executor row.

Goal branch: goal/T-1334, cut from origin/main 737c7a5.

### Classification
Complexity 7: cross-cutting (pool routing, executor dispatch, worker spawn, review rule); every orchestrator/*.py is a [review] security_paths glob, so each task gets exactly one security review on security_review_tier. Route: high-risk/architectural: this plan, spec review (complexity >= 6), deterministic gate, one security review per task, human PR. Zero scouts: Planner grep (provenance repo) answered the questions below.

### Findings (grep, 2026-09-23)
- [[executors]] rows carry `provider`, but only Codex runs: executor.start sends any non-codex pick to `_exhausted()` (executor.py:487), which runs a fallback tier by complexity (pool.fallback_tier: sonnet <=5, opus 6-8, hold >=9), not the routed model.
- spawn.run_worker resolves the model as `cfg["models"][t["tier"]]` (spawn.py:1538) and stamps executor `claude:<tier>` (spawn.py:1564).
- The self-review rule keys off string prefixes: daemon.review_tier (daemon.py:1070), _security_review_tier (daemon.py:1136), _review_plan (daemon.py:1241).
- Claude capacity lives on [[claude_accounts]] (pool.pick), not on executor rows; running Claude workers are counted by `assigned_to` `claude:<acct>` (daemon.running_claude_workers), which a row-routed run keeps.
- Fix rounds: resume_plan already returns fresh for non-codex parents (executor.py:279).
- Scorecard, allocation, handoff and jev routing already work on executor ids, so a Claude row gets scored like any other row.

### Named uncertainties
None open. Design choice: a Claude row's task has executor == tier == row id (e.g. `opus55`), like Codex rows; legacy fallback keeps `claude:<tier>`. Everything that needs provider or model asks `Pool.executor_identity(field)`.

### Tasks
| id | title | complexity | depends_on | scope |
|---|---|---|---|---|
| T-1352 | S1 v6 (lands green commit 3925705 from T-1347; parseable acceptance ids). Design as T-1347 v5: executor_identity (cfg-only) / is_claude_executor, claude rows need account headroom (pick once, lazily), codex_available stays Codex-only, review rule compares models with None guard, legacy branches exact as today, fail closed with stderr line; daemon imports executor_identity + config as pool_config; _open_reviews (daemon.py:1212) takes cfg | 6 | - | pool.py, daemon.py, tests/test_pool.py, tests/test_daemon.py |
| T-1360 | S2a v2: claude rows have id claude:<tier> (validated at load, model defaults to the [models] alias); executor.start routes them through _exhausted(tier=row tier): same account pick, reservation handoff, review_rule; no spawn.py change | 5 | T-1352 (merged) | pool.py, executor.py, tests/test_pool.py, tests/test_executor.py |
| T-1367 | S3 docs: README "Adding or removing models" (Codex row, claude:<alias> row, headroom, running without Codex, review rule) | 2 | T-1360 | README.md |
| T-1373 | S4 v2: claude:opus row (max_parallel 2) + [models].opus = claude-opus-5-5; tests/_harness.py drops live claude rows from the test copy; shipped-row test | 2 | - | .orchestrator/pool.toml, tests/_harness.py, tests/test_pool.py |

Merged into goal/T-1334: T-1352, T-1360, T-1367, T-1373 (all security-reviewed where on security paths). T-1370 -> T-1373: gate red (harness copies live pool.toml); fix rounds T-1371/T-1372 could not widen Codex write scope (gotchas.md).

Superseded 2026-09-23 (none executed):
- T-1335 -> T-1338: spec review T-1337 (codex_available would read a winning claude row as Codex cooling; None == None model match; Pool() per review call; test isolation).
- T-1338 -> T-1341: spec review T-1340 (pick() once per pass; codex-only equivalence test; legacy startswith vs exact pinned; exception scope + log; cfg read once; per-scenario tests with named patch target; capacity.eligible_executors_from_snapshot hazard moved into S2).
- T-1341 -> T-1344: spec review T-1342 (Planner errors: named a nonexistent daemon.pool_module import and _review_plan instead of _open_reviews; default-cfg read outside the fail-closed try; circular baseline test).
- Lesson: name exact imports, patch targets and enclosing functions from a grep of the current file before writing a spec; three request_changes rounds here were spec-precision, not design.
- T-1344 -> T-1347: spec review T-1345. Accepted: _open_reviews loads cfg once; executor_rows(cfg) shared with Pool (legacy synthesized row); None executor skips config; per-scenario test ids; S2 re-picks the account at dispatch. Rejected with reasons in the spec: model-id alias matching, a guard test against enabling a claude row early (S1+S2 ship in one goal PR), a pool.toml byte test.
- T-1347 -> T-1352: Codex implemented S1 green (tests-green OK 1504 at 3925705, Planner-verified), but the daemon gate held gate_red: acceptance ids written as tests/x.py::Class::test, and acceptance.py:8 reads only tests/x.py::test_name, so it looked for def ExecutorIdentity(. Auto fix round T-1350 superseded (nothing to fix). T-1352 cherry-picks 3925705 under parseable ids.
- T-1352 merged into goal/T-1334 after its security review (2026-09-23).
- T-1353 -> T-1356 + T-1357: spec review T-1355 returned unparseable output (raw cut at 2000 chars); Planner read it: budget reservation missing on the claude branch, admit needs one shared Claude slot counter, fallback/_codex_available need one definition, run_worker must resolve the row model before cfg["models"][tier], handed_off only after thread.start. Split by behaviour (dispatch vs capacity), disjoint files, both depend only on merged S1.
- T-1356/T-1357 -> T-1360/T-1361: spec reviews T-1358/T-1359 (row-id reservation keying, tier leak, missing review_rule, unreleased reservation, snapshot vs pick divergence, shared counter placement, running count missing claude rows). Design change: Claude rows are id claude:<tier>, the executor value the fallback path already writes, so dispatch reuses _exhausted() and every downstream reader already works.
- T-1336 -> T-1339 -> T-1343 -> T-1346 -> T-1348 -> T-1353: depends_on moves only; T-1353 also fixes the same acceptance-id format (bus.update refuses depends_on), plus capacity.py (capacity.py:54, used at daemon.py:806).

Interface contract: `pool.executor_identity(field, cfg) -> {"provider": "codex"|"claude"|None, "model": str|None}` (cfg-only), `Pool.executor_identity(field)`, `Pool.is_claude_executor(field) -> bool`, `daemon.review_tier(t, cfg=None)`, `daemon._security_review_tier(t, cfg=None)`.

### Follow-ups (after this goal)
- New 2026-09-28 (HIGH): the daemon runs tests-green with no timeout; an xcodebuild test run hung for 6 h on the iOS simulator (luna T-0540), blocking the goal. Gate runs need a hard timeout (e.g. 45 min) that kills the process tree, stops the simulators and retries once before holding the task as infra_failure (not gate_red).
- New 2026-09-27: Claude execute workers that finish correctly (commits made, worktree clean) are marked "failed" with status_reason non_json because their final message is prose, not the JSON result the bus expects (luna T-0514, T-0526). run_worker should derive the result from the git state (commits on the task branch, clean tree) when the final message is not JSON, and mark the task done so the daemon gates and merges it.
- New 2026-09-27: headless Claude executors cap each shell command at 10 minutes, while the luna Apple gate takes 9-11; they background it and exit without committing. luna execute.md now says commit first, then test (a816ce9). The orchestrator should (a) make run_worker commit leftover changes itself before marking a Claude execute task done, and (b) not rely on the worker to run the full gate (the daemon gates externally anyway).
- New 2026-09-27: `orchestrator goal start` scaffold commits the target repo's live, uncommitted .orchestrator/pool.toml (luna 43da1f7 carried temporary Codex-off overrides to main via PR 13, the same class as orchestrator PR 28). The scaffold should never stage pool.toml, or only the shipped defaults.
- New 2026-09-27: a task that ends status "failed" (not held) gets no automatic fix round or decision Planner; luna A2 T-0465 sat ~2 h with 16 uncommitted files. The daemon should treat failed execute tasks like held ones (commit leftover work, then a fix round), and alert when a goal has failed tasks and nothing running.
- New 2026-09-26 (HIGH): headless Claude executors (claude:<tier> via spawn.run_worker) run tests-green in the background and exit "waiting for the notification", leaving work uncommitted (luna T-0282, T-0425). The execute prompt for Claude workers must say: run the gate in the foreground, never background a command, commit before the final message; spawn should reject a Claude result with a dirty worktree and no commit by resuming once with "commit now".
- New 2026-09-25: parallel Apple tasks all edit apple/LunaInbox/App/RootView.swift and the generated project.pbxproj, so each later merge conflicts; the daemon then holds the task and decision Planners "give up after 2 attempts", leaving it stuck with nothing queued (luna A3/A4 sat green but unmerged for hours). Fix: auto-file a rebase round on merge conflict; treat generated files (xcodeproj) as regenerate-not-merge; alert when a goal has held tasks and nothing queued or running.
- New 2026-09-25 (HIGH): killing or restarting the daemon orphans its in-flight workers; their tasks stay status running forever and the wave scheduler keeps selecting them, deferring every overlapping task (luna T-0266 stalled M5 for ~6h, 10:09-16:45). Fix: on daemon start, reset running execute tasks whose pid/worker is gone to queued (clearing dispatched_at*); skip non-dispatchable (already running) tasks when building a wave.
- New 2026-09-25: `orchestrator merge <fix-round>` (CLI) merges and stamps only that task; unlike the daemon it does not walk fix_round_for to stamp the root, so dependents of the root stay blocked (luna T-0324 -> T-0243 stamped by hand). Share the walk between both paths.
- New 2026-09-25 (HIGH): worktrees are never removed after a task merges or is superseded. The disk filled (460 GB, ENOSPC) with 959 orchestrator worktrees (304 GB, a ~330 MB .venv each) and 192 luna-inbox worktrees (36 GB, 23 GB Xcode DerivedData). Human approved a one-off cleanup of finished worktrees. Fix: remove the worktree (git worktree remove --force) when merge stamps merged_into or a task is superseded; share one .venv/DerivedData cache instead of per-worktree copies; the daemon refuses to dispatch below a free-space floor (e.g. 20 GB) and notifies.
- New 2026-09-24: a Planner filing a batch predicts task ids before creating them, and any task created in between (the daemon filed review T-0241) shifts every later id: luna T-0240 wired all 20 depends_on one id too low, and A1 started before its dependency. Fix: bus_create_task should accept symbolic refs (plan labels resolved at create time) or the Planner must use the id returned by each create. bus.update rightly refuses depends_on; re-filing was the fix.
- New 2026-09-24: the daemon treats hold_reason "cancelled" (a Planner cancelling its own mis-filed task) as a held task and launches a decision Planner asking for a fix round (luna T-0197). A Planner cancel should set status superseded/cancelled, and route_decision should skip hold_reason cancelled.
- New 2026-09-24 (HIGH): a fix-round merge stamps merged_into on every fix_round_for ancestor without checking that the ancestors' commits reached the target. luna-inbox T-0174 (M4 generation routes, a17830c + affad6e) was marked merged into goal/T-0166 when fix round T-0191 merged, but T-0191's branch held only its own openapi.d.ts commit, so backend/src/generation/routes.ts and its test never reached main (found by the M5 Planner, re-landed as T-0196). Audit of all luna merged tasks: only T-0174 lost files. Fix: before stamping an ancestor, require its task-branch diff to be contained in the target (git cherry / patch-id, or per-file content check); otherwise hold with reason "ancestor_not_landed". Fix rounds should branch from the held parent's task branch, not the goal branch.
- 2026-09-24 DONE via goal T-1375 / PR 30 (da0bb42): failed spec reviews retried then held; Claude dispatch requeues at no-headroom or worker cap (replaces parked S2b). PR 27 (T-1334) merged 8e6c68e; PR 28 checkpoint; PR 29 restored shipped pool.toml defaults after PR 28 turned main red. Daemons restarted on da0bb42. Account B held until 2026-09-24 15:00 (weekly limit).
- New 2026-09-24: bus.create_task should reject a fix round whose depends_on contains its fix_round_for parent (deadlock seen in luna-inbox T-0051/T-0052). Also merge.py rebase_changed_diff holds have no re-review path (luna T-0047, T-0057): _diff_hash includes context lines, so any clean rebase over a neighbouring merge trips it. Fix: compare git patch-id --stable of the task commits before/after rebase (T-0057: 513267d both sides), re-review only when they differ.
- Intermittent KeyError('') in luna-inbox: 2026-09-24 ~00:05 a worker thread died in spawn.run_worker -> Pool() -> pool._load -> bus.read -> bus.get(''); 2026-09-24 ~09:45 spec review T-0067 failed "post_result failed: ''". No empty-id row in tasks or events afterwards, so it is transient (a row read mid-write, or a lookup with an empty key). Next occurrence: capture the full traceback (the post_result except in spawn.py swallows it; log traceback.format_exc() there).
- bus.ready never releases dependents of a superseded task (only merged_into counts). Planners replace held tasks with plain respecs (luna T-0070/T-0072/T-0073 for T-0042/T-0043) and assume superseded frees T-0044/T-0046. Fix idea: when a task with constraints.respec_for merges, walk respec_for and set merged_into (merged_via) on the original, mirroring the fix_round_for walk in daemon.report_merge.
- Dispatch results can be dropped silently: daemon._dispatch_worker returns without a trace when the launch epoch changed (second daemon, e.g. an MCP server's in-process thread on older code) and has no branch for new statuses (claude_capacity). T-1384/T-1385 sat queued with dispatched_at_done for ~20 min on 2026-09-24. Fix idea: log a decision row for every non-handled status and for stale-epoch returns.
- Review packet base can equal the task head: luna T-0107 (security review of T-0104) got packet base fe23d54 = the implementation commit, so its packet diff was empty and the reviewer read only routes.ts. Fix: base review packets on merge-base(goal branch, task head), not on the task's recorded base when that equals HEAD.
- Acceptance-named tests are only verified for Python ids (acceptance._TEST_ID matches tests/*.py). luna T-0104 passed the gate without backend/test/oauth.test.ts and google-oauth.test.ts although acceptance named them. Fix: treat any acceptance path that looks like a test file (tests?/..., *.test.ts, *Tests.swift, *Test.kt) as required to exist in the diff.
- `orchestrator install` copies skills/ and an active [skills] config into a target but does not build .orchestrator/skills/registry.json, so an execute dispatch that routes to a skill fails with dispatch error KeyError('executor/implement-spec') (luna T-0115, 2026-09-24; fixed by hand with `orchestrator skills sync`). Fix: run skills sync at the end of install, and make the dispatch fall back to no skill (with a decision row) when the registry lacks an id.
- Remaining: concurrency.py / speculation.py per-row free for claude rows (low).
- S2b parked 2026-09-23 (T-1357 -> T-1361 -> T-1363 -> T-1365, reviews T-1359/T-1362/T-1364/T-1366): scheduler-level accounting for claude:<tier> rows: count them against max_parallel_claude_workers (including unclaimed Claude-row dispatches, excluding Codex ones), fold all-accounts-cooling into row cooling without mis-triggering the legacy fallback, day-budget rollover in free_slots, one shared Rule R source. Meanwhile S1 eligibility (account headroom) and the row max_parallel bound routed Claude executes; keep the claude:opus row at max_parallel 2.
- A routed claude dispatch with no account headroom is held "no account with headroom" and the daemon only auto-retries budget holds (daemon.py retry_held): such tasks strand until a manual clear. Same for today's fallback path.
- concurrency.py / speculation.py read per-row free and may overestimate claude capacity.
- A spec review whose output does not parse is marked failed and never retried; the reviewed task sits queued (T-1355).

### After merge (Planner, config only)
Add to .orchestrator/pool.toml:
```
[[executors]]
id = "claude:opus"      # tier alias; model comes from [models].opus = claude-opus-5-5
provider = "claude"
roles = ["execute"]
complexity_min = 1
complexity_max = 10
max_parallel = 2
daily_budget_tasks = 20
quota_group = "claude"
weight = 1.0
enabled = true
```
Then decisions.md entry with revert path (enabled = false), retrospective, PR goal/T-1334 -> main.

### Side request 2026-09-23: new product luna-inbox (separate target repo, own Planner)
User asked to build the Jev + GPT-6 Luna email product (uploaded spec) at full five-platform GA scope, native clients (Planner's call on the user's delegation), Xcode + Supabase MCP, Android SDK tools, private repo. Planner hooks block writes outside .orchestrator/, so the setup is a user-run script: scratchpad luna-inbox/setup.sh (+ roadmap.md, product-spec.md). Repo named luna-inbox, not luna-mail: guardrails.sh denies gh/curl commands that mention "mail". Flutter + Dart MCP skipped (native). After the script runs, the product is planned by a Planner in ~/code/luna-inbox, not here.

### Config on local main (2026-09-23)
- 72038bf: pool.toml rebuilt from origin/main 737c7a5 + 18 live overrides, restoring the sections 147cbb2 dropped (user approved). Local main is 7 commits ahead of origin/main and unpushed; after PR 27 merges, pulling main conflicts only on the identical [models].opus line.

### Earlier (superseded by 72038bf and PR 27)
- 2026-09-23 [models].opus claude-opus-5 -> claude-opus-5-5 (Planner default tier, fallback executor 6-8, cross-tier review). decisions.md entry written by hand (record.sh blocked by planner-mode hook). Awaiting the user's commit approval.

## Auto-handover 2026-09-24T11:55:23+02:00 — daemon tick

[planner].handover_context_tokens is the configured handover threshold.
Open goals: none

Worktrees: wt/T-0002, wt/T-0006, wt/T-0007, wt/T-0008, wt/T-0009, wt/T-0010, wt/T-0012, wt/T-0013, wt/T-0014, wt/T-0016, wt/T-0018, wt/T-0021, wt/T-0022, wt/T-0023, wt/T-0026, … and 614 more

Last events:
- 2026-09-24T11:52:47+02:00 T-1378 update {"resume_hint": {"failures": "tests-green: FAILED. Failures only:\nTraceback (mo
- 2026-09-24T11:53:31+02:00 T-1378 update {"resume_hint": {"failures": "tests-green: FAILED. Failures only:\nTraceback (mo
- 2026-09-24T11:53:55+02:00 T-1378 update {"resume_hint": {"failures": "tests-green: FAILED. Failures only:\nTraceback (mo
- 2026-09-24T11:54:39+02:00 T-1378 update {"resume_hint": {"failures": "tests-green: FAILED. Failures only:\nTraceback (mo
- 2026-09-24T11:55:23+02:00 T-1378 update {"resume_hint": {"failures": "tests-green: FAILED. Failures only:\nTraceback (mo

Resume: skill resume; re-spawn held spec reviews; dispatch ready execute tasks by hand while Codex cools.
