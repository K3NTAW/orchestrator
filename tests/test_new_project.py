import _harness
"""orchestrator.new_project: `orchestrator new <path>` creates or adopts an empty folder, scaffolds it with
install() and makes one commit, launching nothing. Runs against scratch folders under a temp dir only."""
import contextlib, io, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import scratch_repo
from orchestrator import cli, new_project
from orchestrator.new_project import NewProjectError, create

SUBJECT = "orchestrator: scaffold (new project)"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "commit.gpgsign",
           "GIT_CONFIG_VALUE_0": "false"}


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)


def snapshot(path):
    return sorted(str(p.relative_to(path)) for p in Path(path).rglob("*"))


class NewProjectTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="orch-new-")))

    def assert_scaffolded(self, p):
        self.assertTrue(p.is_dir())
        self.assertEqual(git(p, "branch", "--show-current").stdout.strip(), "main")
        self.assertEqual(git(p, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(git(p, "log", "-1", "--format=%s").stdout.strip(), SUBJECT)
        self.assertTrue((p / ".orchestrator" / "pool.toml").is_file())
        self.assertEqual(git(p, "status", "--porcelain").stdout, "")

    def test_new_creates_repo_with_scaffold_commit(self):
        p = self.tmp / "proj"
        report = create(str(p))
        self.assert_scaffolded(p)
        self.assertEqual(report[0], f"created {p}")
        self.assertIn("git init -b main", report)
        self.assertTrue(report[-1].startswith("commit "))

    def test_new_accepts_empty_dir(self):
        p = self.tmp / "empty"
        p.mkdir()
        report = create(str(p))
        self.assert_scaffolded(p)
        self.assertEqual(report[0], f"used existing {p}")

    def test_new_accepts_empty_initialised_repo(self):
        p = self.tmp / "inited"
        p.mkdir()
        git(p, "init", "-q", "-b", "main")
        report = create(str(p))
        self.assert_scaffolded(p)
        self.assertNotIn("git init -b main", report)

    def test_new_refuses_non_empty_dir(self):
        p = self.tmp / "full"
        p.mkdir()
        (p / "a.txt").write_text("x")
        before = snapshot(p)
        with self.assertRaises(NewProjectError):
            create(str(p))
        self.assertEqual(snapshot(p), before)

    def test_new_refuses_repo_with_commits(self):
        p = scratch_repo(self.tmp / "committed")
        head = git(p, "rev-parse", "HEAD").stdout
        before = snapshot(p)
        with self.assertRaises(NewProjectError):
            create(str(p))
        self.assertEqual(snapshot(p), before)
        self.assertEqual(git(p, "rev-parse", "HEAD").stdout, head)

    def test_new_refuses_missing_parent(self):
        p = self.tmp / "nope" / "proj"
        with self.assertRaises(NewProjectError) as cm:
            create(str(p))
        self.assertIn("parent does not exist", str(cm.exception))
        self.assertFalse((self.tmp / "nope").exists())

    def test_new_launches_nothing(self):
        # git subprocesses are expected; any other Popen (claude, uv, the daemon) or goals.start fails the test.
        def boom(*a, **k):
            raise AssertionError("launch attempted")
        real_popen = subprocess.Popen
        launched = []

        class GuardPopen(real_popen):
            def __init__(self, args, *a, **k):
                if not (isinstance(args, (list, tuple)) and args and args[0] == "git"):
                    launched.append(args)
                    raise AssertionError(f"launch attempted: {args}")
                super().__init__(args, *a, **k)

        with mock.patch("orchestrator.goals.start", boom), mock.patch("subprocess.Popen", GuardPopen):
            report = create(str(self.tmp / "quiet"))
        self.assertEqual(launched, [])
        self.assertTrue(report[-1].startswith("commit "))
        self.assertEqual(git(self.tmp / "quiet", "remote").stdout, "")

    def _run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", ["orchestrator", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_cli_new_exit_codes(self):
        bad = self.tmp / "bad"
        bad.mkdir()
        (bad / "f").write_text("x")
        code, out, err = self._run_cli(["new", str(bad)])
        self.assertEqual(code, 2)
        self.assertIn("new:", err)
        self.assertEqual(snapshot(bad), ["f"])

        good = self.tmp / "good"
        code, out, err = self._run_cli(["new", str(good)])
        self.assertEqual(code, 0, err)
        self.assertIn("Next:", out)
        self.assertIn(f"ORCH_ROOT={good}", out)


if __name__ == "__main__":
    unittest.main()
