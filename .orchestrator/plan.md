# plan.md — Planner checkpoint (2026-09-17 17:15)

Two goals in flight. Codex cooling until 2026-09-19 14:00 → execute tasks go through executor_fallback (sonnet ≤5, opus 6–8, hold ≥9).
main = 7d98760 (PR 2 merged: planner mode, Planner may commit). Stale worktrees wt/T-0002, wt/T-0003 remain (removal = deletion, needs human OK).

## Goal A — eval 001: `orchestrator status --plain` (complexity 2)
Scouts: 1 repo-map scout on account B (spawn_scout) — the eval exists to exercise the scout path; cli.py:19-35 is already known (status prints JSON of Pool().status()+queue).
Execute: 1 task, scope [orchestrator/cli.py, tests/**], fallback → sonnet. Review: hooks only. merge(id) → goal/<A>. Push, PR, retrospective.

## Goal B — multi-model executor pool (complexity 7)
User words: add GPT Luna, Terra, Sol (5.6 now, 6 later) to balance/manage work; adjust as live as possible; evaluate what we use for what via artificialanalysis.ai/models.
Known now: pool.toml has a single [codex] model = gpt-6-astra; executor._run passes `-m`; fallback_tier maps complexity→claude tier; runs/*.jsonl logs tokens/duration/outcome per task; bus results carry rounds, failures, review verdicts.
Scouts (account B, sonnet unless noted):
- B1 web: artificialanalysis.ai/models — data access (API/JSON/page), metrics available (intelligence, coding index, speed, price), presence of Luna/Terra/Sol 5.6 and Astra, robots/ToS. provenance web → untrusted.
- B2 local tooling: Codex CLI 0.154 — accepted model ids (luna/terra/sol 5.6?), per-model vs per-account usage limits, `codex exec -m` behaviour, where limits reset text appears. provenance repo+tool output.
- B3 repo trace-callers: every read of pool.cfg["codex"], fallback_tier, tier→model map, `-m` in executor; minimal-change slots for a [[executors]] table + routing policy.
- B4 repo telemetry gap: what per-task outcome data exists (runs jsonl, bus result fields, tests-green, review verdicts) vs what a per-model scorecard needs (success rate, rounds to green, cost, duration, cooldown hits); propose the scorecard record.
Challenge any <0.7 finding acted on. Then synthesize → specs (≤5 files each, disjoint scopes): (1) pool.toml [[executors]] + pool.py selection by role/complexity/health; (2) executor.py per-model cooldown + model arg; (3) scorecard from runs jsonl + `orchestrator scorecard` CLI; (4) bench.py: fetch artificialanalysis snapshot → .orchestrator/bench.json (data only) + routing weights; (5) README/docs. Review: cross-model adversarial + security checklist for (1),(2),(4) (network + config). Model ids for 6.x: placeholder entries disabled until released.

## Findings so far (17:40)
- A/T-0006 (0.85–0.95): insert --plain at cli.py:22 (parser) and cli.py:30-31 (branch); Pool.status fields: accounts[{id, utilization, day_tokens, daily_budget, cooling_s, reason}], codex{available, running, day_tasks, cooling_s, on_exhausted}; no CLI test exists yet. → T-0011 execute (fallback sonnet).
- B/T-0007 web (untrusted): NO public API; data access is a paid product. Terms of Use forbid automated scraping (0.85). → design: benchmark input is a hand-taken snapshot file .orchestrator/bench.json with source URL + date + who took it, never a fetcher. Metric labels: Intelligence Index, Output Speed (tokens/s), Latency, Price ($/M tokens), Context Window; coding = "Coding Agents" section. Claim that Luna/Terra/Sol 5.6 and Opus 5/Sonnet 5/Haiku 4.5 are absent (0.6) → challenge T-0014.
- B/T-0008 (0.95): codex model via `-m`, `-p <profile>` layers $CODEX_HOME/<name>.config.toml, `-c model=`. Usage-limit scope per account vs per model: UNKNOWN (0.3) → design per-executor cooldown + `quota_group` so a limit hit on one model can cool its group.
- Planner direct check of ~/.codex/models_cache.json (2026-09-16 22:34): ids present = gpt-5.6-luna, gpt-5.6-sol, gpt-5.6-terra, gpt-6-astra, gpt-5.5 (+ codex-auto-review, gpt-reserve, priority). T-0008 finding "no luna/terra/sol on disk" was WRONG (grep pattern gpt-luna* too narrow) despite 0.85 confidence → gotcha: verify negative findings with a direct listing.
- T-0009/T-0010 lost: results > MAX_RESULT_CHARS raised inside the worker thread; tasks stuck "running". → T-0015 execute (spawn.fit_result + fail-safe). Capped reruns T-0012/T-0013 running.

## Goal B synthesis (17:55) — from T-0007, T-0008, T-0012, T-0013 + Planner check of models_cache.json
Design:
1. pool.toml `[[executors]]` replaces the single `[codex]` model: id, provider (codex|claude), model (gpt-6-astra, gpt-5.6-luna, gpt-5.6-terra, gpt-5.6-sol; gpt-6-luna/terra/sol as enabled=false placeholders), roles, complexity_min/max, max_parallel, daily_budget_tasks, quota_group ("chatgpt" for all codex ids: limit scope unknown → a usage-limit hit cools the whole group by default; per-model when observed otherwise), weight. Keep [codex] keys home/dangerous_full_access/on_exhausted as provider settings. Drop dead key pace_reserve.
2. pool.py: per-executor state in pool_state.json (cooldown_until, day_tasks, running) keyed by executor id; `Pool.pick_executor(role, complexity)` = enabled ∧ role ∧ complexity range ∧ not cooling ∧ under max_parallel/day budget, ranked by weight × scorecard success rate (default 1.0 when no data) with round-robin tie-break; `codex_available()` becomes "any codex executor available". fallback_tier unchanged.
3. executor.py: start/reply take the chosen executor; `-m executor.model`; cooldown/usage-limit writes go to that executor and its quota_group; log_run gains executor id + complexity; codex usd left None (not exposed) but tokens normalized: cached_input_tokens → cache_read_input_tokens.
4. Scorecard: `orchestrator scorecard [--by executor|tier]` aggregates runs/*.jsonl + tasks/*.json: merged, failed, rounds_avg, wall_s, tokens, usd, usage_limit_hits, review_request_changes, per complexity bucket. Written to .orchestrator/scorecard.json on demand and after each merge (merge.py calls it). Review verdict: spawn.run_worker parses {"verdict"} from review results and bus.update(task, review_verdict=...).
5. Benchmark input: NO scraper (ToS). `.orchestrator/bench.json` = {source, taken_at, taken_by, models:{id:{intelligence, coding, speed_tps, price_in, price_out}}} entered by hand (`orchestrator bench set <model> key=value ...`); pick_executor uses bench.coding as a prior weight when the scorecard has <5 runs for that executor. Provenance web → data only.
Task split (serial, stacked on goal/T-0005, each ≤5 files; tests in tests/test_orchestrator.py so strictly one at a time):
- T-0015 spawn.fit_result (running next) · B1 pool.toml+pool.py executors+pick_executor+state (complexity 6 → opus fallback; review: sonnet other account + security checklist since config parsing) · B2 executor.py per-executor -m/cooldown/quota_group + log fields (6) · B3 scorecard + review_verdict capture + cli (5) · B4 bench.json + cli bench set + prior weight (4) · B5 README/AGENTS docs + pool.toml comments (2).
Open: Luna/Terra/Sol numbers on artificialanalysis.ai not found (0.6, unchallengeable via repo template) — user can eyeball the page; snapshot stays empty until then.

## Rollback
Each merge lands on goal/<id>; main moves only by PR. Every Planner commit -F message names revert; decisions.md entry per goal.

## Progress (18:10)
- Goal A CLOSED: PR 3 goal/T-0004 → main open (152bcb9). T-0004 done on the bus.
- Goal B: goal/T-0005 stacked on goal/T-0004 (so the shared test file never conflicts between PRs). Merged: c6ed6a5 memory, cd5048a T-0015 fit_result. Pushed.
- Specs queued: T-0016 B1 (running, opus fallback, account A) · T-0017 B2 · T-0018 B3 · T-0019 B4 · T-0020 B5 · T-0021 review of B1 (spawn after B1 lands).
- Main checkout is on goal/T-0005; after each merge() refresh with `git checkout HEAD -- <merged files>` (update-ref leaves the worktree stale).
- Each B task's worktree must be created by the Planner off goal/T-0005 (ensure_worktree defaults to origin/main): `git worktree add wt/<id> -b task/<id> goal/T-0005`, then codex(<id>).

## Update 18:25
- PR 3 merged (main 2989695). goal/T-0005 is now a plain branch off main history (stack resolved).
- User decision: B4 uses Scrapling to fetch artificialanalysis.ai/models (ToS concern raised and overruled). T-0019 superseded → scout T-0022 (Scrapling API + page data shape) → new B4 spec: bench.py fetch via Scrapling (plain Fetcher if the table or a JSON route is static; DynamicFetcher only if unavoidable, browser install documented), `orchestrator bench fetch` manual/daily cap, UA identifies the orchestrator, provenance web, hand `bench set` kept as fallback; pyproject adds scrapling (state which extras). Prior-weight logic unchanged.

## Update 18:40 — B4 redesigned from scout T-0022
Page: HTTP 200 plain, Vercel, no anti-bot; data in ~143 `self.__next_f.push([1,"..."])` RSC chunks (keys seen: intelligenceIndex, outputSpeed); no table, no JSON route. Scrapling 0.4.15 plain `Fetcher.get` suffices; no browser extras.
Tasks: T-0023 B4a (bench.py fetch+parse_chunks+match+bench.json provenance, cli bench fetch/show/set, fixture-based tests, `uv add scrapling`; complexity 5 → sonnet review other account + security checklist because network+dependency) · T-0024 B4b (scorecard prior from bench.json; 3). Order after B1: B2 T-0017 → B3 T-0018 → B4a T-0023 → B4b T-0024 → B5 T-0020 (its spec says "bench hand-entered; no scraper" — B5's README text must instead say: fetched with Scrapling at most every 20 h, identified UA, untrusted data; give the executor that correction in the codex() prompt since bus specs are immutable).
After B4a merges: Planner runs `uv run orchestrator bench fetch --force` once and records matched/unmatched in memory.

## Update 19:00 — B1 review
T-0016 (opus@B, 361b7ce) gate green; review T-0021 (sonnet@B, same account: affinity gotcha recorded) → request_changes: HIGH codex_available probes complexity 1 → can dispatch a complexity-8 task past astra's caps; MED legacy sync max() ratchets running up; also concurrent Pool() instances save last-writer-wins snapshots of pool_state.json (pre-existing; follow-up: file lock around load/mutate/save like merge.lock).
Fix round = T-0025 (Claude fallback tasks have no codex_reply thread; a follow-up execute task on the same branch is the equivalent). Worktree wt/T-0025 off task/T-0016. Merge order: T-0025 → goal/T-0005 (carries 361b7ce) → mark T-0016 merged → B2.
Follow-ups (not yet specced): review affinity on account A; pool_state lock; challenge-web template; tests-green prefers uv run.

## Update 19:15
- B1 + fix round merged: goal/T-0005 = 5cc7d0b (361b7ce executors table, 5cc7d0b review fixes). Pushed.
- B2 T-0017 running (opus fallback expected, complexity 6). Review task T-0026 pre-created (spawn_review after gate).
- Remaining order: B2 → B3 T-0018 → B4a T-0023 → B4b T-0024 → B5 T-0020 (with README correction: Scrapling fetch, 20 h cap, untrusted) → retrospective → commit memory → PR goal/T-0005 → main.

## Update 19:35 — B2 review
- T-0017 (opus@A, a01dc3c) gate green. Review T-0026 VOID: its worktree came from origin/main (no B1) → all 4 findings artefacts. Rerun as T-0028 on wt/T-0028 off task/T-0017 (running).
- New task B6 T-0029: base_for() (review/challenge worktrees off the reviewed branch, execute off goal/<parent>), scoped_diff vs goal branch, review affinity on A + skip the executing account. Runs after B2 merges (spawn.py overlap). Order now: B2 → B6 → B3 → B4a → B4b → B5.

## Update 19:50
- B2 merged (a01dc3c; rerun review T-0028 approve, 2 low: legacy codex.day rollover only via status(); getattr(pool,'scores',dict)() shape — B3 must expose scores as a callable or executor.py adapts). goal/T-0005 pushed.
- B6 T-0029 running (sonnet fallback); review task pre-created (next id). Then B3 T-0018 → B4a T-0023 → B4b T-0024 → B5 T-0020.
- B3 codex() prompt must add: "executor.start currently calls getattr(pool,'scores',dict)(); keep scores a zero-arg callable or change that call site (executor.py is in B3 scope)".

## Update 20:05
- B6 T-0029 (sonnet@A, 3808258) gate green; review T-0030 running on wt/T-0030 (pre-created off task/T-0029 because the MCP server still runs pre-B6 spawn.py until merge). After B6 merges, the server process must be restarted to pick up base_for (it imports spawn.py at start) — otherwise keep pre-creating worktrees.
- B3 review task pre-created (T-0031, inputs T-0018).

## Update 20:20
- B6 review T-0030: request_changes (MED challenge inputs are dicts → base_for dead path; LOW 6.x rows lack quota_group). Fix round T-0032 running (sonnet) on wt/T-0032 off task/T-0029. Merge T-0032 → goal/T-0005 (carries B6) after Planner diff check (complexity 2).
- Then B3 T-0018: worktree off goal/T-0005; codex() prompt must mention getattr(pool,'scores',dict)() call site in executor.start. Review T-0031 pre-created; pre-create wt/T-0031 off task/T-0018 unless the MCP server was restarted with B6 code.

## Update 20:35
- B6 + fix merged: goal/T-0005 = 7b0e2ef (3808258 base_for/affinity, 7b0e2ef challenge base + quota test). Pushed. Graph refreshed (393 nodes).
- B3 T-0018 running (worktree off goal/T-0005). Review T-0031 pre-created → pre-create wt/T-0031 off task/T-0018 before spawn_review (server still runs pre-B6 spawn.py).
- Remaining: B4a T-0023 (5, review + security list: network + dependency) → B4b T-0024 (3) → B5 T-0020 (2, prompt correction re Scrapling) → retrospective → commit memory on goal/T-0005 → push → PR goal/T-0005 → main → Planner runs `uv run orchestrator bench fetch --force` once after B4a merges and records matched/unmatched.

## Update 20:50
- B3 T-0018 (sonnet@B, 1b5a17f) gate green; review T-0031 (sonnet@A) request_changes: MED double-count of review verdicts (review tasks bucketed as claude:sonnet). Fix round T-0034 on wt/T-0034 off task/T-0018. Merge T-0034 → goal/T-0005 (carries B3) after Planner diff check (complexity 2).
- Then B4a T-0023 (worktree off goal/T-0005; review T-0033 with security checklist, pre-create wt/T-0033 off task/T-0023) → B4b T-0024 → B5 T-0020.

## Update 21:05
- B3 + fix merged: goal/T-0005 = 815af4d. Pushed.
- B4a T-0023 running (sonnet fallback; worktree off goal/T-0005). Review T-0033 (security checklist) pre-created → pre-create wt/T-0033 off task/T-0023 before spawn_review.
- Then B4b T-0024 → B5 T-0020 → retrospective → memory commit → PR.

## Update 21:25
- B4a T-0023 (sonnet@A, 67adc81): green inside its own venv, but (a) my external gate and merge()'s gate use the main venv → ImportError scrapling; (b) it added scrapling[fetchers] (pulls playwright, patchright, browserforge, curl-cffi) against the spec.
- Fixes running in parallel (disjoint scopes): T-0035 tests-green.sh under `uv run --project .` (worktree off goal/T-0005) · T-0036 minimal dependency (worktree off task/T-0023).
- Merge order: T-0035 first (its gate is python-only), refresh checkout so ROOT hook is the new one → merge(T-0036) (carries 67adc81; gate now resolves the worktree venv) → then `uv sync` in the main checkout → security review T-0033 on wt/T-0033 off task/T-0036 → B4b T-0024 → B5 T-0020.
- No live bench.json yet: worker did not run the live fetch (or wrote nowhere). Planner runs `uv run orchestrator bench fetch --force` after merge + uv sync.

## Update 21:45
- T-0035 merged (09ef8d1): tests-green runs `uv run --project .` in the target dir; main checkout refreshed, so merge()'s gate now resolves worktree venvs.
- T-0036 (74bf13a) green in its venv: scrapling + curl_cffi + playwright + patchright + browserforge as explicit deps (Fetcher hard-imports them; no browser installed). Live fetch OK: 200, 25 names, 0 matched → B4c matcher fix (strip parentheticals, prefer "(max)").
- Luna/Terra/Sol/Opus 5/Fable 5.1 ARE on the site (T-0007 wrong).
- Order: merge(T-0036) → uv sync main → B4c (off goal) → security review T-0033 on the final bench.py (pre-create worktree off B4c branch) → B4b T-0024 → B5 T-0020 → retrospective → memory commit → PR.

## Update 22:00
- T-0036 merged (019cb09). Main venv synced (scrapling imports). goal/T-0005 pushed. B4c T-0037 running (worktree off goal).
- Next: gate T-0037 with the new hook straight from the main checkout (no uv prefix needed now) → merge → pre-create wt/T-0033 off goal/T-0005 → spawn_review(T-0033, security checklist on final bench.py) → B4b T-0024 → B5 T-0020 → retrospective.

## Update 22:20
- B4c merged (e29a97e): live fetch matches 6/8 (astra, luna, terra, sol, opus 5, fable 5.1; haiku/sonnet 5 absent on the site). Numbers: intelligence, speed, price; no coding index in the page chunks → prior uses intelligence.
- Security review T-0033: request_changes — HIGH Scrapling defaults impersonate chrome + stealth headers (evasion, contradicts the identified-fetch decision); MED http_status literal 200; MED matcher (already fixed). → B4d T-0038 (worktree off goal after B4b merge).
- B4b T-0024 (763986d) gate green → merging now.
- Then B4d → re-review T-0033 findings by a fresh review task on the final bench.py → B5 T-0020 (prompt correction: README says Scrapling fetch, identified UA, ≥20 h, untrusted) → retrospective → memory commit → PR goal/T-0005 → main.

## Update 22:45
- B4b merged (763986d). B4d T-0038 (913a5a9) gate green; live identified fetch: 200, 6 matched, provenance request{impersonate:false, stealth_headers:false}. Re-review T-0039 running on wt/T-0039 (off task/T-0038). B5 T-0020 running in parallel on wt/T-0020 (off goal; disjoint scope; prompt carried the Scrapling correction).
- After both: merge(T-0038) if approve → merge(T-0020) → refresh → retrospective (record.sh draft T-0005 → add decisions + model-notes for sonnet/opus fallback performance) → commit memory on goal/T-0005 (-F message file) → push → PR goal/T-0005 → main (body: what/why/verification/rollback; labels same-family-review) → close T-0005 on the bus → plan.md empty.

## ESCALATION 23:00 — pool window exhausted by the calibration knob
Both accounts show no headroom in the pool's own accounting: window_cap_tokens = 2M (pool.toml, uncalibrated per README §13) reached in 2.2 h, mostly cache reads /10 (B 24M cache reads, A 15M). Zero real rate limits today. Window rolls ~5 h after it started (≈01:40). Held: T-0039 re-review of bench.py, T-0020 docs. Planner does not override state; asks the human to raise window_cap_tokens (suggest 8M) or wait for the roll. Monitor armed for headroom (A<0.55 or B<0.90); on event: spawn_review(T-0039) (wt/T-0039 exists off task/T-0038) and codex(T-0020) (wt/T-0020 exists off goal).
- B4d T-0038 merged (913a5a9) on Planner diff check; T-0039 stays as post-merge audit. goal/T-0005 pushed. Retrospective written (decisions, model-notes). Next: commit memory (-F), draft PR goal/T-0005 → main noting docs + audit pending.

## Next step
T-0016 result → gate (uv run tests-green wt/T-0016) → spawn_review(T-0021) → if approve: merge(T-0016) → refresh → worktree T-0017 → codex(T-0017) → review task for B2 → ... B3, B4 (hooks-only for ≤3, sonnet review for 4–6) → B5 → retrospective (record.sh) → commit memory on goal/T-0005 → push → PR goal/T-0005 → main (note: stacked on PR 3; merge PR 3 first).
Old:
T-0011 running (eval execute) → gate → merge into goal/T-0004 → push → PR. Then codex(T-0015) (same test file; serialized). Read T-0012/T-0013/T-0014 → synthesize B → specs: (1) pool.toml [[executors]] + pool.py pick_executor(role, complexity, health) + quota_group cooldowns; (2) executor.py uses executor.model / -p profile, per-executor state; (3) scorecard (cli `orchestrator scorecard`, from runs jsonl + bus); (4) bench.json snapshot format + `orchestrator bench set` + routing weights; (5) docs. Old next step:
create child tasks (A1 scout; B1–B4 scouts) → spawn_scout each → batch bus_events → A: write-spec + codex(fallback) while B scouts run.
