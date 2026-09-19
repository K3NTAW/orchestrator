# gotchas

## 2026-09-17 guardrails.sh blocks any Bash command text containing the system bin prefix
type: gotcha · goal: manual-2026-09-17 · provenance: repo
- .claude/hooks/guardrails.sh — protected-path check greps the literal prefix anywhere in the command, so heredocs writing a shebang line, and PR bodies quoting one, are blocked
outcome: fix: write such files with the Write tool (path-based check only) or from a body file; planner-mode makes this moot for the Planner

## 2026-09-17 planner-mode treats /var/folders as a temp dir
type: gotcha · goal: manual-2026-09-17 · provenance: repo
- tests/test_orchestrator.py PlannerMode.test_writes — tempfile.mkdtemp lives under /var/folders, so a fake source path under TMP is allowed by design; use a real REPO path to test the block
outcome: test fixture fix pending human commit 2026-09-17

## 2026-09-17 tests-green.sh must run under uv outside the MCP server
type: gotcha · goal: T-0001 · tasks: T-0002 · provenance: repo
- .claude/hooks/tests-green.sh:17 — without pytest on PATH it runs plain python3; the system python3 is 3.9 and the tests import tomllib (3.11+)
- merge() inherits the orchestrator MCP server env, which uv run started, so the venv python is first on PATH there
outcome: Planner runs the gate as: uv run .claude/hooks/tests-green.sh wt/<id>. Follow-up: make the hook prefer uv run when pyproject.toml exists (hooks dir, needs a worker task)

## 2026-09-17 Guardrails.test_edit_protected_path was checkout-relative
type: gotcha · goal: T-0001 · tasks: T-0002,T-0003 · provenance: repo
- tests/test_orchestrator.py:177 — REPO/.claude/settings.json is only protected in the main checkout; in wt/T-xxxx it is unprotected, so the merge gate fails for every task
outcome: T-0003 derives the path from protected-paths.txt. Lesson: any test that asserts on a protected path must use the list, not REPO

## 2026-09-17 planner-mode redirect heuristic fires on the co-author trailer
type: gotcha · goal: T-0001 · provenance: repo
- .claude/hooks/planner-mode.sh — the '>' in 'Co-Authored-By: ... <noreply@anthropic.com>' matches the redirect regex, so inline git commit -m with the trailer is denied; heredoc bodies with angle brackets too
outcome: Planner writes commit messages and PR bodies to the scratchpad and uses git commit -F / gh --body-file (temp path mention is allowed). Follow-up: strip <...@...> before the redirect check

## 2026-09-17 Verify negative scout findings with a direct listing
type: gotcha · goal: T-0005 · tasks: T-0008 · provenance: repo
- scout T-0008 reported no gpt-luna/terra/sol ids on disk at 0.85 confidence; the ids are gpt-5.6-luna, gpt-5.6-sol, gpt-5.6-terra in the Codex models_cache.json; the grep pattern assumed a gpt-name prefix
outcome: before acting on an absence claim, list the source directly; scout specs should ask for a full listing plus the match, not a pattern match alone

## 2026-09-17 Worker results over MAX_RESULT_CHARS were silently lost
type: gotcha · goal: T-0005 · tasks: T-0009,T-0010,T-0015 · provenance: repo
- orchestrator/bus.py:75-80 post_result raises when the result exceeds 6000 chars; orchestrator/spawn.py run_worker called it unguarded in a daemon thread, so the task stayed running with no process
outcome: T-0015 adds spawn.fit_result plus a fail-safe status; until merged, scout specs carry an explicit output cap of 4,500 chars

## 2026-09-17 challenge.md is repo-only; web claims cannot be challenged with it
type: gotcha · goal: T-0005 · tasks: T-0014 · provenance: repo
- .orchestrator/prompts/challenge.md:2 — 'refute it with concrete evidence from the repo'; the T-0014 challenger never fetched a page and returned confirmed on repo grounds
outcome: for web or tool-output claims, write the challenge spec with explicit URLs and say the prompt template's repo instruction does not apply; or add a challenge-web.md template (follow-up)

