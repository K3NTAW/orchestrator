import _harness
import fcntl, inspect, json, os, shutil, subprocess, tempfile, threading, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _harness import g, scratch_repo
from orchestrator import bus, daemon, merge, notify, planner_runs, ship

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, shutil, subprocess, sys, tempfile
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
def head_oid(p):
    return p.get("head_oid") or head_sha(p["head"])
out, code = "", 0
if argv[:2] == ["pr", "list"]:
    out = json.dumps([{"number": p["number"], "state": p["state"], "url": p["url"], "mergeCommit": mc(p),
                       "headRefOid": head_oid(p)}
                      for p in st["prs"] if p["head"] == opt("--head") and p["base"] == opt("--base")])
elif argv[:2] == ["pr", "create"]:
    n = len(st["prs"]) + 1
    p = {"number": n, "state": "OPEN", "url": "https://example.test/pull/%d" % n, "head": opt("--head"),
         "base": opt("--base"), "title": opt("--title"), "body": opt("--body")}
    st["prs"].append(p)
    out = p["url"]
elif argv[:2] == ["pr", "view"]:
    p = st["prs"][int(argv[2]) - 1]
    out = json.dumps({"state": p["state"], "mergeCommit": mc(p), "headRefOid": head_oid(p)})
elif argv[:2] == ["pr", "merge"]:
    p = st["prs"][int(argv[2]) - 1]
    if opt("--match-head-commit") != head_sha(p["head"]):
        code = 1; sys.stderr.write("head moved")
    elif st.get("pending"):
        out = "queued"
    else:
        d = tempfile.mkdtemp(dir=os.environ["SHIP_TEST_TMP"])
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
            p["head_oid"] = head_sha(p["head"])
        shutil.rmtree(d, True)
