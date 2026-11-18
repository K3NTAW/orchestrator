"""Helpers for checking test ids named by task acceptance criteria."""
import re
import shutil
import subprocess
from pathlib import Path


_TEST_ID = re.compile(
    r"(?P<path>tests/[A-Za-z0-9_./-]+\.py)::(?P<name>[A-Za-z_]\w*)"
    r"|(?<![A-Za-z0-9_./-])::(?P<short>[A-Za-z_]\w*)"
)


_TEST_FILE_SUFFIXES = ("js", "jsx", "ts", "tsx", "mjs", "cjs")


def test_file_paths(acceptance):
    """Return acceptance-named test file paths, including non-Python test files."""
    paths = set()
    for criterion in acceptance or []:
        if not isinstance(criterion, str):
            continue
        for token in criterion.split():
            token = token.strip("\"'`[](){}")
            token = token.rstrip(":,;.")
            if not token or "*" in token or token.endswith("/"):
                continue
            if "::" in token:
                left, _ = token.split("::", 1)
                if left.endswith(".py"):
                    continue
                token = left
            path = Path(token)
            if not path.suffix:
                continue
            parts = path.parts
            name = path.name
            in_test_directory = any(part in {"test", "tests", "__tests__"} for part in parts[:-1])
            stem = path.stem
            language_test_name = (
                any(name.endswith(f".test.{suffix}") or name.endswith(f".spec.{suffix}")
                    for suffix in _TEST_FILE_SUFFIXES)
                or name.endswith("Test.swift") or name.endswith("Tests.swift")
                or name.endswith("Test.kt") or name.endswith("Tests.kt")
                or name.endswith("Test.java") or name.endswith("Tests.java")
            )
            if in_test_directory or language_test_name:
                paths.add(token)
    return sorted(paths)


_ADD_VERB = re.compile(
    r"\b(?:adds?|added|new|creates?|created|extends?|extended|gains?|writes?|written)\b",
    re.IGNORECASE,
)


_CLAUSE_SPLIT = re.compile(r";|\.(?=\s+[A-Z]|\s*$)")

_UNCHANGED_AFTER = re.compile(
    r"\b(?:unchanged|untouched|not\s+modified|not\s+changed|stays?|remains?)\b",
    re.IGNORECASE,
)

_UNCHANGED_BEFORE = re.compile(r"\bno\s+changes?\s+to\b", re.IGNORECASE)

_GROUP_SEPARATOR = re.compile(r"^(?:\s|,|\band\b|\bor\b)*$", re.IGNORECASE)


def _paths_needing_diff(clause):
    """Return test files named in one clause, minus those asserted unchanged.

    Adjacent files joined only by commas, 'and' or 'or' form a group; a group is
    exempt when the text after it (up to the next file) carries an unchanged
    marker, or the text before it carries 'no change(s) to'."""
    hits = []
    for match in re.finditer(r"\S+", clause):
        found = test_file_paths([match.group()])
        if found:
            hits.append((match.start(), match.end(), found[0]))
    groups = []
    for start, end, path in hits:
        if groups and _GROUP_SEPARATOR.match(clause[groups[-1]["end"]:start]):
            groups[-1]["paths"].append(path)
            groups[-1]["end"] = end
        else:
            groups.append({"start": start, "end": end, "paths": [path]})
    result = set()
    for index, group in enumerate(groups):
        before = clause[groups[index - 1]["end"] if index else 0:group["start"]]
        after = clause[group["end"]:groups[index + 1]["start"] if index + 1 < len(groups) else len(clause)]
        if _UNCHANGED_AFTER.search(after) or _UNCHANGED_BEFORE.search(before):
            continue
        result.update(group["paths"])
    return result


class MissingTestFile(str):
    """A missing test file path that also says why: absent or not_in_diff."""

    def __new__(cls, path, reason):
        entry = super().__new__(cls, path)
        entry.reason = reason
        return entry


def missing_test_files(worktree, acceptance, changed=None):
    """Return named test files absent from the worktree, or absent from the task diff
    when the clause naming them asks to add or change tests and does not assert
    them unchanged."""
    root = Path(worktree)
    changed_set = set(changed) if isinstance(changed, list) else None
    needs_diff = set()
    for criterion in acceptance or []:
        if not isinstance(criterion, str):
            continue
        for clause in _CLAUSE_SPLIT.split(criterion):
            if _ADD_VERB.search(clause):
                needs_diff.update(_paths_needing_diff(clause))
    missing = []
    for path in test_file_paths(acceptance):
        if not (root / path).is_file():
            missing.append(MissingTestFile(path, "absent"))
        elif changed_set is not None and path in needs_diff and path not in changed_set:
            missing.append(MissingTestFile(path, "not_in_diff"))
    return missing


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