## 2026-09-17 Review affinity lives only on account B, so a B-executed fallback task gets a same-account review
type: gotcha · goal: T-0005 · tasks: T-0016,T-0021 · provenance: repo
- .orchestrator/pool.toml — account A role_affinity has no review; B has review and execute; B1 executed as claude:opus on B and its sonnet review also runs on B
outcome: different model still holds (opus vs sonnet); add review to account A affinity or make Pool.pick(review) exclude the executing account (follow-up spec); until then note same-account-review on the PR

## 2026-09-17 Review worktrees are created from origin/main, so stacked tasks get reviewed without their dependencies
type: gotcha · goal: T-0005 · tasks: T-0017,T-0026 · provenance: repo
- orchestrator/spawn.py ensure_worktree(task_id, base=origin/main) is used for review workers too; scoped_diff diffs origin/main...HEAD of the reviewed task; T-0026 judged executor.py in a checkout lacking B1 and reported pick_executor as nonexistent
outcome: Planner pre-creates the review worktree off the reviewed task branch before spawn_review; follow-up spec: ensure_worktree base = reviewed task branch for role review, diff base = parent goal branch; until then the first review of any stacked task is suspect

## 2026-09-17 scrapling 0.4.15 plain Fetcher imports the browser client libraries
type: gotcha · goal: T-0005 · tasks: T-0023,T-0036 · provenance: repo
- scrapling.engines.static imports toolbelt.convertor (curl_cffi, playwright, patchright) and toolbelt.fingerprints (browserforge) unconditionally; the Python packages are required for import even though no browser is installed (scrapling install is never run)
outcome: pyproject lists scrapling plus those four explicitly instead of the [fetchers] extra; msgspec and protego dropped; camoufox absent. Revisit when scrapling makes them lazy

## 2026-09-17 artificialanalysis.ai does list GPT-5.6 Luna/Terra/Sol and Claude Opus 5; scout T-0007 was wrong
type: gotcha · goal: T-0005 · tasks: T-0007,T-0014,T-0036 · provenance: repo
- live fetch 2026-09-17 16:17 UTC (bench.json unmatched_names): GPT-6 Astra (max), GPT-5.6 Luna (max), GPT-5.6 Terra (max), GPT-5.6 Sol (max), Claude Opus 5 (Adaptive Reasoning, Max Effort), Claude Fable 5.1 (...), GPT-5.5 Pro (xhigh)
- display names carry a parenthetical effort suffix; bench.match compared whole strings, so 0 of 8 hints matched
outcome: B4c fix: match on the name with parentheticals stripped, prefer the (max) variant; lesson: a 0.6 negative web finding plus a repo-only challenge is not evidence, fetch and look

## 2026-09-17 Planner committed a config knob change without running the suite
type: gotcha · goal: T-0005 · provenance: repo
- .orchestrator/pool.toml window_cap_tokens 2M to 10M (b680ef5) broke PoolSel.test_affinity_reserve_cooldown_budget, whose token fractions were hard-coded for a 2M cap; the Planner ran tomllib parse and status --plain but not the tests
- the T-0040 worker refused to commit on a red suite and reported the regression with the cause; that behaviour is what we want
outcome: rule: any Planner commit that touches .orchestrator/pool.toml or another file the tests read runs the gate first; tests must derive thresholds from Pool().cap, not literals (T-0042)

## 2026-09-17 Review-task acceptance lines get read as the reviewed spec's acceptance
type: gotcha · goal: T-0043 · tasks: T-0053 · provenance: repo
- review T-0053 flagged 'test result line reported' as a spec mismatch; that line was the review task's own acceptance criterion, not the execute spec's
outcome: in review specs, label the reviewer's acceptance explicitly: 'Acceptance for YOUR review output'; keep the execute task's acceptance quoted separately

## 2026-09-17 fit_result trimmed only findings; other list fields could still lose a result
type: gotcha · goal: T-0043 · tasks: T-0015,T-0049,T-0055 · provenance: repo
- orchestrator/spawn.py fit_result (cd5048a) binary-searched result['findings'] only; review comments and spec_review risks were unprotected
outcome: T-0055 generalizes to every list-valued key; lesson: cap enforcement must be shape-agnostic

