import _harness
import fcntl, inspect, json, os, shutil, subprocess, tempfile, threading, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _harness import g, scratch_repo
from orchestrator import bus, daemon, merge, notify, planner_runs, ship

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, tempfile
st_path, origin = os.environ["FAKE_GH_STATE"], os.environ["FAKE_GH_ORIGIN"]
st = json.load(open(st_path)) if os.path.exists(st_path) else {"prs": [], "calls": []}
argv = sys.argv[1:]
st["calls"].append(argv)
def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default
def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
def head_sha(branch):
    return git("rev-parse", "refs/heads/" + branch, cwd=origin).stdout.strip() or None
def mc(p):
    return {"oid": p["merge"]} if p.get("merge") else None
out, code = "", 0
if argv[:2] == ["pr", "list"]:
    out = json.dumps([{"number": p["number"], "state": p["state"], "url": p["url"], "mergeCommit": mc(p)}
                      for p in st["prs"] if p["head"] == opt("--head") and p["base"] == opt("--base")])
elif argv[:2] == ["pr", "create"]:
    n = len(st["prs"]) + 1
    p = {"number": n, "state": "OPEN", "url": "https://example.test/pull/%d" % n, "head": opt("--head"),
         "base": opt("--base"), "title": opt("--title"), "body": opt("--body")}
    st["prs"].append(p)
    out = p["url"]
elif argv[:2] == ["pr", "view"]:
    p = st["prs"][int(argv[2]) - 1]
    out = json.dumps({"state": p["state"], "mergeCommit": mc(p), "headRefOid": head_sha(p["head"])})
elif argv[:2] == ["pr", "merge"]:
    p = st["prs"][int(argv[2]) - 1]
    if opt("--match-head-commit") != head_sha(p["head"]):
        code = 1; sys.stderr.write("head moved")
    else:
        d = tempfile.mkdtemp()
        git("clone", "-q", origin, d)
        ident = ["-c", "user.email=t@t", "-c", "user.name=t"]
        if st.get("race"):
            open(os.path.join(d, "BREAK"), "w").write("x")
            git("add", "BREAK", cwd=d); git(*ident, "commit", "-qm", "race", cwd=d)
            st["race"] = False
        r = git(*ident, "merge", "--no-ff", "-m", opt("--subject", "merge"), "origin/" + p["head"], cwd=d)
        if r.returncode:
            code = 1; sys.stderr.write(r.stdout + r.stderr)
        else:
            git("push", "-q", "origin", "HEAD:" + p["base"], cwd=d)
            p["state"], p["merge"] = "MERGED", git("rev-parse", "HEAD", cwd=d).stdout.strip()
json.dump(st, open(st_path, "w"))
print(out)
sys.exit(code)
'''

GATE = r'''#!/bin/bash
wt="$1"; cd "$wt" || exit 2
[ -n "$GATE_LOG" ] && echo "full=${LUNA_GATE_FULL:-}" >> "$GATE_LOG"
if [ -n "$MOVE_TARGET" ] && { [ -z "$MOVE_ONCE" ] || [ ! -f "$MOVE_ONCE" ]; }; then
  [ -n "$MOVE_ONCE" ] && touch "$MOVE_ONCE"
  t=$(mktemp -d)
  git clone -q "$MOVE_TARGET" "$t" && (cd "$t" && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m move && git push -q origin HEAD:main)
