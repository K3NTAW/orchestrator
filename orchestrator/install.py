"""Scaffold the §9 orchestrator layout into another repo, so `ORCH_ROOT=<target>` and a Planner session can run
there. `git worktree add` only ever checks out committed files, so a target repo needs this scaffold committed
before Planner/spawn/bus/merge (all rooted at ORCH_ROOT, see orchestrator/__init__.py) will work on it."""
import json, os, re, shutil, stat, subprocess, sys
from pathlib import Path

PACKAGE_REPO = Path(__file__).resolve().parents[1]  # this repo, regardless of ORCH_ROOT

IGNORE_LINES = [
    "wt/", ".orchestrator/bus.sqlite*", ".orchestrator/runs/", ".orchestrator/pool_state.json",
    ".orchestrator/merge.lock", ".orchestrator/bus.lock", ".orchestrator/daemon.lock",
    "graphify-out/", ".mcp.json",
]

MEMORY_FILES = {
    "decisions.md": "# decisions\n",
    "gotchas.md": "# gotchas\n",
    "model-notes.md": "# model-notes\n",
    "architecture.md": "# architecture\n\n(empty — overwritten summary, not a log)\n",
}

PLAN_TEMPLATE = ("# plan.md — Planner checkpoint\n"
                  "(empty: no goal in progress. The Planner overwrites this after every fan-out and merge.)\n")

GUARDRAIL_HEADER = "# the guardrails themselves"


