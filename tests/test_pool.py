"""Account pool selection (PoolSel), the [[executors]] routing table (Executors), and Planner-transcript token
tallying (PlannerTally): bands, quota groups, cooldowns, budgets, scored ranking."""
import io, json, shutil, sys, time, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_pool.py` doesn't add this dir itself
from _harness import REPO, TMP  # noqa: F401
from orchestrator import pool as P, spawn


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _assistant(ts, input_tokens=0, output_tokens=0, cache_read_input_tokens=0):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {"usage": {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens}}}) + "\n"


def _user(ts):
    return json.dumps({"type": "user", "timestamp": ts, "message": {"usage": {"input_tokens": 999}}}) + "\n"


class PoolSel(unittest.TestCase):
    def setUp(self):
        P.PERSIST.unlink(missing_ok=True); P.PLANNER_USAGE.unlink(missing_ok=True); self.p = P.Pool()

    def test_affinity_reserve_cooldown_budget(self):
        self.assertEqual(self.p.pick("review").id, "A")            # both have review affinity, ties break to A
        A, B = self.p.get("A"), self.p.get("B")
        A.window_tokens = int(self.p.cap * 0.7); self.p.save()       # above 1-reserve(0.35)=0.65 -> scouts go to B;
                                                                      # day_tokens left at 0 so A's daily budget (below
                                                                      # the reserve ceiling at this cap) doesn't also exclude it
        self.assertEqual(self.p.pick("scout").id, "B")
        self.assertEqual(self.p.pick("planner").id, "A")            # planner ceiling is 1.0
        self.p.cooldown(B, 600); self.assertIsNone(self.p.pick("review"))  # held, not failed
        self.p.resume("B"); self.assertEqual(self.p.pick("review").id, "B")
        B.day_tokens = B.daily_budget; self.assertIsNone(self.p.pick("review"))
        self.assertEqual(P.Pool().get("A").window_tokens, A.window_tokens)  # persisted across restarts

    def test_pick_review_avoids_executing_account(self):
        self.assertEqual(self.p.pick("review", avoid="B").id, "A")   # B executed it; A has headroom
        self.assertEqual(self.p.pick("review", avoid="A").id, "B")
        self.p.cooldown(self.p.get("A"), 600)
        self.assertEqual(self.p.pick("review", avoid="B").id, "B")   # only B has headroom -> avoid is ignored

    def test_rate_limit_parsing_and_fallback(self):
        self.assertTrue(P.is_rate_limited("Error: You've hit your usage limit. Resets in 2h 15m"))
        self.assertEqual(P.parse_reset_hint("resets in 2h 15m"), 8100)
        self.assertEqual(P.parse_reset_hint("try again in 30 minutes"), 1800)
        self.assertEqual(P.parse_reset_hint("retry-after: 900"), 900)
        self.assertEqual(P.parse_reset_hint("nothing useful", default=42), 42)
        far = P.parse_reset_hint("You've hit your usage limit. Visit https://x to purchase more credits or try again at Sep 19th, 2099 2:00 PM.")
        self.assertGreater(far, 365 * 24 * 3600)                     # absolute date parsed, not the 1800 default
        self.assertEqual(P.parse_reset_hint("try again at Sep 19th, 2000 2:00 PM"), 60)  # past -> floor
        self.assertEqual([P.fallback_tier(c) for c in (3, 7, 9)], ["sonnet", "opus", None])
        self.assertTrue(P.is_rate_limited("You're out of usage credits. Switch to another model"))
        self.assertFalse(P.is_rate_limited("all good"))

    def test_trust_workspace(self):
        cfg = TMP / "prof"; spawn.trust_workspace(str(cfg), TMP / "wt" / "T-0099")
        self.assertTrue(json.loads((cfg / ".claude.json").read_text())["projects"][str(TMP / "wt" / "T-0099")]["hasTrustDialogAccepted"])


class Executors(unittest.TestCase):
    """[[executors]] routing: complexity bands, disabled placeholders, quota-group cooldowns, scored ranking."""
    LIVE = {"astra", "luna", "terra", "sol"}

    def setUp(self):
        P.PERSIST.unlink(missing_ok=True); P.PLANNER_USAGE.unlink(missing_ok=True); self.p = P.Pool()

    def test_bands_and_enabled(self):
        self.assertIn(self.p.pick_executor("execute", 3).id, self.LIVE)
        self.assertEqual(self.p.pick_executor("execute", 8).id, "astra")   # only astra reaches complexity 8
        self.assertIsNone(self.p.pick_executor("review", 3))               # no executor takes that role
        self.assertEqual({e.id for e in self.p.executors.values() if e.enabled}, self.LIVE)
        for eid in ("luna6", "terra6", "sol6"):
            self.p.executors[eid].weight = 99.0                            # disabled wins nothing, whatever its weight
        self.assertIn(self.p.pick_executor("execute", 3).id, self.LIVE)

    def test_quota_group_cooldown_and_roundtrip(self):
        self.p.cooldown_executor("luna", 600, "usage limit")
        self.assertTrue(all(self.p.executors[i].cooling() for i in self.LIVE))  # one member cools the group
        self.assertIsNone(self.p.pick_executor("execute", 8))
        self.assertIsNone(self.p.pick_executor("execute", 3))
        fresh = P.Pool()                                                   # state survives an MCP restart
        self.assertTrue(fresh.executors["astra"].cooling())
        self.assertFalse(fresh.codex_available())

    def test_scores_and_limits(self):
        self.assertEqual(self.p.pick_executor("execute", 3, {"terra": 3.0}).id, "terra")
        self.p.executors["terra"].running = self.p.executors["terra"].max_parallel
        self.assertNotEqual(self.p.pick_executor("execute", 3, {"terra": 3.0}).id, "terra")
        self.p.executors["astra"].day_tasks = self.p.executors["astra"].daily_budget_tasks
        self.assertIn(self.p.pick_executor("execute", 3).id, {"luna", "sol"})
        st = self.p.status()
        self.assertEqual((len(st["executors"]), sum(e["enabled"] for e in st["executors"])), (7, 4))

    def test_pick_by_expected_cost_with_floor(self):
        from orchestrator import scorecard
        task = {"title": "small", "complexity": 3}
        old_cost, old_build = scorecard.expected_cost, scorecard.build
        scorecard.expected_cost = lambda eid, cls: {"luna": 10, "terra": 20, "sol": 30, "astra": 40}.get(eid)
        scorecard.build = lambda: {"luna": {"merged": 1, "failed": 1}, "terra": {"merged": 3, "failed": 0},
                                   "sol": {"merged": 3, "failed": 0}, "astra": {"merged": 3, "failed": 0}}
        self.addCleanup(lambda: setattr(scorecard, "expected_cost", old_cost))
        self.addCleanup(lambda: setattr(scorecard, "build", old_build))
        self.assertEqual(self.p.pick_executor("execute", 3, task=task).id, "terra")

    def test_pick_falls_back_without_samples(self):
        from orchestrator import scorecard
        old_cost = scorecard.expected_cost
        scorecard.expected_cost = lambda *args, **kwargs: None
        self.addCleanup(lambda: setattr(scorecard, "expected_cost", old_cost))
        self.assertEqual(self.p.pick_executor("execute", 3, {"terra": 3.0}, {"complexity": 3}).id, "terra")

    def test_missing_table_synthesizes_legacy_row(self):
        cfg = {k: v for k, v in P.config().items() if k != "executors"}
        old = P.Pool(cfg)
        self.assertEqual([e.model for e in old.executors.values()], [cfg["codex"]["model"]])
        self.assertTrue(old.codex_available())

    def test_codex_available_respects_complexity(self):
        self.p.executors["astra"].day_tasks = self.p.executors["astra"].daily_budget_tasks
        self.assertFalse(self.p.codex_available(8))   # only astra reaches band 8, and it's over budget
        self.assertTrue(self.p.codex_available(3))     # luna/terra/sol still have headroom at band 3

    def test_legacy_running_syncs_down_not_just_up(self):
        self.p.codex.running = 2; self.p.save()
        fresh = P.Pool()
        self.assertEqual(fresh.executors["astra"].running, 2)
        fresh.codex.running = 0; fresh.save()
        fresher = P.Pool()
        self.assertEqual(fresher.executors["astra"].running, 0)  # regression: used to ratchet up only

    def test_every_executor_row_has_a_quota_group(self):
        # enabling a disabled placeholder later must not silently drop it out of its cooldown group (review T-0030)
        for row in P.config()["executors"]:
            self.assertTrue(row.get("quota_group"), row["id"])


class PlannerTally(unittest.TestCase):
    """Pool.tally_planner reads Claude Code's own transcript files for this project, since a live Planner session
    never posts a JSON result to run_claude the way a spawned worker does."""

    def setUp(self):
        P.PERSIST.unlink(missing_ok=True)
        P.PLANNER_USAGE.unlink(missing_ok=True)
        P._WARNED_MISSING_DIRS.clear()
        self.p = P.Pool()
        self.a = self.p.get("A")
        self.a.config_dir = str(TMP / "fake-claude-a")  # absolute path: os.path.expanduser is a no-op on it
        self.proj_dir = Path(self.a.config_dir) / "projects" / P.encode_project_dir(str(P.ROOT.resolve()))
        shutil.rmtree(self.proj_dir, ignore_errors=True)  # same TMP path is reused across tests in this process
        self.proj_dir.mkdir(parents=True)
        self.now = time.time()
        self.a.window_started = self.now - 100  # window = [now-100, now+17900); comfortably holds "now"
        self.ts_in = _iso(self.now)
        self.ts_out = _iso(self.now - 2 * 86400)  # a different calendar day -> excluded from both day and window

    def _write(self, name, lines):
        (self.proj_dir / name).write_text("".join(lines))

    def test_encode_project_dir_matches_claude_code(self):
        # verified against a real directory name under ~/.claude/projects/ on 2026-09-18
        self.assertEqual(P.encode_project_dir("/Users/k3ntaw/.claude-mem"), "-Users-k3ntaw--claude-mem")
        self.assertEqual(P.encode_project_dir("/Users/k3ntaw/code/orchestrator"), "-Users-k3ntaw-code-orchestrator")

    def test_tally_sums_assistant_usage_in_window(self):
        self._write("a.jsonl", [_assistant(self.ts_in, 100, 50, 20), _assistant(self.ts_out, 999, 999, 999)])
        self.p.tally_planner()
        n = 100 + 50 + 20 // 10
        self.assertEqual(self.a.planner_window_tokens, n)
        self.assertEqual(self.a.planner_day_tokens, n)

    def test_tally_skips_malformed_and_user_lines(self):
        self._write("a.jsonl", [_assistant(self.ts_in, 10, 5, 0), "not json at all\n", _user(self.ts_in)])
        self.p.tally_planner()
        self.assertEqual(self.a.planner_window_tokens, 15)

    def test_tally_resumes_from_offset(self):
        self._write("a.jsonl", [_assistant(self.ts_in, 10, 0, 0)])
        self.p.tally_planner()
        self.assertEqual(self.a.planner_window_tokens, 10)
        with (self.proj_dir / "a.jsonl").open("a") as f:
            f.write(_assistant(self.ts_in, 5, 0, 0))
        self.p.tally_planner()
        self.assertEqual(self.a.planner_window_tokens, 15)  # not 25: the first line isn't re-read

    def test_tally_prunes_missing_files(self):
        self._write("a.jsonl", [_assistant(self.ts_in, 10, 0, 0)])
        self._write("b.jsonl", [_assistant(self.ts_in, 5, 0, 0)])
        self.p.tally_planner()
        self.assertEqual(set(self.a.planner_offsets), {"a.jsonl", "b.jsonl"})
        (self.proj_dir / "b.jsonl").unlink()
        self.p.tally_planner()
        self.assertEqual(set(self.a.planner_offsets), {"a.jsonl"})

    def test_tally_resets_on_rollover(self):
        self._write("a.jsonl", [_assistant(self.ts_in, 10, 0, 0)])
        self.p.tally_planner()
        self.assertEqual(self.a.planner_window_tokens, 10)
        self.a.window_started = self.now - P.WINDOW_S - 10  # force a rollover
        self.a.utilization(self.p.cap)
        self.assertEqual(self.a.planner_window_tokens, 0)
        self.assertIn("a.jsonl", self.a.planner_offsets)     # offset itself survives the rollover
        with (self.proj_dir / "a.jsonl").open("a") as f:
            # +1s margin over the rolled window_started: millisecond truncation in _iso() could otherwise put an
            # exactly-"now" timestamp a fraction before the new window's start
            f.write(_assistant(_iso(time.time() + 1), 7, 0, 0))
        self.p.tally_planner()
        self.assertEqual(self.a.planner_window_tokens, 7)   # only the newly-appended line

    def test_tally_raises_utilization_and_flips_pick(self):
        self.assertEqual(self.p.pick("scout").id, "A")       # both idle -> ties break to A
        big = int(self.p.cap * 0.9)                          # over A's 0.65 scout ceiling (reserve_for_planner=0.35)
        self._write("a.jsonl", [_assistant(self.ts_in, big, 0, 0)])
        self.p.tally_planner()
        self.assertEqual(self.p.pick("scout").id, "B")

    def test_tally_gates_day_and_window_independently(self):
        self.a.window_started = self.now                     # window = [now, now+WINDOW_S)
        three_h_ago = self.now - 3 * 3600                     # still today, but before window_started
        self._write("a.jsonl", [_assistant(_iso(three_h_ago), 10, 5, 0)])
        self.p.tally_planner()
        self.assertEqual(self.a.planner_day_tokens, 15)
        self.assertEqual(self.a.planner_window_tokens, 0)

    def test_tally_skips_line_with_deleted_file_between_glob_and_read(self):
        self._write("a.jsonl", [_assistant(self.ts_in, 10, 0, 0)])
        orig_stat = Path.stat

        def flaky_stat(path, *a, **k):
            if path.name == "a.jsonl":
                raise FileNotFoundError
            return orig_stat(path, *a, **k)

        with mock.patch.object(Path, "stat", flaky_stat):
            self.p.tally_planner()  # must not raise
        self.assertEqual(self.a.planner_window_tokens, 0)
        self.assertEqual(self.a.planner_day_tokens, 0)

    def test_tally_skips_non_string_timestamp(self):
        line = json.dumps({"type": "assistant", "timestamp": 12345, "message": {"usage": {"input_tokens": 10}}}) + "\n"
        self._write("a.jsonl", [line])
        self.p.tally_planner()  # must not raise
        self.assertEqual(self.a.planner_window_tokens, 0)
        self.assertEqual(self.a.planner_day_tokens, 0)

    def test_missing_transcripts_dir_warns_once_per_process(self):
        shutil.rmtree(self.proj_dir)
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.p.tally_planner()
            self.p.tally_planner()
        lines = [l for l in err.getvalue().splitlines() if "planner transcripts not found for A" in l]
        self.assertEqual(len(lines), 1)

    def test_planner_usage_lives_outside_pool_state_and_survives_stale_save(self):
        self._write("a.jsonl", [_assistant(self.ts_in, 10, 0, 0)])
        fresh = P.Pool()                                      # a second Pool, also loaded before the tally
        fresh.get("A").config_dir = self.a.config_dir
        fresh.tally_planner()
        self.assertEqual(fresh.get("A").planner_day_tokens, 10)
        self.assertFalse(P.PERSIST.exists())                  # tally never touches pool_state.json
        self.p.save()                                         # stale pool, loaded before the tally, saved after it
        st = json.loads(P.PERSIST.read_text())
        self.assertFalse(set(st["accounts"]["A"]) & P.PLANNER_ACCOUNT_FIELDS)
        pu = json.loads(P.PLANNER_USAGE.read_text())
        self.assertEqual(pu["A"]["day_tokens"], 10)            # not clobbered by the stale save

    def test_missing_dir_still_rolls_over(self):
        shutil.rmtree(self.proj_dir)
        self.a.planner_window_tokens = 50
        self.a.window_started = self.now - P.WINDOW_S - 10  # force a rollover
        self.p.tally_planner(now=self.now)
        self.assertEqual(self.a.planner_window_tokens, 0)      # rolled over despite the missing dir
        pu = json.loads(P.PLANNER_USAGE.read_text())
        self.assertEqual(pu["A"]["window_tokens"], 0)          # ...and the rollover was saved, not skipped

    def test_tally_writes_only_planner_usage(self):
        self._write("a.jsonl", [_assistant(self.ts_in, 10, 0, 0)])
        self.p.tally_planner()
        self.assertEqual(self.a.planner_window_tokens, 10)
        self.assertFalse(P.PERSIST.exists())                   # only planner_usage.json changed, pool_state.json untouched

    def test_tally_honours_now(self):
        self._write("a.jsonl", [_assistant(self.ts_in, 10, 0, 0)])
        with mock.patch.object(P.time, "time", side_effect=AssertionError("time.time() called despite now=")):
            self.p.tally_planner(now=self.now)
        self.assertEqual(self.a.planner_window_tokens, 10)
        self.assertEqual(self.a.planner_day_tokens, 10)

    def test_null_usage_counts_zero(self):
        line = json.dumps({"type": "assistant", "timestamp": self.ts_in, "message": {"usage": {
            "input_tokens": None, "output_tokens": 5, "cache_read_input_tokens": None}}}) + "\n"
        self._write("a.jsonl", [line])
        self.p.tally_planner()  # must not raise
        self.assertEqual(self.a.planner_window_tokens, 5)

    def test_planner_usage_has_anchors(self):
        self._write("a.jsonl", [_assistant(self.ts_in, 10, 0, 0)])
        self.p.tally_planner(now=self.now)
        entry = json.loads(P.PLANNER_USAGE.read_text())["A"]
        self.assertEqual(entry["day"], datetime.fromtimestamp(self.now, tz=P.TZ).date().isoformat())
        self.assertEqual(entry["window_started_at"],
                          datetime.fromtimestamp(self.a.window_started, tz=P.TZ).isoformat())


class ScoutLimits(unittest.TestCase):
    """Scout turns/budget/timeout cut 2026-09-18 (T-0121): 8 sonnet scouts cost $4.59 in one afternoon."""

    def test_scout_limits_are_tightened(self):
        limits = P.config()["limits"]
        self.assertEqual(limits["max_turns"]["scout"], 12)
        self.assertEqual(limits["max_budget_usd"]["scout"], 1.0)
        self.assertEqual(limits["timeout_s"]["scout"], 600)


class OrchestrateSkillCopies(unittest.TestCase):
    """Two orchestrate SKILL.md copies (Planner-visible and skills/planner) must stay byte-identical (T-0121)."""

    def test_skill_copies_are_byte_identical(self):
        a = (REPO / ".claude" / "skills" / "orchestrate" / "SKILL.md").read_bytes()
        b = (REPO / "skills" / "planner" / "orchestrate" / "SKILL.md").read_bytes()
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
