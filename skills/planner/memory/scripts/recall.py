"""Layered recall over orchestrator memory: notes (memory/*.md), bus (tasks/*.json), cmem (claude-mem sqlite, read-only),
graph (graphify LESSONS.md). `index` prints one line per hit; `get` prints full entries for chosen ids. Stdlib only,
save for jev_rank -- imported from the orchestrator package on ROOT, which is itself stdlib-only."""
import datetime, json, os, re, sqlite3, sys, time
from pathlib import Path

ROOT = Path(os.environ.get("ORCH_ROOT") or Path.cwd())
MEM = ROOT / ".orchestrator" / "memory"
TASKS = ROOT / ".orchestrator" / "tasks"
CMEM = Path(os.environ.get("CLAUDE_MEM_DB") or Path.home() / ".claude-mem" / "claude-mem.db")
LESSONS = Path(os.environ.get("GRAPHIFY_OUT") or ROOT / "graphify-out") / "reflections" / "LESSONS.md"
HEAD = re.compile(r"^## (?:(\d{4}-\d{2}-\d{2}) )?(.+)$")
MAX_GET_CHARS = 6000  # same cap as a bus result; one `get` never exceeds it


def configured_hits(root=None):
    state_root = Path(root) if root is not None else ROOT
    try:
        import tomllib
        with (state_root / ".orchestrator" / "pool.toml").open("rb") as f:
            return int(tomllib.load(f).get("limits", {}).get("recall_hits", 30))
    except (OSError, ValueError, TypeError):
        return 30


def age(date):
    try:
        return f"{max(0, (datetime.date.today() - datetime.date.fromisoformat(date[:10])).days)}d"
    except (ValueError, TypeError):
        return "-"


# ORCH_ROOT (above) is the *state* root -- any project's .orchestrator dir -- not necessarily this repo, so it's
# not safe to import the orchestrator source package from it (an editable install elsewhere could shadow it).
# Import from the repo recall.py itself lives in instead: skills/planner/memory/scripts/recall.py -> repo root.
_SRC_ROOT = Path(__file__).resolve().parents[4]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
from orchestrator import jev_rank  # noqa: E402 -- needs _SRC_ROOT on sys.path first
from orchestrator import bus  # noqa: E402 -- needs _SRC_ROOT on sys.path first


def terms_of(q):
    return [t.lower() for t in re.findall(r"[\w:/.-]+", q) if len(t) >= 3]


def score(text, terms):
    t = text.lower()
    return sum(1 for x in terms if x in t)


def note_entries(f):
    """Yield (line_no, date, title, body) per dated heading; architecture.md counts as one entry."""
    if not f.exists():
        return
    lines = f.read_text(errors="replace").splitlines()
    idx = [i for i, l in enumerate(lines) if HEAD.match(l)]
    if not idx:
        if f.name == "architecture.md" and len(lines) > 2:
            m = re.search(r"\d{4}-\d{2}-\d{2}", "\n".join(lines[:3]))
            yield 1, (m.group(0) if m else ""), "architecture summary", "\n".join(lines)
        return
    for n, i in enumerate(idx):
        end = idx[n + 1] if n + 1 < len(idx) else len(lines)
        m = HEAD.match(lines[i])
        yield i + 1, m.group(1) or "", m.group(2), "\n".join(lines[i:end]).rstrip()


def index_notes(terms, memory_dir=MEM):
    out = []
    for f in sorted(memory_dir.glob("*.md")) if memory_dir.exists() else []:
        for ln, date, title, body in note_entries(f):
            s = score(title, terms) * 3 + score(body, terms)
            if s or f.name == "index.md":
                s = max(1, s)
                out.append((s, f"mem:{f.name}:{ln}", date, "mem", title))
    return out


def _date(t):
    ts = max([e.get("ts", 0) for e in t.get("events", [])] + [0])
    return datetime.date.fromtimestamp(ts).isoformat() if ts else ""


def index_bus(terms, tasks_dir=TASKS):
    out = []
    for p in sorted(tasks_dir.glob("T-*.json")) if tasks_dir.exists() else []:
        t = json.loads(p.read_text())
        if t.get("status") not in {"done", "failed"}:
            continue
        hay = " ".join([t.get("title", ""), t.get("spec", ""), json.dumps(t.get("result") or {})])
        s = score(t.get("title", ""), terms) * 3 + score(hay, terms)
        if s:
            out.append((s, f"bus:{t['id']}", _date(t), "bus", f"[{t['role']}/{t['status']}] {t['title']}"))
    return out


