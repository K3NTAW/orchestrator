"""spawn.run_worker's review-verdict propagation, prompt template rendering / result fitting, and base-branch
selection for stacked/challenge/review tasks (review T-0026, T-0030)."""
import json, subprocess, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_spawn.py` doesn't add this dir itself
from _harness import TMP, g, scratch_repo
from orchestrator import bus, pool as P, spawn


class ReviewVerdict(unittest.TestCase):
    def test_run_worker_captures_verdict_on_review_and_reviewed_task(self):
        reviewed = bus.create_task("feat-rv", "s", ["a"], ["rv.py"], role="execute")
        review = bus.create_task("review feat-rv", "s", ["a"], ["rv.py"], role="review", inputs=[reviewed["id"]])
        (TMP / "wt" / review["id"]).mkdir(parents=True, exist_ok=True)  # short-circuits ensure_worktree's git calls

        orig_pick = P.Pool.pick
        P.Pool.pick = lambda self, role, avoid=None: self.get("A")
        self.addCleanup(lambda: setattr(P.Pool, "pick", orig_pick))

        fake_out = {"result": json.dumps({"verdict": "request_changes", "comments": []}), "usage": {}}
        orig_run_claude = spawn.run_claude
        spawn.run_claude = lambda *a, **k: {"status": "done", "output": fake_out}
        self.addCleanup(lambda: setattr(spawn, "run_claude", orig_run_claude))

        spawn.run_worker(review["id"])
        self.assertEqual(bus.get(review["id"])["review_verdict"], "request_changes")
        self.assertEqual(bus.get(reviewed["id"])["review_verdict"], "request_changes")


class Render(unittest.TestCase):
    def test_templates_fill(self):
        s = spawn.render("scout", id="T-1", title="t", spec="q", acceptance=["a"], turns="20")
        self.assertIn("T-1", s); self.assertNotIn("{{", s)
        self.assertEqual(spawn.extract_json('here: {"summary":"x"} bye')["summary"], "x")
        self.assertTrue(spawn.extract_json("no json")["summary"])

    def test_fit_result_shrinks_oversize(self):
        big = {"summary": "s" * 3000, "findings": [{"claim": "c" * 380, "confidence": 0.5} for _ in range(40)]}
        fitted = spawn.fit_result(big)
        self.assertLess(len(json.dumps(fitted)), bus.MAX_RESULT_CHARS)
        self.assertEqual(fitted["truncated"]["reason"], "over MAX_RESULT_CHARS")
        self.assertGreater(fitted["truncated"]["original_chars"], bus.MAX_RESULT_CHARS)
        self.assertGreaterEqual(len(fitted["findings"]), 1)

    def test_fit_result_leaves_small_result_unchanged(self):
        small = {"summary": "ok", "findings": [{"claim": "x", "confidence": 0.9}]}
        fitted = spawn.fit_result(small)
        self.assertEqual(fitted, small)
        self.assertNotIn("truncated", fitted)


class SpawnBase(unittest.TestCase):
    """base_for/scoped_diff (review T-0026). Builds its own scratch repo + goal/G branch (harness scratch_repo)
    instead of relying on another test file having already turned TMP into a git repo with that branch."""

    @classmethod
    def setUpClass(cls):
        scratch_repo(TMP)
        g("branch", "goal/G")   # a no-op if some other test file's repo setup already created it

    def g(self, *a, cwd=TMP, **k):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, **k)

    def test_review_bases_on_reviewed_task_branch(self):
        reviewed = bus.create_task("feat3", "s", ["a"], ["feat3.py"], role="execute")
        wt = spawn.ensure_worktree(reviewed["id"], base="HEAD")
        bus.update(reviewed["id"], worktree=str(wt))
        review = bus.create_task("review feat3", "s", ["a"], ["feat3.py"], role="review", inputs=[reviewed["id"]])
        self.assertEqual(spawn.base_for(review), f"task/{reviewed['id']}")

    def test_review_falls_back_to_goal_branch_then_origin_main(self):
        no_wt = bus.create_task("no wt yet", "s", ["a"], ["x.py"], role="execute", parent="G")
        review = bus.create_task("review no wt", "s", ["a"], ["x.py"], role="review", inputs=[no_wt["id"]])
        self.assertEqual(spawn.base_for(review), "goal/G")            # task/<id> doesn't exist, parent's goal does
        no_parent = bus.create_task("no wt no goal", "s", ["a"], ["x.py"], role="execute")
        review2 = bus.create_task("review no parent", "s", ["a"], ["x.py"], role="review", inputs=[no_parent["id"]])
        self.assertEqual(spawn.base_for(review2), "origin/main")      # neither branch exists

    def test_execute_stacks_on_existing_goal_branch(self):
        t = bus.create_task("stack", "s", ["a"], ["more.py"], role="execute", parent="G")
        self.assertEqual(spawn.base_for(t), "goal/G")
        t2 = bus.create_task("no goal yet", "s", ["a"], ["more.py"], role="execute", parent="ghost")
        self.assertEqual(spawn.base_for(t2), "origin/main")

    def test_challenge_bases_on_goal_branch(self):
        challenge = bus.create_task("challenge x", "s", ["a"], ["x.py"], role="challenge", parent="G",
                                     inputs=[{"claim": "c", "evidence": "e", "confidence": 0.5}])
        self.assertEqual(spawn.base_for(challenge), "goal/G")
        challenge2 = bus.create_task("challenge y", "s", ["a"], ["y.py"], role="challenge", parent="ghost",
                                      inputs=[{"claim": "c", "evidence": "e", "confidence": 0.5}])
        self.assertEqual(spawn.base_for(challenge2), "origin/main")

    def test_ensure_worktree_resolves_base_when_none_given(self):
        t = bus.create_task("stacked-exec", "s", ["a"], ["stacked.py"], role="execute", parent="G")
        wt = spawn.ensure_worktree(t["id"])
        self.assertEqual(self.g("merge-base", "--is-ancestor", "goal/G", f"task/{t['id']}").returncode, 0)

    def test_scoped_diff_excludes_predecessor_hunks(self):
        wt_goal = TMP / "wt" / "_goal_seed"
        self.g("worktree", "add", str(wt_goal), "goal/G")
        (wt_goal / "shared.py").write_text("A = 1\n")
        self.g("add", "-A", cwd=wt_goal); self.g("commit", "-qm", "predecessor shared.py", cwd=wt_goal)
        self.g("worktree", "remove", str(wt_goal), "--force")

        t = bus.create_task("stack2", "s", ["a"], ["shared.py"], role="execute", parent="G")
        wt = spawn.ensure_worktree(t["id"]); bus.update(t["id"], worktree=str(wt))
        (wt / "shared.py").write_text("A = 1\nB = 1\n")
        self.g("add", "-A", cwd=wt); self.g("commit", "-qm", "stack2 add B", cwd=wt)

        review = bus.create_task("review stack2", "s", ["a"], ["shared.py"], role="review", inputs=[t["id"]])
        diff = spawn.scoped_diff(review)
        self.assertIn("+B = 1", diff)
        self.assertNotIn("+A = 1", diff)                              # predecessor's hunk, already in goal/G


if __name__ == "__main__":
    unittest.main()
