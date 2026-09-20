"""Helpers for checking test ids named by task acceptance criteria."""
import re
import shutil
import subprocess
from pathlib import Path


_TEST_ID = re.compile(
    r"(?P<path>tests/[A-Za-z0-9_./-]+\.py)::(?P<name>[A-Za-z_]\w*)"
    r"|(?<![A-Za-z0-9_./-])::(?P<short>[A-Za-z_]\w*)"
)


class NotCollectedTest(tuple):
    """A two-item missing-test result whose definition unittest will not collect."""

    reason = "not_collected"

    def __new__(cls, path, name):
        return super().__new__(cls, (path, name))


def named_tests(acceptance):
    """Return full and same-criterion shorthand test references in source order."""
    result = []
    for criterion in acceptance or []:
        if not isinstance(criterion, str):
            continue
        previous = None
        for match in _TEST_ID.finditer(criterion):
            if match.group("path"):
                previous = match.group("path")
                result.append((previous, match.group("name")))
            elif previous:
                result.append((previous, match.group("short")))
    return result


def _runner(worktree):
    root = Path(worktree)
    if (root / "pyproject.toml").is_file() and shutil.which("uv"):
        result = subprocess.run(
            ["uv", "run", "--project", str(root), "python", "-c", "import pytest"],
            capture_output=True,
        )
        return "pytest" if result.returncode == 0 else "unittest"
    return "pytest" if shutil.which("pytest") else "unittest"


def missing_tests(worktree, acceptance, runner=None):
    """Return acceptance test ids whose files do not define the named function."""
    root = Path(worktree)
    runner = runner or _runner(root)
    missing = []
    for path, name in named_tests(acceptance):
        try:
            contents = (root / path).read_text()
        except (OSError, UnicodeError):
            missing.append((path, name))
            continue
        if not re.search(rf"^\s*def\s+{re.escape(name)}\s*\(", contents, re.MULTILINE):
            missing.append((path, name))
        elif runner == "unittest" and re.search(
                rf"^def\s+{re.escape(name)}\s*\(", contents, re.MULTILINE):
            missing.append(NotCollectedTest(path, name))
    return missing