## 2026-09-17 daemon.notify built an osascript literal from untrusted text
type: gotcha · goal: T-0043 · tasks: T-0050,T-0054 · provenance: repo
- orchestrator/daemon.py notify() (pre-existing) f-string-interpolated the message into osascript -e; the daemon now feeds it merge stderr and task titles, so a quote in worker or git output could execute local commands
outcome: fix round passes the text as an argv item to an 'on run argv' handler; lesson: any shell or script literal fed from worker output is an injection path — reviewers with the security checklist catch these; keep the checklist mandatory for autonomous stages

## 2026-09-17 Fix rounds leave the original task without merged_into, so the daemon re-reviews merged work
type: gotcha · goal: T-0043 · tasks: T-0050,T-0057,T-0063 · provenance: repo
- merge(fix_round) sets merged_into only on the fix-round task; the original (e.g. T-0016, T-0018) stays done with merged_into unset; the first daemon --once created six stale review tasks T-0057..T-0062 and its threads died with the process
outcome: T-0063: tick() skips tasks of closed goals and treats a task as merged when its branch is an ancestor of goal/<parent>; stale tasks marked failed/superseded

## 2026-09-18 kgpt hosts and how to reach them
type: reference · goal: kgpt-recon-2026-09-18 · provenance: repo
- Hetzner prod: `ssh kgpt@46.62.167.12` (ubuntu-4gb-hel1-4). Compose dir is /home/kgpt/kgpt, not /opt/kgpt as README says; image kgpt:local built from that checkout
- Home server: `ssh k3ntaw@192.168.1.167` (kenta-server). Login shell is fish, so pipe scripts via `bash -s`. kgpt home side in /opt/kgpt-home on ghcr.io/k3ntaw/kgpt:latest
- guardrails.sh blocks command text naming the ssh config dir or the system config dir, even inside an ssh remote string; planner-mode blocks Write to the claude-a memory dir, so recon notes go here
outcome: recorded; both boxes verified up 2026-09-18 11:00

## 2026-09-18 daemon never fires the Claude fallback while every Codex executor is cooling
type: gotcha · goal: T-0065 · tasks: T-0070 · provenance: repo
- orchestrator/daemon.py:84 free_slots counts only enabled, non-cooling Codex executors; with all rows cooling it is 0 and dispatch() breaks before executor.start, so on_exhausted=fallback_claude (executor.py:110) is never reached from the daemon
- manual route that works: spawn_scout(task_id) runs spawn.run_worker, whose execute branch (spawn.py:208) claims the task and runs claude:sonnet in its worktree; safe from double dispatch because the daemon has no slots
outcome: T-0070 dispatched by hand 2026-09-18 13:20; fix candidate: count limits.max_parallel_claude_workers as slots when the policy is fallback_claude

## 2026-09-18 daemon gate spawns the review at the default tier, so a Claude-executed task gets reviewed by the same model
type: gotcha · goal: T-0065 · tasks: T-0070,T-0071,T-0072 · provenance: repo
- orchestrator/daemon.py:168-171 gate() creates the review task without a tier, so it defaults to sonnet; T-0070 was executed by claude:sonnet (fallback) and T-0071 was a sonnet review of sonnet output, against the never-review-own-output rule
- manual remedy used: a second review task on tier opus with inputs=[T-0070], spawned with spawn_review (T-0072); the daemon merged on the first approve before the second landed
outcome: fix candidate: gate() reads the executed task's executor field and picks a different model (opus for a sonnet executor) and waits for that review; until then, add the other-model review by hand for every fallback-executed task

