import _harness

import io
import resource
import unittest
from unittest import mock

from orchestrator import daemon, machine, watchdog


def _cfg(value=None):
    return {"daemon": {} if value is None else {"nofile_soft": value}}


class RaiseNofileTest(unittest.TestCase):
    def _run(self, soft, hard, cfg):
        log = io.StringIO()
        with mock.patch.object(machine.resource, "getrlimit", return_value=(soft, hard)) as get, \
                mock.patch.object(machine.resource, "setrlimit") as setr:
            result = machine.raise_nofile(cfg, label="test", log=log)
        get_called = get.called
        return result, setr, log.getvalue(), get_called

    def test_raises_256_to_default(self):
        result, setr, out, _ = self._run(256, 1 << 20, _cfg())
        self.assertEqual(result, (256, 65536))
        setr.assert_called_once_with(resource.RLIMIT_NOFILE, (65536, 1 << 20))
        self.assertIn("256 -> 65536", out)

    def test_keeps_higher_limit(self):
        result, setr, out, _ = self._run(100000, 1 << 20, _cfg(65536))
        self.assertIsNone(result)
        setr.assert_not_called()
        self.assertEqual(out, "")

    def test_keeps_infinite_soft_limit(self):
        result, setr, _, _ = self._run(resource.RLIM_INFINITY, resource.RLIM_INFINITY, _cfg(65536))
        self.assertIsNone(result)
        setr.assert_not_called()

    def test_clamps_to_hard(self):
        result, setr, _, _ = self._run(256, 10240, _cfg(65536))
        self.assertEqual(result, (256, 10240))
        setr.assert_called_once_with(resource.RLIMIT_NOFILE, (10240, 10240))

    def test_zero_does_nothing(self):
        result, setr, _, get_called = self._run(256, 1 << 20, _cfg(0))
        self.assertIsNone(result)
        setr.assert_not_called()
        self.assertFalse(get_called)

    def test_setrlimit_failure_is_logged(self):
        log = io.StringIO()
        with mock.patch.object(machine.resource, "getrlimit", return_value=(256, 1 << 20)), \
                mock.patch.object(machine.resource, "setrlimit", side_effect=ValueError("nope")):
            self.assertIsNone(machine.raise_nofile(_cfg(), label="test", log=log))
        self.assertIn("could not raise", log.getvalue())

    def test_pool_toml_sets_65536(self):
        import tomllib
        from pathlib import Path
        cfg = tomllib.loads((Path(__file__).resolve().parents[1] / ".orchestrator" / "pool.toml").read_text())
        self.assertEqual(cfg["daemon"]["nofile_soft"], 65536)


class StartPathTest(unittest.TestCase):
    def test_daemon_start_raises_limit(self):
        with mock.patch.object(machine.resource, "getrlimit", return_value=(256, 1 << 20)), \
                mock.patch.object(machine.resource, "setrlimit") as setr, \
                mock.patch.object(machine, "_nofile_setting", return_value=65536), \
                mock.patch.object(daemon, "Pool"), mock.patch.object(daemon, "tick"), \
                mock.patch.object(daemon, "_drain_gates"), mock.patch.object(daemon, "terminate_own_gates"), \
                mock.patch("sys.stderr", io.StringIO()):
            daemon.main(once=True)
        setr.assert_called_once_with(resource.RLIMIT_NOFILE, (65536, 1 << 20))

    def test_watchdog_start_raises_limit(self):
        with mock.patch.object(machine.resource, "getrlimit", return_value=(256, 1 << 20)), \
                mock.patch.object(machine.resource, "setrlimit") as setr, \
                mock.patch.object(machine, "_nofile_setting", return_value=65536), \
                mock.patch.object(watchdog, "run_once"), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(watchdog.run(once=True), 0)
        setr.assert_called_once_with(resource.RLIMIT_NOFILE, (65536, 1 << 20))

    def test_daemon_start_skips_when_zero(self):
        with mock.patch.object(machine.resource, "setrlimit") as setr, \
                mock.patch.object(machine, "_nofile_setting", return_value=0), \
                mock.patch.object(daemon, "Pool"), mock.patch.object(daemon, "tick"), \
                mock.patch.object(daemon, "_drain_gates"), mock.patch.object(daemon, "terminate_own_gates"):
            daemon.main(once=True)
        setr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
