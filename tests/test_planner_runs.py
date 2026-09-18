"""orchestrator.planner_runs: autonomous decision points (scouts_done/held/closable), the dedup/blocking rules in
.orchestrator/runs/planner_runs.json, and daemon.tick()'s autonomous-launch gate. goals.launch_planner and
goals.identity_of are patched per test so nothing here spawns a real `claude` subprocess or reads real process
state; every test gets its own sandbox for bus.STATE/TASKS/RUNS, handover.STATE/ROOT and planner_runs.STATE so
plan.md, tasks and planner_runs.json never touch the shared TMP root other test files use."""
import json, os, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_planner_runs.py` doesn't add this dir itself
from _harness import REPO, TMP  # noqa: F401
from orchestrator import bus, daemon, goals, handover
from orchestrator import planner_runs as PR
from orchestrator import pool as P


class PlannerRunsBase(unittest.TestCase):
    def setUp(self):
        sandbox = Path(tempfile.mkdtemp(prefix="orch-planner-runs-"))
        for mod, name, value in (
            (bus, "STATE", sandbox), (bus, "TASKS", sandbox / "tasks"), (bus, "RUNS", sandbox / "runs"),
            (handover, "STATE", sandbox), (handover, "ROOT", sandbox),
            (PR, "STATE", sandbox),
        ):
            self.swap(mod, name, value)
        P.PERSIST.unlink(missing_ok=True)
        self.addCleanup(P.PERSIST.unlink, True)
        self.clear_env("ORCH_NOTIFY_URL")
        self.clear_env("ORCH_NOTIFY_DESKTOP")
        self.clear_env("ORCH_DAEMON_HOST")

    def swap(self, mod, name, value):
        orig = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(setattr, mod, name, orig)

    def clear_env(self, name):
        had = name in os.environ
        orig = os.environ.pop(name, None)
        self.addCleanup(lambda: os.environ.__setitem__(name, orig) if had else os.environ.pop(name, None))

    def set_env(self, name, value):
        had = name in os.environ
        orig = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(lambda: os.environ.__setitem__(name, orig) if had else os.environ.pop(name, None))

    def patch_launch_planner(self, fn):
        self.swap(goals, "launch_planner", fn)

    def patch_identity_of(self, fn):
        self.swap(goals, "identity_of", fn)

    def goal(self, title="Goal"):
        t = bus.create_task(f"GOAL: {title}", title, ["Planner closes the goal with a PR"], ["**"],
                            role="triage", complexity=5)
        return t["id"]

    def scout_child(self, goal_id, status="done"):
        t = bus.create_task("scout", "spec", ["x"], ["y"], role="scout", parent=goal_id, complexity=3)
        if status != "queued":
            bus.update(t["id"], status=status)
        return t["id"]

    def execute_child(self, goal_id, **fields):
        t = bus.create_task("execute", "spec", ["x"], ["y"], role="execute", parent=goal_id, complexity=3)
        if fields:
            bus.update(t["id"], **fields)
        return t["id"]

    def record(self, goal_id, kind, payload_key):
        for r in PR._load_records():
            if r["goal_id"] == goal_id and r["kind"] == kind and r["payload_key"] == payload_key:
                return r
        return None


class DecisionPoints(PlannerRunsBase):
    def test_decision_points_scouts_done_held_closable(self):
        g1 = self.goal("scouts")
        self.scout_child(g1, "done")
        self.scout_child(g1, "failed")
        self.assertIn((g1, "scouts_done", g1), list(PR.decision_points()))

        g2 = self.goal("held")
        tid = self.execute_child(g2)
        bus.update(tid, status="held", hold_reason="gate_red")
        held_key = PR._held_key(bus.get(tid))
        self.assertIsNotNone(held_key)
        self.assertIn((g2, "held", held_key), list(PR.decision_points()))

        g3 = self.goal("closable")
        etid = self.execute_child(g3)
        bus.update(etid, status="done", merged_into=f"goal/{g3}")
        self.assertIn((g3, "closable", g3), list(PR.decision_points()))

        # scouts_done must stop firing once an execute child exists for that goal.
        self.execute_child(g1)
        self.assertNotIn((g1, "scouts_done", g1), list(PR.decision_points()))

        # closable must not fire while a sibling is still queued or running.
        g4 = self.goal("not-closable")
        e1 = self.execute_child(g4)
        bus.update(e1, status="done", merged_into=f"goal/{g4}")
        self.execute_child(g4)  # second child left "queued"
        self.assertNotIn((g4, "closable", g4), list(PR.decision_points()))

    def test_held_key_changes_with_each_new_hold(self):
        goal_id = self.goal()
        tid = self.execute_child(goal_id)
        bus.update(tid, status="held", hold_reason="gate_red")
        key1 = PR._held_key(bus.get(tid))
        self.assertIsNotNone(key1)

        # A decision for key1 already gave up -- it must never block a fresh hold on the same task.
        PR._record_running(goal_id, "held", key1, {"pid": 1, "pid_start": None, "log": "x"}, "A", 1)
        records = PR._load_records()
        for r in records:
            if r["payload_key"] == key1:
                r["status"] = "gave_up"
        PR._save_records(records)
        self.assertNotIn((goal_id, "held", key1), list(PR.decision_points()))

        time.sleep(0.01)
        bus.update(tid, status="queued")
        bus.update(tid, status="held", hold_reason="gate_red")
        key2 = PR._held_key(bus.get(tid))
        self.assertNotEqual(key1, key2)
        self.assertIn((goal_id, "held", key2), list(PR.decision_points()))


class RunGuards(PlannerRunsBase):
    def test_run_skips_when_daemon_host_is_mcp(self):
        goal_id = self.goal()
        calls = []
        self.patch_launch_planner(lambda *a, **k: calls.append(a) or {"pid": 1, "pid_start": None, "log": "x"})
        os.environ["ORCH_DAEMON_HOST"] = "mcp"

        r = PR.run(goal_id, "scouts_done", goal_id)
        self.assertFalse(r["launched"])
        self.assertEqual(r["reason"], "planner session attached")
        self.assertEqual(calls, [])

        rec = self.record(goal_id, "scouts_done", goal_id)
        self.assertEqual(rec["status"], "skipped")
        self.assertEqual(rec["skipped_reason"], "planner session attached")
        self.assertNotIn("pid", rec)

    def test_run_skips_when_planner_session_file_has_live_pid_and_ignores_stale(self):
        goal_id = self.goal()
        calls = []
        self.patch_launch_planner(lambda *a, **k: calls.append(a) or {"pid": 1, "pid_start": None, "log": "x"})
        session_path = PR.STATE / "planner_session.json"
        session_path.parent.mkdir(parents=True, exist_ok=True)

        session_path.write_text(json.dumps({"pid": 424242, "pid_start": None}))
        self.patch_identity_of(lambda pid, pid_start: pid == 424242)  # only the "live" pid identifies

        r = PR.run(goal_id, "scouts_done", goal_id)
        self.assertFalse(r["launched"])
        self.assertEqual(r["reason"], "planner session attached")
        self.assertEqual(calls, [])

        # A stale file (dead or reused pid) must not block.
        session_path.write_text(json.dumps({"pid": 999999, "pid_start": None}))
        r2 = PR.run(goal_id, "held", "some-other-key")
        self.assertTrue(r2["launched"], r2)
        self.assertEqual(len(calls), 1)

    def test_run_skips_without_account_headroom(self):
        goal_id = self.goal()
        calls = []
        self.patch_launch_planner(lambda *a, **k: calls.append(a) or {"pid": 1, "pid_start": None, "log": "x"})
        pool = P.Pool()
        pool.cooldown(pool.get("A"), 600)
        pool.cooldown(pool.get("B"), 600)

        r = PR.run(goal_id, "scouts_done", goal_id)
        self.assertFalse(r["launched"])
        self.assertEqual(r["reason"], "no account with headroom")
        self.assertEqual(calls, [])

        rec = self.record(goal_id, "scouts_done", goal_id)
        self.assertEqual(rec["status"], "skipped")
        self.assertEqual(rec["skipped_reason"], "no account with headroom")

    def test_run_launches_once_per_key(self):
        goal_id = self.goal()
        calls = []
        self.patch_launch_planner(lambda *a, **k: calls.append(a) or {"pid": 777, "pid_start": None, "log": "x"})

        r1 = PR.run(goal_id, "scouts_done", goal_id)
        self.assertTrue(r1["launched"], r1)
        self.assertEqual(len(calls), 1)
        rec = self.record(goal_id, "scouts_done", goal_id)
        self.assertEqual(rec["status"], "running")

        r2 = PR.run(goal_id, "scouts_done", goal_id)
        self.assertFalse(r2["launched"])
        self.assertEqual(len(calls), 1)  # unchanged: the running record already blocks a second launch


class Reconcile(PlannerRunsBase):
    def test_reconcile_exited_ok_exited_early_gave_up_and_gave_up_blocks_redecision(self):
        goal_id = self.goal()
        notified = []
        self.swap(daemon, "notify", lambda msg: notified.append(msg))

        # scouts_done -> exited_ok: an execute child appears while the record's process is gone.
        PR._record_running(goal_id, "scouts_done", goal_id, {"pid": 111, "pid_start": None, "log": "x"}, "A", 0)
        self.execute_child(goal_id)
        self.patch_identity_of(lambda pid, pid_start: False)
        PR.reconcile()
        rec = self.record(goal_id, "scouts_done", goal_id)
        self.assertEqual(rec["status"], "exited_ok")
        self.assertEqual(notified, [])

        # held -> exited_early (attempts 1, retry allowed), then a second early exit -> gave_up (one notify).
        tid = self.execute_child(goal_id)
        bus.update(tid, status="held", hold_reason="gate_red")
        key = PR._held_key(bus.get(tid))
        PR._record_running(goal_id, "held", key, {"pid": 222, "pid_start": None, "log": "x"}, "A", 0)

        PR.reconcile()
        rec1 = self.record(goal_id, "held", key)
        self.assertEqual(rec1["status"], "exited_early")
        self.assertEqual(rec1["attempts"], 1)
        self.assertEqual(notified, [])
        self.assertIn((goal_id, "held", key), list(PR.decision_points()))  # not blocked yet

        PR._record_running(goal_id, "held", key, {"pid": 223, "pid_start": None, "log": "y"}, "A", rec1["attempts"])
        PR.reconcile()
        rec2 = self.record(goal_id, "held", key)
        self.assertEqual(rec2["status"], "gave_up")
        self.assertEqual(rec2["attempts"], 2)
        self.assertEqual(len(notified), 1)

        # gave_up blocks forever, even though the task is still held exactly the same way.
        self.assertNotIn((goal_id, "held", key), list(PR.decision_points()))
        PR.reconcile()  # idempotent: no further transition, no second notify
        rec3 = self.record(goal_id, "held", key)
        self.assertEqual(rec3["status"], "gave_up")
        self.assertEqual(len(notified), 1)


class McpSessionRegistration(PlannerRunsBase):
    def test_mcp_registers_and_deregisters_planner_session(self):
        # orchestrator.mcp is never imported elsewhere in this test suite (module-level code starts a real
        # daemon thread and the MCP server object) -- ORCH_DAEMON=0 must be set before this, its first and only
        # import in the whole process, so that autostart never spins one up against the shared TMP root.
        self.set_env("ORCH_DAEMON", "0")

        import orchestrator.mcp as mcp_mod

        sandbox = Path(tempfile.mkdtemp(prefix="orch-mcp-session-"))
        self.swap(mcp_mod, "STATE", sandbox)

        path = mcp_mod.register_planner_session()
        self.assertEqual(path, sandbox / "planner_session.json")
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["host"], "mcp")
        self.assertIn("started_at", data)
        self.assertEqual(os.environ.get("ORCH_DAEMON_HOST"), "mcp")

        mcp_mod.deregister_planner_session()
        self.assertFalse(path.exists())

        # A registration from a different (newer) pid must never be deleted by a stale deregister call.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid() + 12345, "pid_start": None,
                                    "started_at": time.time(), "host": "mcp"}))
        mcp_mod.deregister_planner_session()
        self.assertTrue(path.exists())


class TickAutonomous(PlannerRunsBase):
    def test_tick_launches_nothing_when_autonomous_false(self):
        pool = P.Pool()
        self.assertFalse(pool.cfg.get("planner", {}).get("autonomous", False))
        calls = []
        self.swap(PR, "reconcile", lambda: calls.append("reconcile"))
        self.swap(PR, "decision_points", lambda: (calls.append("decision_points"), iter(()))[1])
        self.swap(PR, "run", lambda *a: calls.append(("run", a)))

        daemon.tick(pool)

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
