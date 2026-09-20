"""A compact, static map of the orchestrator package for worker context."""
import ast
import datetime as dt
import re
import subprocess
from pathlib import Path


def _first_sentence(text):
    text = " ".join((text or "").split())
    match = re.match(r".*?[.!?](?:\s|$)", text)
    sentence = match.group(0).strip() if match else text
    return sentence[:80].rstrip() + ("…" if len(sentence) > 80 else "")


def _parameters(arguments):
    names = [argument.arg for argument in arguments.posonlyargs + arguments.args]
    if arguments.vararg:
        names.append("*" + arguments.vararg.arg)
    elif arguments.kwonlyargs:
        names.append("*")
    names.extend(argument.arg for argument in arguments.kwonlyargs)
    if arguments.kwarg:
        names.append("**" + arguments.kwarg.arg)
    return ", ".join(names)


def _module_imports(tree, names):
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "orchestrator" and len(parts) > 1 and parts[1] in names:
                    imports.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").startswith("orchestrator"):
                parts = node.module.split(".")
                if len(parts) > 1 and parts[1] in names:
                    imports.add(parts[1])
                elif node.module == "orchestrator":
                    imports.update(alias.name for alias in node.names if alias.name in names)
            elif node.level == 1:
                if node.module and node.module.split(".")[0] in names:
                    imports.add(node.module.split(".")[0])
                elif not node.module:
                    imports.update(alias.name for alias in node.names if alias.name in names)
    return imports


def _render(sha, date, modules):
    lines = [f"repo map {sha} {date}"]
    for module in modules:
        description = f" — {module['doc']}" if module["doc"] else ""
        lines.extend(("", f"## {module['name']}.py{description}"))
        lines.extend(f"- {symbol}" for symbol in module["symbols"])
    return "\n".join(lines) + "\n"


def build(root, budget_chars=4000, rev=None):
    """Return a deterministic, AST-only markdown summary of ``orchestrator/*.py``."""
    root = Path(root)
    if rev is None:
        files = sorted((root / "orchestrator").glob("*.py"))
        sources = [(path, path.read_text()) for path in files]
        revision = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                                  capture_output=True, text=True, check=False)
        sha = revision.stdout.strip() or "unknown"
    else:
        listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", rev, "--", "orchestrator"],
                                cwd=root, capture_output=True, text=True, check=True).stdout.splitlines()
        paths = sorted(Path(path) for path in listed
                       if path.startswith("orchestrator/") and Path(path).parent == Path("orchestrator")
                       and Path(path).suffix == ".py")
        sources = [(path, subprocess.run(["git", "show", f"{rev}:{path.as_posix()}"], cwd=root,
                                         capture_output=True, text=True, check=True).stdout)
                   for path in paths]
        revision = subprocess.run(["git", "rev-parse", "--short", rev], cwd=root,
                                  capture_output=True, text=True, check=True)
        sha = revision.stdout.strip()
    parsed = []
    names = {path.stem for path, _ in sources}
    for path, source in sources:
        tree = ast.parse(source, filename=str(path))
        symbols = []
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("_"):
                continue
            if isinstance(node, ast.ClassDef):
                symbols.append(f"class {node.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                symbols.append(f"{prefix} {node.name}({_parameters(node.args)})")
        parsed.append({"name": path.stem, "doc": _first_sentence(ast.get_docstring(tree)),
                       "symbols": sorted(symbols), "imports": _module_imports(tree, names)})

    ranks = {module["name"]: 0 for module in parsed}
    for module in parsed:
        for imported in module["imports"]:
            ranks[imported] += 1
    date = dt.date.today().isoformat()
    modules = [{**module, "symbols": list(module["symbols"])} for module in parsed]
    while len(_render(sha, date, modules)) > budget_chars:
        populated = [module for module in modules if module["symbols"]]
        if not populated:
            break
        # Keep a useful public API sketch for every module until the larger
        # modules have been reduced to the same three-symbol floor.
        candidates = [module for module in populated if len(module["symbols"]) > 3]
        if not candidates:
            candidates = populated
        module = min(candidates, key=lambda item: (
            -len(item["symbols"]), ranks[item["name"]], item["name"]
        ))
        module["symbols"].pop(0)
    return _render(sha, date, modules)[:budget_chars]