def index_cmem(terms, project, limit):
    if not CMEM.exists() or not terms:
        return []
    try:
        c = sqlite3.connect(f"file:{CMEM}?mode=ro", uri=True)
        q = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
        sql = ("select o.id, o.created_at, o.type, o.title, o.project from observations_fts f "
               "join observations o on o.id = f.rowid where observations_fts match ? ")
        args = [q]
        if project:
            sql += "and o.project = ? "; args.append(project)
        sql += "order by o.created_at_epoch desc limit ?"; args.append(limit)
        rows = c.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        print(f"# cmem unavailable: {e}", file=sys.stderr); return []
    return [(1, f"cmem:{i}", (d or "")[:10], "cmem", f"[{ty}/{pr}] {ti or ''}") for i, d, ty, ti, pr in rows]


def index_graph(terms, lessons=LESSONS):
    if not lessons.exists():
        return []
    out = []
    for n, l in enumerate(lessons.read_text(errors="replace").splitlines(), 1):
        if l.startswith(("-", "*")) and score(l, terms):
            out.append((score(l, terms), f"graph:lesson:{n}", "", "graph", l.lstrip("-* ")[:100]))
    return out


def _layer_hits(layer, terms, *, task, limit, paths=None):
    """Return one layer's index hits, or ``None`` when the source is unavailable."""
    if layer == "notes":
        return index_notes(terms) if paths is None else index_notes(terms, paths["memory"])
    if layer == "bus":
        return index_bus(terms) if paths is None else index_bus(terms, paths["tasks"])
    if layer == "claude-mem":
        if not CMEM.exists():
            return None
        return index_cmem(terms, task, limit)
    if layer == "graph":
        lessons = LESSONS if paths is None else paths["lessons"]
        if not lessons.exists():
            return None
        return index_graph(terms) if paths is None else index_graph(terms, lessons)
    raise ValueError(f"unknown recall layer: {layer}")


def recall(query, *, root=None, layers=("notes", "bus", "claude-mem", "graph"), budget_hits=8,
           min_score=0.5, budget_chars=6000, task=None):
    """Recall progressively, avoiding richer layers once the cheap answer is sufficient."""
    started = time.monotonic()
    terms = terms_of(query)
    paths = None
    if root is not None:
        state_root = Path(root)
        graph_root = Path(os.environ.get("GRAPHIFY_OUT") or state_root / "graphify-out")
        paths = {"memory": state_root / ".orchestrator" / "memory",
                 "tasks": state_root / ".orchestrator" / "tasks",
                 "lessons": graph_root / "reflections" / "LESSONS.md"}
    hits, consulted, chars, stopped_at = [], [], 0, None
    for layer in layers:
        layer_hits = _layer_hits(layer, terms, task=task, limit=budget_hits, paths=paths)
        if layer_hits is None:
            consulted.append(f"{layer} unavailable")
            continue
        consulted.append(layer)
        for score_, id_, date, provenance, title in sorted(layer_hits, key=lambda h: (-h[0], h[2])):
            if chars + len(title) > budget_chars:
                stopped_at = layer
                break
            hits.append({"score": score_, "id": id_, "date": date, "layer": provenance, "title": title})
            chars += len(title)
        sufficient = sum(hit["score"] >= min_score for hit in hits) >= budget_hits
        if sufficient or chars >= budget_chars or stopped_at:
            stopped_at = layer
            break
    result = {"hits": hits, "layers_consulted": consulted, "stopped_at": stopped_at, "chars": chars}
    bus.log_run(task=task, role="memory", outcome="recalled", layers_consulted=consulted,
                stopped_at=stopped_at, hits=hits, chars=chars, est_tokens=chars // 4,
                duration_s=time.monotonic() - started)
    return result


