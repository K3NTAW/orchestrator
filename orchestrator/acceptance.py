"""Helpers for checking test ids named by task acceptance criteria."""
import re
from pathlib import Path


_TEST_ID = re.compile(
    r"(?P<path>tests/[A-Za-z0-9_./-]+\.py)::(?P<name>[A-Za-z_]\w*)"
    r"|(?<![A-Za-z0-9_./-])::(?P<short>[A-Za-z_]\w*)"
)


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


def missing_tests(worktree, acceptance):
    """Return acceptance test ids whose files do not define the named function."""
    root = Path(worktree)
    missing = []
    for path, name in named_tests(acceptance):
        try:
            contents = (root / path).read_text()
        except (OSError, UnicodeError):
            missing.append((path, name))
            continue
        if not re.search(rf"^\s*def\s+{re.escape(name)}\s*\(", contents, re.MULTILINE):
            missing.append((path, name))
    return missing
