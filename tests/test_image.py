"""Bring-up hardening from the C-O5 security review: the executor image and its guardrails. Grep-based
assertions only -- no docker build here, that's exercised on the host, not in this gate."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_image.py` doesn't add this dir itself
from _harness import REPO


class Image(unittest.TestCase):
    def test_protected_paths_cover_venv(self):
        text = (REPO / ".orchestrator" / "protected-paths.txt").read_text()
        self.assertIn("/opt/orchestrator/.venv", text)

    def test_dockerfile_pins_gh_and_asserts_version(self):
        text = (REPO / "Dockerfile").read_text()
        self.assertRegex(text, r"ARG GH_VERSION=\S+")
        self.assertIn("ARG GH_SHA256_AMD64=", text)
        self.assertIn("ARG GH_SHA256_ARM64=", text)
        self.assertRegex(text, r"echo\s+\"\$\{gh_sha256\}\s+/tmp/gh\.tar\.gz\"\s*\|\s*sha256sum -c -")
        self.assertRegex(text, r'gh --version \| grep -qF "gh version \$\{GH_VERSION\}"')
        # gh is no longer pulled from the floating apt repo
        self.assertNotIn("cli.github.com/packages", text)

    def test_compose_mounts_gitconfig_readonly(self):
        text = (REPO / "docker-compose.executor.yml").read_text()
        self.assertRegex(text, r"gitconfig:/home/orch/\.gitconfig:ro\b")

    def test_uv_runs_frozen(self):
        text = (REPO / "Dockerfile").read_text()
        self.assertIn("uv sync --frozen", text)
        self.assertIn("uv run --frozen orchestrator serve", text)


class Docs(unittest.TestCase):
    def test_executor_host_has_numbered_gitconfig_step(self):
        text = (REPO / "docs" / "executor-host.md").read_text()
        heading = re.compile(r"^## \d+\..*(?:[Gg]itconfig|git credential)", re.MULTILINE)
        self.assertRegex(text, heading)
        self.assertIn("gh auth setup-git", text)


if __name__ == "__main__":
    unittest.main()
