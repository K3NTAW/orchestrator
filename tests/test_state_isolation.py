import _harness
"""Regression for the T-1749 ship gate (rounds 1-4): PR 50 committed runtime state (tasks, workers, locks,
ship_state) and live operator settings (pool.toml with every Codex row disabled) into the checkout's .orchestrator/.
The suite must pass regardless: state paths resolve under the harness TMP root, and pool-dependent modules read the
pinned baseline from tests/helpers.py instead of the checkout's live pool.toml."""
import json, os, re, subprocess, sys, tomllib, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO, TMP
import helpers
from orchestrator import ROOT, STATE, bus, daemon, pool as P, ship, worker_registry

FOREIGN = "T-9990"
TESTS = Path(__file__).resolve().parent
# The test_daemon tests that failed on the overlaid tree in every red ship gate.
DAEMON_TESTS = [
    "test_daemon.LiveGateRegistryIsolation",
    "test_daemon.Daemon.test_reservation_owner_matches_running_executor",
    "test_daemon.Daemon.test_dispatch_does_not_block",
    "test_daemon.Daemon.test_depends_on_gates_dispatch",
    "test_daemon.Daemon.test_dispatch_active_respects_slots_and_dependencies",
    "test_daemon.Daemon.test_fix_round_dispatch_resumes_parent_thread_when_compatible",
]


class StateIsolation(unittest.TestCase):
    def plant(self, path, text):
        """Create a foreign file in the checkout's real state dir for this test only. A file that already exists
        (committed runtime state on main) is left untouched and never removed."""
        if path.exists():
            return
        made = [p for p in reversed(path.parents) if p.is_relative_to(REPO / ".orchestrator") and not p.exists()]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.addCleanup(lambda: [path.unlink(missing_ok=True)] + [d.rmdir() for d in reversed(made)])

    def plant_foreign_state(self):
        real = REPO / ".orchestrator"
        task = {"id": FOREIGN, "title": "foreign", "status": "queued", "role": "execute", "complexity": 2,
                "spec": "x", "acceptance": ["x"], "scope": ["x.py"], "events": [], "pipeline": {}}
        self.plant(real / "tasks" / f"{FOREIGN}.json", json.dumps(task))
        self.plant(real / "workers" / f"{FOREIGN}.json", json.dumps({"task_id": FOREIGN, "status": "running"}))
        self.plant(real / "gates" / f"{FOREIGN}.json", json.dumps({"task_id": FOREIGN, "pid": os.getpid()}))
        self.plant(real / "daemon.lock", "")
        self.plant(real / "bus.lock", "")
        self.plant(real / "ship_state.json", json.dumps({"ship_enabled_at": 1, "goals": {
            "T-0001": {"state": "held", "hold_reason": "foreign"}}}))

    def test_state_paths_resolve_under_tmp_root(self):
        """Every module-level state path the suite touches lives under the harness root, never the checkout."""
        self.assertEqual(ROOT, TMP)
        for path in (STATE, bus.STATE, bus.TASKS, bus.RUNS, P.CFG, P.PERSIST, daemon.LOCK_PATH,
                     daemon.gate_runner.gates_dir(), worker_registry._path(FOREIGN), ship._state_path(ROOT)):
            self.assertTrue(Path(path).is_relative_to(TMP), path)
            self.assertFalse(Path(path).resolve().is_relative_to((REPO / ".orchestrator").resolve()), path)

    def test_foreign_checkout_state_is_invisible(self):
        self.plant_foreign_state()
        self.assertNotIn(FOREIGN, [t["id"] for t in bus.read()])
        with self.assertRaises(KeyError):
            bus.get(FOREIGN)
        self.assertNotIn(FOREIGN, [w.get("task_id") for w in worker_registry.listing(include_finished=True)])
        self.assertNotIn(FOREIGN, daemon.gate_runner._live_registry_keys())
        self.assertNotEqual(bus.next_id(), "T-9991")

    def test_daemon_suite_passes_with_foreign_checkout_state(self):
        """The once-failing test_daemon tests pass in a fresh process while the checkout's state dir holds foreign
        task, worker, gate, lock and ship_state files."""
        self.plant_foreign_state()
        env = {k: v for k, v in os.environ.items() if k not in ("ORCH_ROOT", "ORCH_MACHINE_DIR")}
        run = subprocess.run([sys.executable, "-m", "unittest", "-q", *DAEMON_TESTS], cwd=TESTS, env=env,
                             capture_output=True, text=True, timeout=600)
        self.assertEqual(run.returncode, 0, run.stderr[-4000:])

    def test_pin_pool_overrides_live_operator_settings(self):
        """A pool.toml with every executor disabled (PR 50's live setting) is replaced by the baseline while pinned,
        and restored byte for byte afterwards."""
        path = TMP / ".orchestrator" / "pool.toml"
        original = path.read_text()
        self.addCleanup(path.write_text, original)
        hostile = re.sub(r"(?m)^enabled\s*=\s*true", "enabled = false", helpers.BASELINE_POOL_TOML)
        path.write_text(hostile)
        self.assertFalse([e for e in tomllib.loads(hostile)["executors"] if e.get("enabled")])
        restore = helpers.pin_pool()
        try:
            baseline = {e["id"] for e in tomllib.loads(helpers.BASELINE_POOL_TOML)["executors"] if e.get("enabled")}
            self.assertTrue(baseline)
            self.assertEqual({e["id"] for e in P.config()["executors"] if e.get("enabled")}, baseline)
        finally:
            restore()
        self.assertEqual(path.read_text(), hostile)


if __name__ == "__main__":
    unittest.main()