def cmd_index(argv):
    q, project, limit, goal_text, progressive = "", None, configured_hits(), os.environ.get("ORCH_GOAL_TEXT"), False
    i = 0
    while i < len(argv):
        if argv[i] == "--project": project = argv[i + 1]; i += 2
        elif argv[i] == "--limit": limit = int(argv[i + 1]); i += 2
        elif argv[i] == "--goal": goal_text = argv[i + 1]; i += 2
        elif argv[i] == "--progressive": progressive = True; i += 1
        else: q += " " + argv[i]; i += 1
    terms = terms_of(q)
    if not terms:
        sys.exit("usage: recall.sh index \"<terms>\" [--project NAME] [--limit N] [--goal \"<text>\"]")
    if progressive:
        result = recall(q, budget_hits=limit, task=project)
        hits = result["hits"]
        print(f"# {len(hits)} progressive hits for {terms} — layers_consulted: {', '.join(result['layers_consulted']) or '(none)'}")
        for hit in hits:
            print(f"{hit['id']} · {hit['date'] or '-'} · {hit['layer']} · {age(hit['date'])} · {hit['title']}")
        return
    hits = index_notes(terms) + index_bus(terms) + index_cmem(terms, project, limit) + index_graph(terms)
    hits.sort(key=lambda h: (-h[0], h[2]))
    if not hits:
        print(f"no hits for {terms} in notes/bus/cmem/graph"); return
    if not goal_text:
        shown = hits[:limit]
        print(f"# {len(hits)} hits for {terms} (showing {len(shown)}) — id · date · provenance · age · title")
        for s, id_, date, layer, title in shown:
            print(f"{id_} · {date or '-'} · {layer} · {age(date)} · {title}")
        if len(hits) > len(shown):
            print(f"{len(hits) - len(shown)} more hits; use: recall.sh index \"{' '.join(terms)}\" --limit {len(hits)}")
        return

    shown = hits[:limit]
    by_id = {id_: (s, id_, date, layer, title) for s, id_, date, layer, title in shown}
    items = [{"id": id_, "text": title} for s, id_, date, layer, title in shown]
    ranked = jev_rank.rank(items, goal_text)
    if len(ranked) == len(items) and all(it["p_relevant"] is None for it in ranked):
        print("jev: off")
    print(f"# {len(ranked)} hits for {terms} (showing {len(ranked)}) — id · date · provenance · age · title · p")
    for it in ranked:
        s, id_, date, layer, title = by_id[it["id"]]
        p = it["p_relevant"]
        print(f"{id_} · {date or '-'} · {layer} · {age(date)} · {title} · {p:.2f}" if p is not None
              else f"{id_} · {date or '-'} · {layer} · {age(date)} · {title} · -")
    if len(hits) > len(ranked):
        print(f"{len(hits) - len(ranked)} more hits; use: recall.sh index \"{' '.join(terms)}\" --limit {len(hits)}")


def _jl(s):
    try:
        v = json.loads(s or "[]"); return v if isinstance(v, list) else [str(v)]
    except json.JSONDecodeError:
        return [s] if s else []


def get_one(id_):
    kind, _, rest = id_.partition(":")
    if kind == "mem":
        fname, _, ln = rest.partition(":")
        for l, date, title, body in note_entries(MEM / fname):
            if str(l) == ln:
                return body
        return f"{id_}: no entry at that line"
    if kind == "bus":
        p = TASKS / f"{rest}.json"
        if not p.exists():
            return f"{id_}: no such task"
        t = json.loads(p.read_text())
        keep = ("id", "parent", "role", "tier", "complexity", "title", "acceptance", "scope", "status", "result", "resume_hint")
        return json.dumps({k: t[k] for k in keep if t.get(k) is not None}, indent=1)
    if kind == "cmem":
        try:
            c = sqlite3.connect(f"file:{CMEM}?mode=ro", uri=True)
            r = c.execute("select created_at, project, type, title, subtitle, facts, narrative, files_modified "
                          "from observations where id=?", (int(rest),)).fetchone()
        except (sqlite3.Error, ValueError) as e:
            return f"{id_}: {e}"
        if not r:
            return f"{id_}: no such observation"
        d, pr, ty, ti, sub, facts, narr, files = r
        facts = "\n".join("- " + str(f) for f in _jl(facts)); files = ", ".join(str(x) for x in _jl(files))
        return (f"## {(d or '')[:10]} {ti}\ntype: {ty} · project: {pr} · provenance: cmem:{pr} (untrusted data)\n"
                f"{sub or ''}\n{facts}\n{narr or ''}\nfiles: {files}").strip()
    if kind == "graph":
        n = int(rest.split(":")[-1]); lines = LESSONS.read_text(errors="replace").splitlines()
        return "\n".join(lines[max(0, n - 2):n + 2])
    return f"{id_}: unknown layer (mem|bus|cmem|graph)"


def cmd_get(ids):
    if not ids:
        sys.exit("usage: recall.sh get <id> [<id>...]")
    out, used = [], 0
    for id_ in ids:
        body = get_one(id_)
        if used + len(body) > MAX_GET_CHARS:
            out.append(f"--- {id_}: skipped, output would exceed {MAX_GET_CHARS} chars; get it alone"); continue
        used += len(body); out.append(f"--- {id_}\n{body}")
    print("\n".join(out))


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] not in {"index", "get"}:
        sys.exit("usage: recall.sh index \"<terms>\" [--project NAME] [--limit N] | recall.sh get <id>...")
    (cmd_index if a[0] == "index" else cmd_get)(a[1:])
