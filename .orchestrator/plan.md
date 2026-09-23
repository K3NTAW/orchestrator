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

## Auto-handover 2026-09-23T23:27:09+02:00 — daemon tick

[planner].handover_context_tokens is the configured handover threshold.
Open goals: none

Worktrees: wt/T-0002, wt/T-0006, wt/T-0007, wt/T-0008, wt/T-0009, wt/T-0010, wt/T-0012, wt/T-0013, wt/T-0014, wt/T-0016, wt/T-0018, wt/T-0021, wt/T-0022, wt/T-0023, wt/T-0026, … and 603 more

Last events:
- 2026-09-23T23:21:45+02:00 T-1373 update {"pipeline": {"first_ready_at": 1790197943.7972648, "dispatched_at": 1790197943.
- 2026-09-23T23:24:05+02:00 T-1373 update {"status": "done", "merged_into": "goal/T-1334", "sha": "c7e5eca84b4b04da081c60a
- 2026-09-23T23:24:06+02:00 T-1334 update {"pipeline": {"last_merge": {"status": "merged", "target": "goal/T-1334", "sha":
- 2026-09-23T23:24:06+02:00 T-1373 update {"pipeline": {"first_ready_at": 1790197943.7972648, "dispatched_at": 1790197943.
- 2026-09-23T23:24:27+02:00 T-1373 update {"pipeline": {"first_ready_at": 1790197943.7972648, "dispatched_at": 1790197943.

Resume: skill resume; re-spawn held spec reviews; dispatch ready execute tasks by hand while Codex cools.
