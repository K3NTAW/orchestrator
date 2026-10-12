import _harness
"""orchestrator.gate slot accounting against the gate registry: gates_dir() follows gate.STATE, and every live
registry entry in the repo, a ship gate included, takes one [gate].max_parallel slot."""
import json, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrator import gate, machine


class GateRegistrySlots(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.enterContext(mock.patch.object(gate, "STATE", self.state))

    def live_entry(self, key):
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        gate.gates_dir().mkdir(parents=True, exist_ok=True)
        (gate.gates_dir() / f"{key}.json").write_text(json.dumps({
            "task_id": key, "pid": proc.pid, "pgid": proc.pid, "proc_start": machine.process_start(proc.pid)}))

    def test_gates_dir_follows_state(self):
        self.assertEqual(gate.gates_dir(), self.state / "gates")

    def test_live_ship_gate_entry_takes_a_slot(self):
        cfg = {"gate": {"max_parallel": 1}}
        self.assertTrue(gate.try_acquire_slot("T-free", cfg))
        gate.release_slot("T-free")
        self.live_entry("ship-T-1749")
        self.assertEqual(gate.live_slots(), 1)
        self.assertFalse(gate.try_acquire_slot("T-blocked", cfg))
        self.assertTrue(gate.try_acquire_slot("T-second", {"gate": {"max_parallel": 2}}))
        gate.release_slot("T-second")


if __name__ == "__main__":
    unittest.main()