def _git_toplevel(path: Path):
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def _shorten(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s.startswith(home + os.sep) or s == home else s


def _rewrite_protected_paths(text: str, target: Path) -> str:
    replacements = [
        _shorten(target / ".claude" / "hooks"),
        _shorten(target / ".claude" / "settings.json"),
        _shorten(target / ".orchestrator" / "protected-paths.txt"),
    ]
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        out.append(lines[i]); i += 1
        if lines[i - 1].strip() == GUARDRAIL_HEADER:
            out.extend(replacements)
            i += len(replacements)  # drop the source repo's own 3 lines
    return "\n".join(out) + "\n"


def _rewrite_mcp(data: dict, target: Path, orch_repo: Path) -> dict:
    for name, server in data.get("mcpServers", {}).items():
        if server.get("command") != "uv":
            continue  # e.g. github: copied unchanged
        args = list(server.get("args", []))
        if "run" in args:
            idx = args.index("run")
            if args[idx:idx + 2] != ["run", "--project"]:
                args[idx + 1:idx + 1] = ["--project", str(orch_repo)]
        server["args"] = args
        server["env"] = {**server.get("env", {}), "ORCH_ROOT": str(target)}
    return data


def install(target, orch_repo: Path = PACKAGE_REPO) -> list[str]:
    target = Path(target)
    top = _git_toplevel(target) if target.exists() else None
    if top is None or os.path.realpath(top) != os.path.realpath(target):
        print(f"install: {target} is not a git repository toplevel", file=sys.stderr)
        raise SystemExit(2)

    target = target.resolve()
    orch_repo = Path(orch_repo).resolve()
    report: list[str] = []

    def rel(p: Path) -> str:
        return str(p.relative_to(target))

    def create_if_absent(dst: Path, write) -> None:
        if dst.exists():
            report.append(f"kept {rel(dst)}")
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        write(dst)
        report.append(f"created {rel(dst)}")

    def copy_always(src: Path, dst: Path) -> None:
        existed = dst.exists()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        report.append(f"{'updated' if existed else 'created'} {rel(dst)}")

    def write_always(dst: Path, content: str) -> None:
        existed = dst.exists()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content)
        report.append(f"{'updated' if existed else 'created'} {rel(dst)}")

    def copy_tree_always(src: Path, dst: Path) -> None:
        for dirpath, _dirnames, filenames in os.walk(src, followlinks=True):
            rel_dir = Path(dirpath).relative_to(src)
            for fname in filenames:
                copy_always(Path(dirpath) / fname, dst / rel_dir / fname)

    # .orchestrator/pool.toml — copy, kept if present
    create_if_absent(target / ".orchestrator" / "pool.toml",
                      lambda dst: shutil.copy2(orch_repo / ".orchestrator" / "pool.toml", dst))

    # .orchestrator/prompts/* — copy, always updated
    copy_tree_always(orch_repo / ".orchestrator" / "prompts", target / ".orchestrator" / "prompts")

    # .orchestrator/protected-paths.txt — copy, rewritten to the target's own guardrail paths, always updated
    src_text = (orch_repo / ".orchestrator" / "protected-paths.txt").read_text()
    write_always(target / ".orchestrator" / "protected-paths.txt", _rewrite_protected_paths(src_text, target))

    # .orchestrator/memory/*.md — header-only, kept if present
    for name, header in MEMORY_FILES.items():
        create_if_absent(target / ".orchestrator" / "memory" / name, lambda dst, h=header: dst.write_text(h))

    # .orchestrator/plan.md — empty checkpoint template, kept if present
    create_if_absent(target / ".orchestrator" / "plan.md", lambda dst: dst.write_text(PLAN_TEMPLATE))

    # .orchestrator/tasks/.gitkeep
    create_if_absent(target / ".orchestrator" / "tasks" / ".gitkeep", lambda dst: dst.write_text(""))

    # .claude/hooks/* — copy, executable bits preserved (shutil.copy2 preserves mode), always updated
    copy_tree_always(orch_repo / ".claude" / "hooks", target / ".claude" / "hooks")

    # .claude/settings.json — copy; if the target already has its own, write settings.orchestrator.json instead
    src_settings = orch_repo / ".claude" / "settings.json"
    dst_settings = target / ".claude" / "settings.json"
    if dst_settings.exists() and dst_settings.read_bytes() != src_settings.read_bytes():
        alt = target / ".claude" / "settings.orchestrator.json"
        before = alt.exists()
        create_if_absent(alt, lambda dst: shutil.copy2(src_settings, dst))
        if not before:
            report.append(f"note: {rel(dst_settings)} already exists, left untouched; "
                           f"orchestrator config lives in {rel(alt)}")
    elif dst_settings.exists():
        report.append(f"kept {rel(dst_settings)}")
    else:
        copy_always(src_settings, dst_settings)

    # .claude/skills/** and skills/** — copy, always updated
    copy_tree_always(orch_repo / ".claude" / "skills", target / ".claude" / "skills")
    copy_tree_always(orch_repo / "skills", target / "skills")

    # every .mcp*.json — rewrite uv commands to --project <orch_repo> and set env.ORCH_ROOT, always updated
    for src in sorted(orch_repo.glob(".mcp*.json")):
        data = _rewrite_mcp(json.loads(src.read_text()), target, orch_repo)
        write_always(target / src.name, json.dumps(data, indent=2) + "\n")

    # .gitignore — append this repo's ignore set, no duplicates
    gitignore = target / ".gitignore"
    existing = set(gitignore.read_text().splitlines()) if gitignore.exists() else set()
    missing = [line for line in IGNORE_LINES if line not in existing]
    if missing:
        existed = gitignore.exists()
        with gitignore.open("a") as f:
            if existed and existing:
                f.write("\n")
            f.write("\n".join(missing) + "\n")
        report.append(f"{'updated' if existed else 'created'} .gitignore (+{len(missing)} lines)")
    else:
        report.append("kept .gitignore")

    # .orchestrator/tests.sh — only if the target has a Makefile with test/lint/typecheck targets
    makefile = target / "Makefile"
    if makefile.exists():
        text = makefile.read_text()
        has = lambda name: re.search(rf"(?m)^{re.escape(name)}\s*:", text)
        if has("test") and has("lint") and has("typecheck"):
            def write_tests_sh(dst: Path) -> None:
                lines = ["#!/usr/bin/env bash", "set -euo pipefail", "make lint", "make typecheck", "make test"]
                web_pkg = target / "clients" / "web" / "package.json"
                if web_pkg.exists():
                    try:
                        scripts = json.loads(web_pkg.read_text()).get("scripts", {})
                    except json.JSONDecodeError:
                        scripts = {}
                    if "typecheck" in scripts:
                        lines.append("(cd clients/web && npm run typecheck)")
                dst.write_text("\n".join(lines) + "\n")
                dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            create_if_absent(target / ".orchestrator" / "tests.sh", write_tests_sh)

    return report