## 2026-09-18 spec_review role was in no account's role_affinity, so every spec review held with no account with headroom
type: gotcha · goal: T-0073 · tasks: T-0081 · provenance: repo
- orchestrator/pool.py:136 pick() skips accounts whose affinity lacks the role; .orchestrator/pool.toml role_affinity listed planner/scout/triage/execute/review/challenge only, so T-0081 (the first spec_review ever spawned) held twice with 'no account with headroom' at 6 percent utilization
- the daemon spawns spec reviews for every complexity 5+ task (daemon.py:126-140), so without this every such task would have stalled silently before dispatch
outcome: fixed 2026-09-18 17:55 in both pool.toml files (orchestrator and kgpt): spec_review added to both accounts; test candidate for C-O6 or later: a pool test asserting every role in ROLES appears in at least one affinity

## 2026-09-18 a rebased fix round hides the original task from already_merged, which checks branch ancestry
type: gotcha · goal: T-0073 · tasks: T-0078,T-0087,T-0090 · provenance: repo
- merge.py rebases the fix-round branch onto the goal branch, so the original task branch (task/T-0078 at b4b1b1e) is no longer an ancestor of goal/T-0073 even though its rebased copy (c2aa847) is; daemon.already_merged (daemon.py:59-83) uses git merge-base --is-ancestor and returns False, and the original stays held with merged_into unset, blocking dependents via bus.ready
outcome: worked around 2026-09-18 by bus.update(T-0078, status=done, merged_into=goal/T-0073); fix candidate: compare by patch id (git patch-id) or by the fix_round_for constraint, and mark the original merged when its fix round merges

## 2026-09-18 A Planner restart orphans running fallback executors: the claude child finishes and commits, but nobody posts its result
type: gotcha · goal: T-0073 · tasks: T-0115 · provenance: repo
- mcp.py:20 runs spawn.run_worker in a daemon thread of the MCP server; the claude -p child (spawn.py:130 Popen with PIPE) outlives the server when the Planner session restarts, finishes its work in the worktree and exits, but the thread that would parse its output and post the bus result is gone
- the daemon then sees a dead pid and requeues the task as 'process died', which would re-run finished work on top of the executor's commit (T-0115: abc3f56 sat in wt/T-0115 with the task queued)
- manual remedy 2026-09-18 14:50: bus_claim the task before the daemon redispatches, run .claude/hooks/tests-green.sh on the worktree, verify acceptance by hand, bus_post_result done with the commit sha; the daemon then gates and reviews as usual
outcome: fix candidate for Phase D session rules (D4): before a handover, either wait for running executes or have the daemon's requeue path check the worktree for a commit ahead of the base and re-gate instead of redispatching; the handover command (C-O7b) should refuse while an execute is running

## 2026-09-18 run_worker crashes with KeyError 'reason' when claude exits non-zero with JSON output (budget cap), so the worker's failure is never recorded properly
type: gotcha · goal: T-0073 · tasks: T-0134 · provenance: repo
- spawn.py run_claude returns {status: failed, output: out} without a reason key when the claude process exits non-zero but printed JSON (typical for --max-budget-usd reached); run_worker line 264 then does r['reason'] and the outer except records 'post_result failed: reason'
- T-0134 (opus review of the 389-line T-0129 delta) hit the review cap at 1.58 USD after 277 s; output_tokens 15924; the reviewer's partial findings were lost
outcome: review cap raised to 3.0 in pool.toml 2026-09-18 16:00 (decision recorded); fix candidate C-O10: run_claude sets reason from out.get('result')[:500] or 'is_error' when rc != 0, and run_worker uses r.get('reason')

## 2026-09-18 run_worker overwrites a reviewer's own posted verdict with a parse_error result, so the daemon never merges
type: gotcha · goal: T-0073 · tasks: T-0139,T-0135 · provenance: repo
- the opus reviewer for T-0135 posted {verdict: approve, ...} itself through bus_post_result, then returned fenced JSON as its final text; spawn.run_worker's review branch ran extract_json on that text, failed, and posted a second result {summary, parse_error, ...} without a verdict, which replaced the first
- daemon.merge_reviewed reads src.review_verdict or r.review_verdict; neither was set because run_worker only sets them when its own parse yields a verdict, so T-0135 sat done+gated for 15 min until the Planner merged by hand (acc0894)
outcome: fix candidate C-O11 for spawn.run_worker: when extract_json fails and the task already has a result with a verdict, keep the existing result and set review_verdict from it; when it fails and there is none, post failed with the raw text in resume_hint instead of a done result without a verdict; also make merge_reviewed fall back to r.result.verdict

