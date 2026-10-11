import _harness

import fcntl
import io
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from orchestrator import bus

TIMEOUT_S = 10


class BusLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(bus, "LOCK", Path(self.tmp.name) / "bus.lock"); p.start(); self.addCleanup(p.stop)

    def run_threads(self, target, n=2):
        threads = [threading.Thread(target=target, name=f"worker-{i}", daemon=True) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(TIMEOUT_S)
        return [t for t in threads if t.is_alive()]

    def test_two_threads_and_nested_finish(self):
        inside, overlaps, done = [0], [], []

        def work():
            for _ in range(20):
                with bus.locked():
                    with bus.locked():  # nested: re-entrant within the thread
                        inside[0] += 1
                        if inside[0] != 1:
                            overlaps.append(inside[0])
                        time.sleep(0.001)
                        inside[0] -= 1
            done.append(threading.current_thread().name)

        self.assertEqual(self.run_threads(work, n=3), [], "bus.locked() hung across threads")
        self.assertEqual(len(done), 3)
        self.assertEqual(overlaps, [])  # serialized across threads

    def test_per_descriptor_flock_hangs_before_fix(self):
        """The pre-fix pattern: each holder opens its own descriptor. On one process, the second flock blocks
        behind the first even though both belong to the same process (timeout-guarded)."""
        first = open(bus.LOCK, "a+")
        self.addCleanup(first.close)
        fcntl.flock(first, fcntl.LOCK_EX)
        acquired = threading.Event()

        def second():
            with open(bus.LOCK, "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                acquired.set()
                fcntl.flock(fh, fcntl.LOCK_UN)

        t = threading.Thread(target=second, daemon=True)
        t.start()
        self.assertFalse(acquired.wait(0.5), "a second descriptor took the flock while the first held it")
        fcntl.flock(first, fcntl.LOCK_UN)
        t.join(TIMEOUT_S)
        self.assertTrue(acquired.is_set())

    def test_one_process_lock_and_descriptor(self):
        seen = set()

        def work():
            with bus.locked():
                with bus.locked():
                    seen.add(id(bus._descriptor()))
                    self.assertTrue(bus._rlock._is_owned())

        self.assertEqual(self.run_threads(work, n=4), [])
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(bus._rlock, type(threading.RLock()))

    def test_long_wait_warns_stage_and_holder_once(self):
        holding, release = threading.Event(), threading.Event()

        def holder():
            with bus.locked(stage="holder_stage"):
                holding.set()
                release.wait(TIMEOUT_S)

        t = threading.Thread(target=holder, name="holder-thread", daemon=True)
        t.start()
        self.assertTrue(holding.wait(TIMEOUT_S))
        threading.Timer(0.6, release.set).start()
        err = io.StringIO()
        with mock.patch.object(bus, "lock_wait_warn_s", return_value=0.1), redirect_stderr(err):
            with bus.locked(stage="waiter_stage"):
                pass
        t.join(TIMEOUT_S)
        lines = [line for line in err.getvalue().splitlines() if "lock wait" in line]
        self.assertEqual(len(lines), 1, err.getvalue())
        self.assertIn("waiter_stage", lines[0])
        self.assertIn("holder-thread", lines[0])
        self.assertIn("holder_stage", lines[0])

    def test_warn_threshold_from_pool_toml(self):
        state = Path(self.tmp.name)
        (state / "pool.toml").write_text("[daemon]\nlock_wait_warn_s = 7\n")
        with mock.patch.object(bus, "STATE", state):
            self.assertEqual(bus.lock_wait_warn_s(), 7.0)
            (state / "pool.toml").write_text("[daemon]\n")
            self.assertEqual(bus.lock_wait_warn_s(), bus.LOCK_WAIT_WARN_S)


if __name__ == "__main__":
    unittest.main()