json.dump(st, open(st_path, "w"))
print(out)
sys.exit(code)
'''

GATE = r'''#!/bin/bash
wt="$1"; cd "$wt" || exit 2
[ -n "$GATE_LOG" ] && echo "full=${LUNA_GATE_FULL:-}" >> "$GATE_LOG"
if [ -n "$MOVE_TARGET" ] && { [ -z "$MOVE_ONCE" ] || [ ! -f "$MOVE_ONCE" ]; }; then
  [ -n "$MOVE_ONCE" ] && touch "$MOVE_ONCE"
  t=$(mktemp -d "$SHIP_TEST_TMP/move.XXXXXX")
  git clone -q "$MOVE_TARGET" "$t" && (cd "$t" && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m move && git push -q origin HEAD:main)
  python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], True)' "$t"
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
        self.tmp = self.dir / "tmp"; self.tmp.mkdir()
        env = mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}", "SHIP_TEST_TMP": str(self.tmp),
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

    def test_closed_pr_holds_instead_of_reopening(self):
        gid = self.goal()
        gid2 = self.goal(files=(("g2.txt", "a\n"),))
        main_sha = self.origin_sha("main")
        self.gh_state.write_text(json.dumps({"calls": [], "prs": [
            {"number": 1, "state": "CLOSED", "url": "https://example.test/pull/1", "head": f"goal/{gid}", "base": "main"},
            {"number": 2, "state": "MERGED", "url": "https://example.test/pull/2", "head": f"goal/{gid2}",
             "base": "main", "merge": main_sha}]}))
        self.assertEqual(self.advance(gid), {"status": "held", "reason": "ship PR closed"})
        self.advance(gid)
        self.assertEqual(len([n for n in self.notes if "ship held: ship PR closed" in n]), 1)
        self.assertEqual(self.calls("pr", "create"), [])
        self.assertEqual(self.gate_runs(), [])
        # A human-merged PR whose head was never gated here gets a post-merge gate before it counts.
        result = self.advance(gid2)
        self.assertEqual(result, {"status": "shipped", "merge_sha": main_sha})
        self.assertEqual(len(self.gate_runs()), 1)
        self.assertEqual(self.calls("pr", "merge"), [])
        self.assertEqual(self.calls("pr", "create"), [])
        # Released by retry, a goal whose PR is still closed holds again instead of opening a new PR.
        ship.retry(gid)
        self.assertEqual(self.advance(gid)["reason"], "ship PR closed")
        self.assertEqual(self.calls("pr", "create"), [])

    def test_push_only_after_green_gate(self):
        events, real_git, real_gate = [], ship._git, ship._gate

        def git(root, *args, check=False):
            if args[:1] == ("push",):
                events.append("push")
            return real_git(root, *args, check=check)

        def gate(*a):
            events.append("gate")
            return real_gate(*a)

        with mock.patch.object(ship, "_git", side_effect=git), mock.patch.object(ship, "_gate", side_effect=gate):
            gid = self.goal(files=(("goal.txt", "g\n"), ("RED", "x\n")))
            before = self.origin_sha(f"goal/{gid}")
            self.assertEqual(self.advance(gid)["reason"], "gate red")
            self.assertEqual(events, ["gate"])
            self.assertEqual(self.origin_sha(f"goal/{gid}"), before)
            self.assertEqual(self.calls("pr", "create"), [])
            self.assertNotIn("gated_head_sha", self.state(gid))
            self.assertFalse(self.state(gid)["gate_ok"])
            events.clear()
            gid2 = self.goal(files=(("g2.txt", "a\n"),))
            self.push_main("other.txt", "o\n")
            self.assertEqual(self.advance(gid2)["status"], "shipped")
        self.assertEqual(events[:2], ["gate", "push"])
        st = self.state(gid2)
        self.assertTrue(st["gate_ok"])
        self.assertEqual(st["gated_head_sha"], self.gh()["prs"][0]["head_oid"])

    def test_retry_after_red_gate_never_marks_shipped(self):
        gid = self.goal(files=(("goal.txt", "g\n"), ("RED", "x\n")))
        self.assertEqual(self.advance(gid)["reason"], "gate red")
        self.assertEqual(ship.retry(gid)["status"], "released")
        # A human merges the never-gated branch by hand and the PR shows as MERGED.
        clone = self.dir / "manual"
        g("clone", "-q", str(self.origin), str(clone), cwd=self.dir)
        g("config", "user.email", "t@t", cwd=clone); g("config", "user.name", "t", cwd=clone)
        g("merge", "--no-ff", "-m", "manual", f"origin/goal/{gid}", cwd=clone)
        g("push", "-q", "origin", "HEAD:main", cwd=clone)
        merge_sha = self.origin_sha("main")
        self.gh_state.write_text(json.dumps({"calls": [], "prs": [
            {"number": 1, "state": "MERGED", "url": "https://example.test/pull/1", "head": f"goal/{gid}",
             "base": "main", "merge": merge_sha}]}))
        result = self.advance(gid)
        self.assertEqual(result, {"status": "held", "reason": "post-merge gate red"})
        st = self.state(gid)
        self.assertEqual((st["state"], st["post_merge_gate"]), ("held", "red"))
        self.assertFalse([n for n in self.notes if "shipped to main" in n])
        self.assertEqual(ship.retry(gid)["status"], "refused")

    def test_fetch_failure_counts_as_error(self):
        gid = self.goal()
        real = ship._git

        def offline(root, *args, check=False):
            if args[:1] == ("fetch",):
                return subprocess.CompletedProcess(args, 1, "", "network down")
            return real(root, *args, check=check)

        with mock.patch.object(ship, "_git", side_effect=offline):
            self.assertEqual(self.advance(gid)["status"], "error")
            self.assertEqual(self.state(gid)["state"], "gating")
            self.assertEqual(self.advance(gid)["status"], "error")
            self.assertEqual(self.advance(gid), {"status": "held", "reason": "ship failed 3 times in a row"})
        self.assertIn("network down", self.state(gid)["failure_tail"])
        ship.retry(gid)
        self.assertEqual(self.advance(gid)["status"], "shipped")

    def test_already_in_target_ships_without_push(self):
        gid = self.goal()
        g("push", "-q", "origin", f"goal/{gid}:main", cwd=self.root)
        before = self.origin_sha(f"goal/{gid}")
        self.assertEqual(self.advance(gid), {"status": "shipped", "reason": "already in target"})
        st = self.state(gid)
        self.assertEqual((st["state"], st["shipped_reason"]), ("shipped", "already in target"))
        self.assertEqual([c for c in self.gh()["calls"] if c[:2] != ["pr", "list"]], [])
        self.assertEqual(self.gate_runs(), [])
        self.assertEqual(self.origin_sha(f"goal/{gid}"), before)
        self.assertNotIn(gid, [t["id"] for t in ship.candidates()])
        # The same check runs in gating, for a goal that reached the target after its PR was recorded.
        g("fetch", "-q", "origin", cwd=self.root)
        g("merge", "-q", "--ff-only", "origin/main", cwd=self.root)
        gid2 = self.goal(files=(("g2.txt", "a\n"),))
        ship._ship_update(gid2, state="gating", pr_number=None)
        g("push", "-q", "origin", f"goal/{gid2}:main", cwd=self.root)
        with mock.patch.object(ship, "_find_pr", return_value=None):
            self.assertEqual(ship._gating(gid2, self.root, self.cfg, "main")["reason"], "already in target")
        self.assertEqual(self.state(gid2)["state"], "shipped")

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

    def test_once_skips_ship(self):
        with mock.patch.object(daemon.ship, "tick") as tick:
            self.assertIsNone(daemon.ship_tick(self.pool, None))
            tick.assert_not_called()
            stop = threading.Event()
            daemon.ship_tick(self.pool, stop)
            tick.assert_called_once_with(self.pool, stop_event=stop, inline=False)
            stop.set()
            daemon.ship_tick(self.pool, stop)
            self.assertEqual(tick.call_count, 1)
        self.assertIn("ship_tick(pool, stop_event)", inspect.getsource(daemon.tick))

    def test_no_ship_thread_after_main_exit(self):
        self.goal()
        dead = mock.Mock(is_alive=mock.Mock(return_value=False))
        with self.mine(), mock.patch.object(ship.threading, "main_thread", return_value=dead), \
                mock.patch.object(ship.threading, "Thread") as thread:
            self.assertIsNone(ship.tick(self.pool, stop_event=threading.Event(), root=self.root, inline=False))
        thread.assert_not_called()
        self.assertEqual(self.gh()["calls"], [])
        from orchestrator import mcp
        src = inspect.getsource(mcp.main)
        self.assertIn("thread.stop_event.set()", src)
        self.assertLess(src.index("srv.run()"), src.index("thread.stop_event.set()"))

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

    # review fix round (T-1683)
    def test_h1_gates_and_merges_the_target_sha_read_once(self):
        gid = self.goal()
        before = self.origin_sha("main")
        real, moved = ship._git, []

        def racing_git(root, *args, check=False):
            if args[:1] == ("push",) and "refs/heads/goal/" in args[-1] and not moved:
                # The daemon thread fetches the same repo between the merge and the push.
                moved.append(self.push_main("race.txt", "r\n"))
                real(self.root, "fetch", "origin")
            return real(root, *args, check=check)

        seen = []
        real_merging = ship._merging
        with mock.patch.object(ship, "_git", side_effect=racing_git), \
                mock.patch.object(ship, "_merging", side_effect=lambda *a: seen.append(dict(self.state(a[0])))
                                  or real_merging(*a)):
            self.assertEqual(self.advance(gid)["status"], "shipped")
        self.assertEqual(seen[0]["gated_target_sha"], before)
        st = self.state(gid)
        self.assertEqual((st["attempts"], st["gated_target_sha"]), (1, moved[0]))
        self.assertEqual(g("merge-base", "--is-ancestor", st["gated_target_sha"], st["gated_head_sha"],
                           cwd=self.origin).returncode, 0)
        # _merging refuses a gated head that does not contain the gated target.
        gid2 = self.goal(files=(("g2.txt", "a\n"),))
        head = g("rev-parse", f"goal/{gid2}", cwd=self.root).stdout.strip()
        ship._ship_update(gid2, state="merging", pr_number=9, gated_head_sha=head,
                          gated_target_sha=self.origin_sha("main"))
        with self.assertRaises(ship._Hold) as h:
            ship._merging(gid2, self.root, self.cfg, "main")
        self.assertEqual(h.exception.reason, "ship gated head does not contain gated target")

    def test_h2_red_post_merge_gate_never_ships(self):
        gid = self.goal()
        self.gh_state.write_text(json.dumps({"calls": [], "prs": [], "race": True}))
        with mock.patch.object(ship, "rollback", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.advance(gid)
        st = self.state(gid)
        self.assertEqual((st["post_merge_gate"], st["rollback"]["status"], st["state"]), ("red", "running", "merging"))
        result = self.advance(gid)
        self.assertEqual(result, {"status": "held", "reason": "post-merge gate red"})
        st = self.state(gid)
        self.assertEqual(st["rollback"]["status"], "rolled_back")
        self.assertNotEqual(g("cat-file", "-e", "main:goal.txt", cwd=self.origin).returncode, 0)
        self.assertEqual(ship.retry(gid)["status"], "refused")
        self.assertFalse([n for n in self.notes if "shipped to main" in n])
        # A rollback that failed holds and is not run again on resume.
        gid2 = self.goal(files=(("g2.txt", "a\n"),))
        ship._ship_update(gid2, state="merging", pr_number=1, merge_sha=st["merge_sha"], post_merge_gate="red",
                          rollback={"status": "failed", "reason": "push failed"})
        with mock.patch.object(ship, "rollback") as rb:
            result = self.advance(gid2)
        rb.assert_not_called()
        self.assertEqual(result, {"status": "held", "reason": "post-merge gate red, rollback failed"})
        self.assertEqual(self.state(gid2)["state"], "held")

    def test_resume_detects_merged_rollback(self):
        gid = self.goal()
        self.gh_state.write_text(json.dumps({"calls": [], "prs": [], "race": True}))
        with mock.patch.object(ship, "rollback", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.advance(gid)
        merge_sha = self.state(gid)["merge_sha"]
        # The interrupted rollback had already got its revert PR merged.
        st = self.gh()
        st["prs"].append({"number": len(st["prs"]) + 1, "state": "MERGED", "url": "https://example.test/pull/9",
                          "head": f"rollback/{merge_sha[:12]}", "base": "main", "merge": "f" * 40,
                          "head_oid": "e" * 40})
        self.gh_state.write_text(json.dumps(st))
        with mock.patch.object(ship, "rollback") as rb:
            result = self.advance(gid)
        rb.assert_not_called()
        self.assertEqual(result, {"status": "held", "reason": "post-merge gate red"})
        rollback = self.state(gid)["rollback"]
        self.assertEqual((rollback["status"], rollback["revert_sha"], rollback["resumed"]),
                         ("rolled_back", "f" * 40, True))

    def test_m3_unresolved_first_parent_holds(self):
        gid = self.goal()
        self._crash_after_merge(gid)
        real = ship._git

        def offline(root, *args, check=False):
            if args[:1] == ("fetch",):
                return subprocess.CompletedProcess(args, 1, "", "offline")
            return real(root, *args, check=check)

        with mock.patch.object(ship, "_git", side_effect=offline):
            result = self.advance(gid)
        self.assertEqual(result, {"status": "held", "reason": "ship merge parent unresolved"})
        self.assertIn("offline", self.state(gid)["failure_tail"])
        self.assertFalse([n for n in self.notes if "shipped to main" in n])

    def test_m4_revert_regated_when_target_moves(self):
        gid, sha = self.shipped()
        self.cfg["ship"]["gate_env"].update(MOVE_TARGET=str(self.origin), MOVE_ONCE=str(self.dir / "moved"))
        result = ship.rollback(sha, root=self.root, cfg=self.cfg)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(len(self.gate_runs()), 3)
        call = self.calls("pr", "merge")[-1]
        self.assertEqual(call[call.index("--match-head-commit") + 1], self.gh()["prs"][-1]["head_oid"])
        self.assertNotEqual(g("cat-file", "-e", "main:goal.txt", cwd=self.origin).returncode, 0)
        # Still moving after the re-gate: the PR stays open.
        gid2 = self.goal(files=(("g2.txt", "a\n"),))
        self.cfg["ship"]["gate_env"] = {"GATE_LOG": str(self.gate_log)}
        self.assertEqual(self.advance(gid2)["status"], "shipped")
        self.cfg["ship"]["gate_env"]["MOVE_TARGET"] = str(self.origin)
        result = ship.rollback(self.state(gid2)["merge_sha"], root=self.root, cfg=self.cfg)
        self.assertEqual(result["status"], "target_moved")
        self.assertEqual(self.gh()["prs"][-1]["state"], "OPEN")
        self.assertTrue(any("kept moving" in n for n in self.notes))

    def test_m5_merge_pending_holds(self):
        gid = self.goal()
        self.gh_state.write_text(json.dumps({"calls": [], "prs": [], "pending": True}))
        result = self.advance(gid)
        self.assertEqual(result, {"status": "held", "reason": "merge pending"})
        self.assertEqual(len(self.calls("pr", "merge")), 1)
        self.assertEqual(self.state(gid)["state"], "held")

    def test_l6_consecutive_errors_hold(self):
        gid = self.goal()
        with mock.patch.object(ship, "_gating", side_effect=OSError("boom")):
            self.assertEqual(self.advance(gid)["status"], "error")
            self.assertEqual(self.advance(gid)["status"], "error")
            self.assertEqual(self.advance(gid), {"status": "held", "reason": "ship failed 3 times in a row"})
        self.assertEqual(self.state(gid)["errors"], 3)
        self.assertTrue(any("ship failed 3 times in a row" in n for n in self.notes))
        ship.retry(gid)
        self.assertEqual(self.advance(gid)["status"], "shipped")
        self.assertEqual(self.state(gid)["errors"], 0)

    def test_l7_stale_merged_pr_opens_new_pr(self):
        gid = self.goal()
        old_head = g("rev-parse", f"goal/{gid}", cwd=self.root).stdout.strip()
        self.gh_state.write_text(json.dumps({"calls": [], "prs": [
            {"number": 1, "state": "MERGED", "url": "https://example.test/pull/1", "head": f"goal/{gid}",
             "base": "main", "merge": self.origin_sha("main"), "head_oid": old_head}]}))
        g("checkout", "-q", f"goal/{gid}", cwd=self.root)
        self.commit(self.root, "more.txt", "more\n")
        g("push", "-q", "origin", f"goal/{gid}", cwd=self.root)
        g("checkout", "-q", "main", cwd=self.root)
        self.assertEqual(self.advance(gid)["status"], "shipped")
        self.assertEqual(self.state(gid)["pr_number"], 2)
        self.assertEqual(g("cat-file", "-e", "main:more.txt", cwd=self.origin).returncode, 0)
        self.assertEqual(len(self.calls("pr", "create")), 1)

    def test_l8_rollback_rejects_non_sha(self):
        with mock.patch.object(ship, "_git") as git:
            for bad in ("main", "HEAD~1", "--all", "abc", "deadbeef;x", "A" * 40, None):
                self.assertEqual(ship.rollback(bad, root=self.root, cfg=self.cfg)["status"], "refused")
        git.assert_not_called()

    def test_l9_gitignore_covers_ship_state(self):
        lines = (_harness.REPO / ".gitignore").read_text().splitlines()
        self.assertIn(".orchestrator/ship-wt/", lines)
        self.assertIn(".orchestrator/ship.lock", lines)

    def test_l10_harness_disables_ship_and_temp_dirs_stay_local(self):
        from orchestrator.pool import config
        self.assertFalse(ship.settings(config())["enabled"])
        self.assertIn('mkdtemp(dir=os.environ["SHIP_TEST_TMP"])', FAKE_GH)
        self.assertIn('mktemp -d "$SHIP_TEST_TMP/', GATE)
        gid = self.goal()
        self.cfg["ship"]["gate_env"].update(MOVE_TARGET=str(self.origin), MOVE_ONCE=str(self.dir / "moved"))
        self.assertEqual(self.advance(gid)["status"], "shipped")
        self.assertTrue((self.dir / "moved").exists())
        self.assertEqual(list(self.tmp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