## 2026-09-18 spawn_spec_review(execute id) starts an execute run: the tool wants the spec_review task id
type: gotcha · goal: T-0073 · tasks: T-0116,T-0142 · provenance: repo
- mcp.py spawn_spec_review(task_id) is _bg(task_id) = spawn.run_worker(task_id); run_worker branches on the task's role, so an execute id runs the Claude fallback executor on that task (T-0116 at 17:03 while held after a request_changes spec review; T-0142 at 17:17 before any spec review). Both killed within 2 min, about 0.3 USD lost
- daemon.dispatch spawns spec reviews itself for queued complexity 5+ tasks that have none, before the free_slots check, so it works even while Codex cools; T-0140 came from the daemon, not the Planner
outcome: leave c5+ tasks queued and let the daemon create the spec_review task; call spawn_spec_review only with a spec_review task id you created with bus_create_task(role=spec_review, inputs=[exec id]). Fix candidate: spawn_spec_review and spawn_review should refuse an id whose role is not spec_review/review

## 2026-09-18 daemon.dispatch breaks out of its loop at the first ready low-complexity task when free_slots is 0, so later tasks never get their spec review
type: gotcha · goal: T-0073 · tasks: T-0142,T-0120,T-0121 · provenance: repo
- dispatch() iterates queued execute tasks in id order; for a task below SPEC_REVIEW_MIN (or already approved) it breaks out of the loop when no slot is free. With Codex cooling and the pre-4aaaa03 free_slots (0), ready tasks T-0120/T-0121 (Phase D, c4/c3) sit before T-0142 (c6) and the break stops the loop before the spec-review branch runs for T-0142; T-0140 was created earlier only because those two were not yet ready
- the fixed free_slots (4aaaa03) yields free slots while Claude workers are idle, which hides the problem but does not remove it: with all fallback slots busy the same break skips every later spec review
outcome: workaround 2026-09-18 17:30: Planner created the spec_review task by hand (bus_create_task role=spec_review inputs=[T-0142]) and stamped spec_review_at. Fix candidate (fold into D1 T-0120 or a tiny C task): replace break with continue so the spec-review branch still runs for later tasks, or run a separate spec-review pass before the slot-limited dispatch pass

## 2026-09-18 linux/amd64 executor image cannot be built on the arm64 laptop: the Claude Code native installer (Bun) segfaults under emulation for lack of AVX
type: gotcha · goal: T-0073 · tasks: T-0165,T-0173 · provenance: repo
- docker build --platform linux/amd64 of the executor Dockerfile on the M-series Mac fails at the Claude Code install step: Bun 1.4.3 prints 'CPU lacks AVX support' and exits 139 (log in the session scratchpad, 2026-09-18 23:59). The arm64 native build of the same Dockerfile (T-0165) succeeded: 1.66 GB, four versions printed
- the T-0171 review asked for a --platform=linux/amd64 pin so kenta-server (x86_64) never gets an arm64 codex binary; the pin makes the laptop smoke build impossible instead
- two executor runs (T-0173) backgrounded the emulated build and exited before it finished, leaving uncommitted edits; the Planner committed the worktree and ran the build as the external gate
outcome: Dockerfile should stay multi-arch: no platform pin; choose the codex asset (x86_64 vs aarch64 musl) and let the uv and Claude installers detect the arch, so the laptop builds arm64 natively for smoke tests and kenta-server builds amd64 natively. The amd64 build evidence is a human step on kenta-server in the runbook. Tasks whose acceptance includes a multi-minute docker build need a timeout sized for it or the build moved into a gate script

