"""Shared hermetic fixtures for tests that must not see the checkout's live operator settings.

The checkout's .orchestrator/pool.toml carries live operator settings (2026-10-09, PR 50: every Codex executor
disabled, budgets and routing modes raised), so the suite never reads it. tests/_harness.py builds the shared TMP
root's pool.toml from tests/fixtures/pool.toml (the pool.toml committed at 569f4b1) instead. BASELINE_POOL_TOML is
that same sanitized text, for tests that overwrite the shared file and need to put the baseline back.
A test module that depends on pool settings calls pin_pool() in setUpModule and the returned restore in
tearDownModule, so every reader of STATE / "pool.toml" (pool.config, allocation, watchdog, promotion, bus) sees the
baseline for that module only and later modules see the harness file unchanged."""
from pathlib import Path

from _harness import POOL_TOML, TMP

BASELINE_POOL_TOML = POOL_TOML


def pin_pool(state=None):
    """Write BASELINE_POOL_TOML over <state>/pool.toml (default: the shared TMP root) and return a callable that
    restores the previous file exactly."""
    path = Path(state or TMP / ".orchestrator") / "pool.toml"
    previous = path.read_text() if path.exists() else None
    path.write_text(BASELINE_POOL_TOML)

    def restore():
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous)
    return restore
