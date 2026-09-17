"""Write orchestrator memory. `draft <GOAL>` builds an entry from the goal's child tasks; `add` appends one dated entry
(gotcha titles deduped); `set architecture` overwrites the summary file. Planner only; hook-enforced by retrospect-written.sh."""
import json, os, re, sys
from datetime import date
from pathlib import Path

ROOT = Path(os.environ.get("ORCH_ROOT") or Path.cwd())
MEM = ROOT / ".orchestrator" / "memory"
TASKS = ROOT / ".orchestrator" / "tasks"
FILES = {"decisions", "gotchas", "model-notes"}
TYPES = {"decision", "gotcha", "discovery", "model"}
DECISIONS_BUDGET = 300
SECRET = re.compile(r"(sk-[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})")
HEAD = re.compile(r"^## (\d{4}-\d{2}-\d{2}) (.+)$", re.M)
TODAY = date.today().isoformat()


def load(tid):
    p = TASKS / f"{tid}.json"
    if not p.exists():
        sys.exit(f"no task {tid}")
    return json.loads(p.read_text())


def cmd_draft(argv):
    if not argv:
        sys.exit("usage: record.sh draft <GOAL_ID>")
    goal = load(argv[0])
    kids = sorted((json.loads(p.read_text()) for p in TASKS.glob("T-*.json")), key=lambda t: t["id"])
    kids = [t for t in kids if t.get("parent") == goal["id"]]
    ids = ",".join(t["id"] for t in kids)
    print(f"## {TODAY} {goal['title']}")
    print(f"type: decision · goal: {goal['id']} · tasks: {ids or '-'} · provenance: repo")
    for t in kids:
        r = t.get("result") or {}
        summ = str(r.get("summary") or r.get("verdict") or "").strip().replace("\n", " ")[:160]
        print(f"- [{t['role']}/{t['status']}] {t['title']}: {summ or 'no result'}")
        for f in (r.get("findings") or [])[:3]:
            ev = ",".join(f.get("evidence") or [])[:80]
            print(f"  - {ev} — {str(f.get('claim', ''))[:120]} (conf {f.get('confidence', '?')})")
        for c in (r.get("comments") or [])[:3]:
            print(f"  - {c.get('path', '')}:{c.get('line', '')} — review: {str(c.get('issue', ''))[:120]} [{c.get('severity', '')}]")
        if t.get("status") == "failed":
            print(f"  - failed: {str(t.get('resume_hint') or r.get('reason') or '')[:160]}")
    print("outcome: <merged/abandoned + the alternative that lost and why>")
    print(f"# edit the lines above, drop transcript detail, then: record.sh add --file decisions --type decision "
          f"--title ... --goal {goal['id']} --fact ... --outcome ...", file=sys.stderr)


def parse_add(argv):
    o = {"fact": [], "provenance": "repo", "tasks": ""}
    i = 0
    while i < len(argv):
        k = argv[i]
        if not k.startswith("--") or i + 1 >= len(argv):
            sys.exit(f"bad argument {k}")
        v = argv[i + 1]; k = k[2:]
        if k == "fact":
            o["fact"].append(v)
        else:
            o[k] = v
        i += 2
    for req in ("file", "type", "title", "goal"):
        if not o.get(req):
            sys.exit(f"--{req} is required")
    if o["file"] not in FILES:
        sys.exit("--file must be decisions|gotchas|model-notes (architecture uses `set`)")
    if o["type"] not in TYPES:
        sys.exit("--type must be decision|gotcha|discovery|model")
    return o


def render(o):
    meta = f"type: {o['type']} · goal: {o['goal']}" + (f" · tasks: {o['tasks']}" if o["tasks"] else "") + f" · provenance: {o['provenance']}"
    lines = [f"## {TODAY} {o['title']}", meta] + [f"- {f}" for f in o["fact"]]
    if o.get("outcome"):
        lines.append(f"outcome: {o['outcome']}")
    return "\n".join(lines)


def cmd_add(argv):
    o = parse_add(argv)
    text = render(o)
    if SECRET.search(text):
        sys.exit("refusing: entry looks like it contains a secret")
    MEM.mkdir(parents=True, exist_ok=True)
    p = MEM / f"{o['file']}.md"
    cur = p.read_text() if p.exists() else f"# {o['file']}\n"
    cur = re.sub(r"\n\(empty[^\n]*\)\n?", "\n", cur)  # drop the scaffold placeholder line
    if o["file"] == "gotchas":
        heads = list(HEAD.finditer(cur))
        for n, m in enumerate(heads):
            if m.group(2) == o["title"]:
                end = heads[n + 1].start() if n + 1 < len(heads) else len(cur)
                body = text.split("\n", 1)[1]
                new = f"## {TODAY} {o['title']}\n(updated {TODAY}; first seen {m.group(1)})\n{body}\n\n"
                cur = cur[:m.start()] + new + cur[end:]
                p.write_text(cur.rstrip() + "\n"); print(f"updated existing gotcha in {p.relative_to(ROOT)}"); return
    p.write_text(cur.rstrip() + "\n\n" + text + "\n")
    n = len(p.read_text().splitlines())
    print(f"appended to {p.relative_to(ROOT)} ({n} lines)")
    if o["file"] == "decisions" and n > DECISIONS_BUDGET:
        print(f"warning: decisions.md is {n} lines (> {DECISIONS_BUDGET}); run skill compact-memory", file=sys.stderr)


def cmd_set(argv):
    if argv != ["architecture"]:
        sys.exit("usage: record.sh set architecture < summary.md")
    body = sys.stdin.read().strip()
    if not body:
        sys.exit("empty stdin")
    if SECRET.search(body):
        sys.exit("refusing: summary looks like it contains a secret")
    MEM.mkdir(parents=True, exist_ok=True)
    (MEM / "architecture.md").write_text(f"# architecture\nupdated {TODAY}\n\n{body}\n")
    print(f"overwrote .orchestrator/memory/architecture.md ({len(body.splitlines()) + 3} lines)")


if __name__ == "__main__":
    a = sys.argv[1:]
    cmds = {"draft": cmd_draft, "add": cmd_add, "set": cmd_set}
    if not a or a[0] not in cmds:
        sys.exit("usage: record.sh draft <GOAL_ID> | add --file F --type T --title .. --goal .. [--tasks ..] [--fact ..]... "
                 "[--outcome ..] [--provenance ..] | set architecture < summary.md")
    cmds[a[0]](a[1:])
