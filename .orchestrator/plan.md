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

## Next step
T-0011 running (eval execute) → gate → merge into goal/T-0004 → push → PR. Then codex(T-0015) (same test file; serialized). Read T-0012/T-0013/T-0014 → synthesize B → specs: (1) pool.toml [[executors]] + pool.py pick_executor(role, complexity, health) + quota_group cooldowns; (2) executor.py uses executor.model / -p profile, per-executor state; (3) scorecard (cli `orchestrator scorecard`, from runs jsonl + bus); (4) bench.json snapshot format + `orchestrator bench set` + routing weights; (5) docs. Old next step:
create child tasks (A1 scout; B1–B4 scouts) → spawn_scout each → batch bus_events → A: write-spec + codex(fallback) while B scouts run.
