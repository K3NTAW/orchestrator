"""One resolver for the `claude` and `uv` binaries. A daemon started from a shell without the Homebrew bin dir on
PATH failed every spawn with "claude CLI not found on PATH" (luna-inbox T-0599, 2026-10-06), so every spawn
resolves the absolute path here: PATH first, then the usual install locations."""
import os
import shutil

NAME = "claude"
CANDIDATES = ("/opt/homebrew/bin/claude", "/usr/local/bin/claude", "~/.local/bin/claude", "~/.claude/local/claude")
UV_CANDIDATES = ("/opt/homebrew/bin/uv", "~/.local/bin/uv", "~/.cargo/bin/uv")
HOMEBREW_BIN = "/opt/homebrew/bin"


def _first(name, candidates):
    found = shutil.which(name)
    if found and os.path.isabs(found):  # a relative PATH entry resolves against whatever cwd spawns it
        return found
    for candidate in candidates:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def resolve() -> str | None:
    """Absolute path of the claude CLI, or None when neither PATH nor any known location has it."""
    return _first(NAME, CANDIDATES)


def resolve_uv() -> str | None:
    return _first("uv", UV_CANDIDATES)


def argv(*args) -> list:
    """The argv every claude spawn uses; run it with Popen(..., executable=resolve())."""
    return [NAME, *args]


def missing_reason() -> str:
    return "claude CLI not found; looked in: PATH, " + ", ".join(CANDIDATES)
