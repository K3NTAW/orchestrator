"""Deterministic conditional instruction selection and composition."""
from pathlib import Path

from . import STATE, promotion


def _scope(task):
    return [str(path) for path in task.get("scope", [])]


def _language(task):
    scope = _scope(task)
    if scope and all(Path(path).suffix.lower() == ".md" for path in scope):
        return "docs"
    suffixes = {Path(path).suffix.lower() for path in scope}
    for suffix, language in ((".py", "python"), (".ts", "typescript"),
                             (".tsx", "typescript"), (".swift", "swift")):
        if suffix in suffixes:
            return language
    return None


def _security(task, role, signals):
    return role == "security_review" or bool((signals or {}).get("security"))


RULES = [
    ("python-unittest", lambda task, role, signals: (_language(task) == "python", "python scope")),
    ("tool-budget", lambda task, role, signals: (role == "execute", "execute role")),
    ("review-security", lambda task, role, signals: (_security(task, role, signals), "security review")),
    ("docs-task", lambda task, role, signals: (_language(task) == "docs", "docs-only scope")),
    ("migrations", lambda task, role, signals: (
        any("migrations" in path.lower() or "alembic" in path.lower() for path in _scope(task)),
        "migration scope")),
]


def select(task, role, signals=None):
    selected, reasons = [], {}
    mandatory = ["review-security"] if role == "security_review" else []
    for module, predicate in RULES:
        matched, reason = predicate(task, role, signals)
        if matched or module in mandatory:
            selected.append(module)
            reasons[module] = reason
    return {"modules": selected, "mandatory": mandatory, "reasons": reasons}


def module_text(name):
    return (STATE / "prompts" / "modules" / f"{name}.md").read_text()


def compose(base_text, modules):
    ordered = [name for name, _ in RULES if name in modules]
    if not ordered:
        return base_text
    body = "\n".join(module_text(name).rstrip("\n") for name in ordered)
    return base_text.rstrip("\n") + "\n\n## conditional instructions\n" + body + "\n"


def mode():
    return promotion.mode("conditional_instructions")
