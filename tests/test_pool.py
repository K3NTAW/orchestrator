"""Account pool selection (PoolSel) and the [[executors]] routing table (Executors): bands, quota groups,
cooldowns, budgets, scored ranking."""
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_pool.py` doesn't add this dir itself
from _harness import REPO, TMP  # noqa: F401
from orchestrator import pool as P, spawn


class PoolSel(unittest.TestCase):
    def setUp(self):
        P.PERSIST.unlink(missing_ok=True); self.p = P.Pool()

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
        P.PERSIST.unlink(missing_ok=True); self.p = P.Pool()

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


if __name__ == "__main__":
    unittest.main()
