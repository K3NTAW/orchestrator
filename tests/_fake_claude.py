"""Fake claude CLI for spawn tests. The resolver drops non-absolute shutil.which results and falls back to fixed
install paths, so patching shutil.which to "claude" only passes on machines that happen to have
/opt/homebrew/bin/claude. Tests that fake the CLI patch the resolver itself to an absolute path instead."""
from unittest import mock

from orchestrator import claude_cli

PATH = "/nonexistent/fake-bin/claude"


def patch():
    """Patch claude_cli.resolve to the absolute fake path; use as a context manager or start()/stop()."""
    return mock.patch.object(claude_cli, "resolve", return_value=PATH)


_module_patch = None


def setUpModule():
    """Module-wide fake for test files whose spawns all expect a CLI; a test can still patch resolve to None."""
    global _module_patch
    _module_patch = patch()
    _module_patch.start()


def tearDownModule():
    global _module_patch
    if _module_patch is not None:
        _module_patch.stop()
        _module_patch = None
