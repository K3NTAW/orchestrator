import _harness  # noqa: F401 - share the suite's single isolated ORCH_ROOT
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = _harness.REPO / "scripts" / "with-tokens.sh"
FAKE_F = (
    'f() { [ "$1 $2" = "tok get" ] && case "$3" in a-key) printf secret-a;; b-key) printf secret-b;; '
    "*) return 1;; esac; }\n"
)
SECRETS = ("secret-a", "secret-b")
PRINT_LEN = ["python3", "-c", "import os,sys;print(' '.join(str(len(os.environ[k])) for k in sys.argv[1:]))"]


class WithTokensTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.tmp / "f.sh").write_text(FAKE_F)
        self.allowlist = self.tmp / "allowlist.txt"
        self.allowlist.write_text("# tokens the human allows\n\na-key\n  b-key  \nmissing-key\n")

    def run_script(self, *args, allowlist=None):
        env = dict(os.environ)
        env["WITH_TOKENS_F_SH"] = str(self.tmp / "f.sh")
        env["WITH_TOKENS_ALLOWLIST"] = str(allowlist or self.allowlist)
        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30
        )

    def assert_no_secret(self, proc):
        for secret in SECRETS:
            self.assertNotIn(secret, proc.stdout)
            self.assertNotIn(secret, proc.stderr)

    def test_runs_command_with_token_in_env(self):
        proc = self.run_script("A=a-key", "--", "python3", "-c", "import os;print(len(os.environ['A']))")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "8")
        self.assertNotIn("secret-a", proc.stderr)
        self.assert_no_secret(proc)

    def test_two_tokens(self):
        proc = self.run_script("A=a-key", "B_2=b-key", "--", *PRINT_LEN, "A", "B_2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "8 8")
        self.assert_no_secret(proc)

    def test_not_allowlisted_refused(self):
        self.allowlist.write_text("b-key\n")
        proc = self.run_script("A=a-key", "--", *PRINT_LEN, "A")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("with-tokens: a-key is not allowlisted", proc.stderr)
        self.assert_no_secret(proc)

    def test_missing_allowlist_refuses_all(self):
        proc = self.run_script("A=a-key", "--", *PRINT_LEN, "A", allowlist=self.tmp / "absent.txt")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("with-tokens: no allowlist", proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.allowlist.write_text("# only comments\n\n")
        proc = self.run_script("A=a-key", "--", *PRINT_LEN, "A")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("with-tokens: no allowlist", proc.stderr)
        self.assert_no_secret(proc)

    def test_missing_token_exits_1_and_names_id_only(self):
        proc = self.run_script("A=a-key", "M=missing-key", "--", *PRINT_LEN, "A", "M")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("with-tokens: missing or empty token: missing-key", proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assert_no_secret(proc)

    def test_bad_env_name_refused(self):
        for name in ("lower", "PATH", "LD_PRELOAD", "1A"):
            with self.subTest(name=name):
                proc = self.run_script(f"{name}=a-key", "--", *PRINT_LEN)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assert_no_secret(proc)
        proc = self.run_script("A=bad/id", "--", *PRINT_LEN)
        self.assertEqual(proc.returncode, 2)

    def test_requires_double_dash_and_command(self):
        for args in (("A=a-key",), ("A=a-key", "--"), ("A=a-key", "python3"), ("--", "python3"), ()):
            with self.subTest(args=args):
                proc = self.run_script(*args)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("Usage:", proc.stderr)
                self.assert_no_secret(proc)

    def test_list_prints_allowlist_only(self):
        proc = self.run_script("--list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "a-key\nb-key\nmissing-key\n")
        self.assert_no_secret(proc)
        proc = self.run_script("--list", allowlist=self.tmp / "absent.txt")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()