fi
[ -f RED ] && { echo "RED failure tail" >&2; exit 1; }
[ -f BREAK ] && [ -f goal.txt ] && { echo "post-merge red" >&2; exit 1; }
exit 0
'''


class Ship(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ship-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.origin = self.dir / "origin.git"
        g("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.dir)
        self.root = scratch_repo(self.dir / "root")
        g("remote", "add", "origin", str(self.origin), cwd=self.root)
        g("push", "-q", "origin", "main", cwd=self.root)
        bindir = self.dir / "bin"; bindir.mkdir()
        (bindir / "gh").write_text(FAKE_GH); (bindir / "gh").chmod(0o755)
        gate_script = self.dir / "gate.sh"; gate_script.write_text(GATE); gate_script.chmod(0o755)
        self.gh_state = self.dir / "gh.json"
        self.gate_log = self.dir / "gate.log"
        env = mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}",
                                           "FAKE_GH_STATE": str(self.gh_state), "FAKE_GH_ORIGIN": str(self.origin)})
        env.start(); self.addCleanup(env.stop)
        for p in (mock.patch.object(merge, "TESTS_GREEN", gate_script),
                  mock.patch.object(notify, "notify", side_effect=lambda m: self.notes.append(m)),
                  mock.patch.object(ship, "_record_decision", side_effect=lambda *a: self.decisions.append(a))):
            p.start(); self.addCleanup(p.stop)
        self.notes, self.decisions, self.goals = [], [], []
        self.cfg = {"ship": {"enabled": True, "target": "main", "max_regates": 3,
                             "gate_env": {"GATE_LOG": str(self.gate_log)}}, "gate": {"timeout_s": 120}}
        self.pool = SimpleNamespace(cfg=self.cfg)

    # helpers
    def commit(self, cwd, name, text, msg=None):
        Path(cwd, name).write_text(text)
        g("add", name, cwd=cwd); g("commit", "-qm", msg or f"add {name}", cwd=cwd)
        return g("rev-parse", "HEAD", cwd=cwd).stdout.strip()

    def goal(self, files=(("goal.txt", "goal\n"),), closed=True):
        gid = bus.create_task("ship me", "spec", ["a"], ["s"], role="execute", constraints={"goal": True})["id"]
        self.goals.append(gid)
        g("checkout", "-q", "-b", f"goal/{gid}", cwd=self.root)
        for name, text in files:
            self.commit(self.root, name, text)
        g("push", "-q", "origin", f"goal/{gid}", cwd=self.root)
        g("checkout", "-q", "main", cwd=self.root)
        if closed:
            bus.post_result(gid, {"goal_closed": True, "pr_url": None})
        return gid

    def push_main(self, name, text):
        clone = self.dir / f"c{len(os.listdir(self.dir))}"
        g("clone", "-q", str(self.origin), str(clone), cwd=self.dir)
        g("config", "user.email", "t@t", cwd=clone); g("config", "user.name", "t", cwd=clone)
        sha = self.commit(clone, name, text)
        g("push", "-q", "origin", "HEAD:main", cwd=clone)
        return sha

    def gh(self):
        return json.loads(self.gh_state.read_text()) if self.gh_state.exists() else {"prs": [], "calls": []}

    def calls(self, *prefix):
        return [c for c in self.gh()["calls"] if c[:len(prefix)] == list(prefix)]

    def state(self, gid):
        return (bus.get(gid).get("pipeline") or {}).get("ship") or {}

    def advance(self, gid):
        return ship.advance(gid, self.pool, root=self.root)

    def origin_sha(self, ref):
        return g("rev-parse", ref, cwd=self.origin).stdout.strip()

    def mine(self):
        real = ship.candidates
        return mock.patch.object(ship, "candidates", lambda: [t for t in real() if t["id"] in self.goals])

    def gate_runs(self):
        return self.gate_log.read_text().splitlines() if self.gate_log.exists() else []

    # tests
    def test_ship_disabled_by_default(self):
        self.assertFalse(ship.settings({})["enabled"])
        self.assertFalse(ship.settings({"ship": {}})["enabled"])
        self.goal()
        with self.mine():
            self.assertIsNone(ship.tick(SimpleNamespace(cfg={"gate": {}}), root=self.root))
        self.assertEqual(self.gh()["calls"], [])

    def test_ship_runs_after_routine_close_only(self):
        gid = self.goal(closed=False)
        self.assertNotIn(gid, [t["id"] for t in ship.candidates()])
        with self.mine():
            self.assertIsNone(ship.tick(self.pool, root=self.root))
        self.assertEqual(self.gh()["calls"], [])
        ctx = {"gate_state": "green", "all_children_merged": True, "routes": {}}
        planner_runs.routine_close({"goal_id": gid}, ctx, SimpleNamespace(cfg={"daemon": {}}))
        self.assertTrue(bus.get(gid)["result"]["goal_closed"])
        self.assertIn(gid, [t["id"] for t in ship.candidates()])
        with self.mine():
            result = ship.tick(self.pool, root=self.root)
        self.assertEqual(result["status"], "shipped")

    def test_ship_gate_runs_in_background_not_in_tick(self):
        gid = self.goal()
        entered, release, seen = threading.Event(), threading.Event(), []
        real_gate = ship._gate

        def slow_gate(*a):
            seen.append(threading.current_thread())
            entered.set()
            release.wait(30)
            return real_gate(*a)

        with self.mine(), mock.patch.object(ship, "_gate", side_effect=slow_gate):
            started = ship.tick(self.pool, stop_event=threading.Event(), root=self.root, inline=False)
            self.assertEqual(started["status"], "started")
            self.assertFalse(started["thread"].daemon)
            self.assertTrue(entered.wait(30))
            self.assertIsNot(seen[0], threading.current_thread())
            self.assertEqual(self.state(gid)["state"], "gating")
            release.set()
            ship.join_threads(60)
        self.assertEqual(self.state(gid)["state"], "shipped")

    def _crash_after_merge(self, gid):
        with mock.patch.object(ship, "_on_merged", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.advance(gid)
        self.assertEqual(self.state(gid)["state"], "merging")
        self.assertEqual(self.gh()["prs"][0]["state"], "MERGED")
        return len(self.gh()["calls"])

    def test_ship_resumes_after_crash_when_pr_already_merged(self):
        gid = self.goal()
        self._crash_after_merge(gid)
        result = self.advance(gid)
        self.assertEqual(result["status"], "shipped")
        st = self.state(gid)
        self.assertEqual(st["merge_sha"], self.gh()["prs"][0]["merge"])
        self.assertEqual(len(self.calls("pr", "merge")), 1)
        self.advance(gid)
        self.assertEqual(len([n for n in self.notes if "shipped to main" in n]), 1)

    def test_resume_reads_pr_truth_after_crash(self):
        gid = self.goal()
        n = self._crash_after_merge(gid)
        gates = len(self.gate_runs())
        self.advance(gid)
        after = self.gh()["calls"][n:]
        self.assertEqual(after[0][:2], ["pr", "view"])
        self.assertFalse([c for c in after if c[:2] in (["pr", "merge"], ["pr", "create"])])
        self.assertEqual(len(self.gate_runs()), gates)

    def test_ship_regates_when_target_moved(self):
        gid = self.goal()
        self.cfg["ship"]["gate_env"].update(MOVE_TARGET=str(self.origin), MOVE_ONCE=str(self.dir / "moved"))
        self.assertEqual(self.advance(gid)["status"], "shipped")
        self.assertEqual(self.state(gid)["attempts"], 1)
        self.assertEqual(len(self.gate_runs()), 2)
        self.assertEqual(self.state(gid)["gated_target_sha"], self.origin_sha("main^1"))

    def test_ship_holds_after_max_regates(self):
        gid = self.goal()
        self.cfg["ship"]["max_regates"] = 2
        self.cfg["ship"]["gate_env"]["MOVE_TARGET"] = str(self.origin)
        result = self.advance(gid)
        self.assertEqual(result, {"status": "held", "reason": "ship target kept moving"})
        self.assertEqual(len(self.gate_runs()), 3)
        self.assertEqual(self.calls("pr", "merge"), [])

    def test_ship_holds_on_conflict(self):
        gid = self.goal()
        self.push_main("goal.txt", "main side\n")
        self.assertEqual(self.advance(gid)["reason"], "ship conflict")
        self.assertEqual(self.state(gid)["state"], "held")
        self.assertEqual(self.gate_runs(), [])
        self.assertEqual(self.calls("pr", "merge"), [])

    def test_ship_holds_on_red_gate_and_notifies_once(self):
        gid = self.goal(files=(("goal.txt", "g\n"), ("RED", "x\n")))
        self.assertEqual(self.advance(gid)["reason"], "gate red")
        st = self.state(gid)
        self.assertEqual(st["state"], "held")
        self.assertIn("RED failure tail", st["failure_tail"])
        self.advance(gid)
        self.assertEqual(len([n for n in self.notes if "ship held: gate red" in n]), 1)
        self.assertNotIn(gid, [t["id"] for t in ship.candidates()])
        self.assertEqual(self.calls("pr", "merge"), [])

    def test_ship_retry_releases_hold(self):
        gid = self.goal(files=(("goal.txt", "g\n"), ("RED", "x\n")))
        self.advance(gid)
        self.assertEqual(ship.retry(gid)["status"], "released")
        st = self.state(gid)
        self.assertEqual((st["state"], st["retries"], st["last_error"]), ("pending", 1, None))
        self.assertIn(gid, [t["id"] for t in ship.candidates()])
        self.assertEqual(ship.retry(gid)["status"], "not_held")

    def test_hold_after_retry_notifies_again(self):
        gid = self.goal(files=(("goal.txt", "g\n"), ("RED", "x\n")))
        self.advance(gid)
        ship.retry(gid)
        self.advance(gid)
        self.assertEqual(len([n for n in self.notes if "ship held: gate red" in n]), 2)
        self.assertEqual(set(self.state(gid)["notified"]), {"held:gate red:0", "held:gate red:1"})

    def test_ship_merge_uses_match_head_commit(self):
        gid = self.goal()
        self.assertEqual(self.advance(gid)["status"], "shipped")
        (call,) = self.calls("pr", "merge")
        st = self.state(gid)
        self.assertEqual(call[call.index("--match-head-commit") + 1], st["gated_head_sha"])
        self.assertEqual(st["gated_head_sha"], self.origin_sha(f"goal/{gid}"))
        self.assertIn("--merge", call)
        self.assertEqual(call[call.index("--subject") + 1],
                         f"Merge PR 1: ship me (goal/{gid}, auto-merged after green full gate)")
        self.assertEqual(st["merge_sha"], self.origin_sha("main"))
        self.assertIn(f"rollback: orchestrator rollback {st['merge_sha']}", self.notes[-1])
        self.assertIn(f"orchestrator rollback {st['merge_sha']}", self.decisions[-1][1])

    def test_detached_worktree_and_ff_push(self):
        gid = self.goal()
        self.push_main("other.txt", "o\n")
        local = g("rev-parse", f"goal/{gid}", cwd=self.root).stdout.strip()
        self.assertEqual(self.advance(gid)["status"], "shipped")
        self.assertEqual(g("rev-parse", f"goal/{gid}", cwd=self.root).stdout.strip(), local)
        head = self.state(gid)["gated_head_sha"]
        self.assertEqual(g("merge-base", "--is-ancestor", local, head, cwd=self.origin).returncode, 0)
        self.assertEqual(len(g("worktree", "list", cwd=self.root).stdout.splitlines()), 1)
        self.assertEqual(list((self.root / ".orchestrator" / "ship-wt").iterdir()), [])
        # A diverged remote goal branch is never force-pushed over.
        gid2 = self.goal(files=(("g2.txt", "a\n"),))
        clone = self.dir / "diverge"
        g("clone", "-q", "-b", f"goal/{gid2}", str(self.origin), str(clone), cwd=self.dir)
        g("config", "user.email", "t@t", cwd=clone); g("config", "user.name", "t", cwd=clone)
        g("reset", "-q", "--hard", "HEAD~1", cwd=clone)
        self.commit(clone, "x.txt", "x\n")
        g("push", "-q", "origin", f":goal/{gid2}", cwd=clone)
        g("push", "-q", "origin", f"HEAD:refs/heads/goal/{gid2}", cwd=clone)
        remote = self.origin_sha(f"goal/{gid2}")
        self.assertEqual(self.advance(gid2)["reason"], "ship push not fast-forward")
        self.assertEqual(self.origin_sha(f"goal/{gid2}"), remote)

    def test_gate_env_full_passed(self):
        gid = self.goal()
        self.cfg["ship"]["gate_env"] = {"LUNA_GATE_FULL": "1"}
        seen = []
        real = ship.gate.run_gate
        with mock.patch.object(ship.gate, "run_gate", side_effect=lambda *a, **k: seen.append(k) or real(*a, **k)):
            self.advance(gid)
        self.assertEqual(seen[0]["env"]["LUNA_GATE_FULL"], "1")
        self.assertEqual(seen[0]["env"]["PATH"], os.environ["PATH"])
        self.assertIs(seen[0]["cfg"], self.cfg)

    def test_gate_env_from_config(self):
        gid = self.goal()
        self.cfg["ship"]["gate_env"]["LUNA_GATE_FULL"] = "1"
        self.advance(gid)
        self.assertEqual(self.gate_runs(), ["full=1"])
        self.cfg["ship"]["gate_env"].pop("LUNA_GATE_FULL")
        gid2 = self.goal(files=(("g2.txt", "a\n"),))
        self.advance(gid2)
        self.assertEqual(self.gate_runs()[-1], "full=")

    def test_closed_pr_ignored_merged_pr_counts_as_shipped(self):
        gid = self.goal()
        gid2 = self.goal(files=(("g2.txt", "a\n"),))
        main_sha = self.origin_sha("main")
        self.gh_state.write_text(json.dumps({"calls": [], "prs": [
            {"number": 1, "state": "CLOSED", "url": "https://example.test/pull/1", "head": f"goal/{gid}", "base": "main"},
            {"number": 2, "state": "MERGED", "url": "https://example.test/pull/2", "head": f"goal/{gid2}",
             "base": "main", "merge": main_sha}]}))
        self.assertEqual(self.advance(gid)["status"], "shipped")
        self.assertEqual(self.state(gid)["pr_number"], 3)
        result = self.advance(gid2)
        self.assertEqual(result, {"status": "shipped", "merge_sha": main_sha})
        self.assertEqual(len(self.calls("pr", "merge")), 1)
        self.assertEqual(len(self.calls("pr", "create")), 1)

    def test_ship_update_only_touches_ship_key(self):
        gid = self.goal()
        bus.update(gid, pipeline={"closed_at": 1.0, "other": "x", "ship": {"state": "pending", "attempts": 2}})
        ship._ship_update(gid, state="gating")
        pipeline = bus.get(gid)["pipeline"]
        self.assertEqual({k: v for k, v in pipeline.items() if k != "ship"}, {"closed_at": 1.0, "other": "x"})
        self.assertEqual((pipeline["ship"]["state"], pipeline["ship"]["attempts"]), ("gating", 2))
        self.assertIn("updated_at", pipeline["ship"])
        writes = [line for line in inspect.getsource(ship).splitlines() if "bus.update(" in line]
        self.assertEqual(len(writes), 1)

    def test_once_mode_runs_inline(self):
        gid = self.goal()
        with self.mine():
            result = ship.tick(self.pool, root=self.root, inline=True)
        self.assertEqual(result["status"], "shipped")
        self.assertEqual(self.state(gid)["state"], "shipped")
        self.assertIn("inline=stop_event is None", inspect.getsource(daemon.tick))

    def test_second_ship_waits_for_lock(self):
        self.goal()
        path = self.root / ".orchestrator" / "ship.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.mine():
                self.assertIsNone(ship.tick(self.pool, root=self.root))
            self.assertEqual(self.gh()["calls"], [])
            fcntl.flock(held, fcntl.LOCK_UN)
        with self.mine():
            self.assertEqual(ship.tick(self.pool, root=self.root)["status"], "shipped")

    def test_post_merge_gate_on_base_race_rolls_back(self):
        gid = self.goal()
        self.gh_state.write_text(json.dumps({"calls": [], "prs": [], "race": True}))
        result = self.advance(gid)
        self.assertEqual(result, {"status": "held", "reason": "post-merge gate red"})
        st = self.state(gid)
        self.assertEqual(st["post_merge_gate"], "red")
        self.assertEqual(st["rollback"]["status"], "rolled_back")
        self.assertNotEqual(g("rev-parse", f"{st['merge_sha']}^1", cwd=self.origin).stdout.strip(),
                            st["gated_target_sha"])
        self.assertNotEqual(g("cat-file", "-e", "main:goal.txt", cwd=self.origin).returncode, 0)
        self.assertTrue(any("post-merge gate red" in n for n in self.notes))

    # rollback
    def shipped(self):
        gid = self.goal()
        self.assertEqual(self.advance(gid)["status"], "shipped")
        return gid, self.state(gid)["merge_sha"]

    def test_rollback_reverts_merge_commit(self):
        gid, sha = self.shipped()
        result = ship.rollback(sha, root=self.root, cfg=self.cfg)
        self.assertEqual(result["status"], "rolled_back")
        self.assertNotEqual(g("cat-file", "-e", "main:goal.txt", cwd=self.origin).returncode, 0)
        pr = self.gh()["prs"][-1]
        self.assertTrue(pr["title"].startswith("Revert Merge PR 1:"))
        self.assertEqual((pr["head"], pr["state"]), (f"rollback/{sha[:12]}", "MERGED"))
        self.assertIn("--merge", self.calls("pr", "merge")[-1])
        self.assertEqual(result["revert_sha"], self.origin_sha("main"))

    def test_rollback_refuses_non_merge(self):
        sha = self.origin_sha("main")
        result = ship.rollback(sha, root=self.root, cfg=self.cfg)
        self.assertEqual(result["status"], "refused")
        self.assertIn("not a merge commit", result["reason"])
        self.assertEqual(self.gh()["calls"], [])

    def test_rollback_refuses_non_first_parent(self):
        gid, sha = self.shipped()
        side = g("rev-parse", f"{sha}^2", cwd=self.origin).stdout.strip()
        result = ship.rollback(side, root=self.root, cfg=self.cfg)
        self.assertEqual(result["status"], "refused")
        self.assertIn("first-parent", result["reason"])
        self.assertEqual(len(self.calls("pr", "create")), 1)

    def test_rollback_conflict_aborts(self):
        gid, sha = self.shipped()
        self.push_main("goal.txt", "changed later\n")
        result = ship.rollback(sha, root=self.root, cfg=self.cfg)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(len(self.calls("pr", "create")), 1)
        self.assertTrue(any("revert conflict" in n for n in self.notes))
        self.assertEqual(list((self.root / ".orchestrator" / "ship-wt").iterdir()), [])

    def test_rollback_gate_red_no_merge(self):
        gid, sha = self.shipped()
        self.push_main("RED", "x\n")
        result = ship.rollback(sha, root=self.root, cfg=self.cfg)
        self.assertEqual(result["status"], "gate_red")
        self.assertEqual(len(self.calls("pr", "merge")), 1)
        self.assertTrue(any("not merged" in n for n in self.notes))


if __name__ == "__main__":
    unittest.main()