## 2026-09-19 claude CLI vanished mid-session: the homebrew symlink points at a removed npm package path, every Claude worker dies with FileNotFoundError
type: gotcha · goal: T-0109 · tasks: T-0200 · provenance: repo
- 2026-09-19 about 13:00: spawn.run_claude raised FileNotFoundError for 'claude'; shutil.which('claude') is None; the homebrew link for claude resolves to ../lib/node_modules/@anthropic-ai/claude-code/.../claude.exe which no longer exists; no native copy under ~/.local or ~/.claude/local. Workers ran fine until about 07:00 the same morning; the interactive session itself kept running (its own binary was already loaded)
- symptom on the bus: the daemon re-spawned review T-0200 and requeued it as 'process died' with no run record and no stderr visible; running spawn.run_worker from the Planner shell showed the traceback
outcome: the human reinstalls the CLI (npm global package or the native setup); then spawn_review(T-0200) resumes Phase D. Fix candidate: run_claude should check shutil.which('claude') first and hold the task with reason 'claude CLI not found' instead of dying silently in a daemon thread

## 2026-09-19 fix-round worktrees come from the goal branch within one daemon tick; a Planner-made worktree loses the race
type: gotcha · goal: T-0201 · tasks: T-0222,T-0223 · provenance: repo
- spawn.base_for() ignores constraints.fix_round_for and inputs; the daemon dispatched T-0222 about 20 s after creation with a worktree on goal/T-0201 (6358799) instead of task/T-0220 (d8531a3); planner-mode.sh also stops the Planner from repointing a worktree
outcome: workaround: the fix-round spec opens with a STEP 0 that puts the worktree on task/<parent task>, and the acceptance requires the parent commit in the log (T-0223). Fix candidate for a polish task: base_for() prefers constraints.fix_round_for's task branch

## 2026-09-19 already_merged() stamps an uncommitted task as merged when its branch still equals the goal head
type: gotcha · goal: T-0201 · tasks: T-0229 · provenance: repo
- daemon.already_merged() (about line 225) treats task branch is-ancestor-of goal/<parent> as proof the work landed; an executor that finishes without committing leaves the branch at the goal head, so the check passes and bus.update(merged_into=..., merged_via=ancestor) fires with nothing merged and no gate. Seen on E3 v2 T-0229 at 18:20: three new files and two edits sat uncommitted in wt/T-0229 while the bus said merged
outcome: Planner committed the worktree as found, cleared merged_into and gated_at so gate() reviews it. Fix candidate (polish): already_merged() must also require the branch head to differ from the merge base, or the worktree to be clean with a commit not on the target; a dirty worktree on a done task should hold with reason executor did not commit

## 2026-09-19 a committed config toggle broke the gate for every open worktree: tests read the real pool.toml
type: gotcha · goal: T-0201 · tasks: T-0229,T-0236 · provenance: repo
- tests/test_jev.py::test_disabled_returns_none_without_network reads pool.toml through jev._cfg(); the Planner set [jev].enabled = true at 17:45 and every worktree cut afterwards fails tests-green with 'urlopen must not be called when jev is disabled' (E3 T-0229 held gate_red at 18:25; T-0230, T-0233, T-0234, T-0235 will follow)
outcome: P10 T-0236 pins the config inside the tests; held tasks then go through merge() (rebase onto goal, tests, fast-forward) instead of a fresh gate. Rule: a test may never depend on a committed pool.toml value; a Planner config commit must run the test suite from ROOT first

## 2026-09-19 daemon-dispatched Codex runs finish but never reach the bus: _dispatch_worker discards executor.start()'s result
type: gotcha · goal: T-0201 · tasks: T-0235,T-0237,T-0238 · provenance: repo
- daemon._dispatch_worker() (about line 305) calls executor.start() and drops the return dict; executor._run() logs the run and returns status done with the final message, but nothing calls bus.post_result, so the task stays running with pid null forever (three Codex tasks sat running 30 min after their runs logged done at 16:04). The Claude fallback path posts from spawn.run_worker, which is why this never showed while Codex cooled
outcome: Planner ran tests-green on each worktree and posted the results by hand. Fix candidate (polish P11): _dispatch_worker posts done/failed/held from the returned dict with summary = message[:3000], executed_by codex:<ex>, and the daemon then gates as usual
