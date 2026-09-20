"""skills/planner/memory/scripts: record.sh (draft/add/set) and recall.sh (index/get) over the orchestrator's
memory files, plus the retrospect-written hook accepting what record.sh writes."""
import importlib.util, io, json, os, subprocess, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_memory_skill.py` doesn't add this dir itself
from _harness import HOOKS, REPO, TMP, hook  # noqa: F401
from orchestrator import bus, jev


class MemorySkill(unittest.TestCase):
    S = REPO / "skills" / "planner" / "memory" / "scripts"

    def run_s(self, name, *args, stdin=None):
        env = {**os.environ, "CLAUDE_MEM_DB": str(TMP / "no.db"), "GRAPHIFY_OUT": str(TMP / "no-graph")}
        return subprocess.run(["bash", str(self.S / name), *args], input=stdin, capture_output=True, text=True, cwd=REPO, env=env)

    def load_recall(self):
        """Import recall.py in-process (not via the recall.sh subprocess) so a test can monkeypatch
        orchestrator.jev.ask and have jev_rank.rank see the patch -- a subprocess wouldn't."""
        spec = importlib.util.spec_from_file_location(f"recall_mod_{id(self)}", self.S / "recall.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_record_then_recall(self):
        today = time.strftime("%Y-%m-%d")
        goal = bus.create_task("GOAL: memory skill", "spec", ["works"], ["skills/**"])
        kid = bus.create_task("Map memory", "where", ["cites"], ["skills/**"], parent=goal["id"])
        bus.post_result(kid["id"], {"summary": "memory lives in .orchestrator/memory",
                                    "findings": [{"claim": "four files", "evidence": ["CLAUDE.md:11"], "confidence": 0.9}]})
        d = self.run_s("record.sh", "draft", goal["id"])
        self.assertEqual(d.returncode, 0, d.stderr); self.assertIn(kid["id"], d.stdout); self.assertIn("CLAUDE.md:11", d.stdout)
        a = self.run_s("record.sh", "add", "--file", "decisions", "--type", "decision", "--title", "Layered memory skill",
                       "--goal", goal["id"], "--fact", "skills/planner/memory/SKILL.md:1 — three layers", "--outcome", "shipped")
        self.assertEqual(a.returncode, 0, a.stderr)
        txt = (TMP / ".orchestrator" / "memory" / "decisions.md").read_text()
        self.assertIn(f"## {today} Layered memory skill", txt); self.assertNotIn("(empty", txt)
        for i in range(2):  # same gotcha title twice -> one entry, latest body
            self.run_s("record.sh", "add", "--file", "gotchas", "--type", "gotcha", "--title", "shebang trips guardrails",
                       "--goal", goal["id"], "--fact", f"fact {i}")
        g = (TMP / ".orchestrator" / "memory" / "gotchas.md").read_text()
        self.assertEqual(g.count("shebang trips guardrails"), 1); self.assertIn("fact 1", g); self.assertNotIn("fact 0", g)
        s = self.run_s("record.sh", "add", "--file", "decisions", "--type", "decision", "--title", "x", "--goal", goal["id"],
                       "--fact", "token ghp_" + "a" * 30)
        self.assertEqual(s.returncode, 1); self.assertIn("secret", s.stderr)
        bad = self.run_s("record.sh", "add", "--file", "architecture", "--type", "decision", "--title", "x", "--goal", goal["id"])
        self.assertEqual(bad.returncode, 1)
        self.run_s("record.sh", "set", "architecture", stdin="entry: orchestrator/cli.py")
        self.run_s("record.sh", "set", "architecture", stdin="entry: orchestrator/mcp.py")
        arch = (TMP / ".orchestrator" / "memory" / "architecture.md").read_text()
        self.assertIn("mcp.py", arch); self.assertNotIn("cli.py", arch); self.assertIn(f"updated {today}", arch)
        ix = self.run_s("recall.sh", "index", "layered memory skill")
        self.assertEqual(ix.returncode, 0, ix.stderr)
        self.assertIn("mem:decisions.md:", ix.stdout); self.assertIn(f"bus:{kid['id']}", ix.stdout); self.assertNotIn("cmem:", ix.stdout)
        mem_id = next(l.split(" · ")[0] for l in ix.stdout.splitlines() if l.startswith("mem:decisions"))
        got = self.run_s("recall.sh", "get", mem_id, f"bus:{kid['id']}", "mem:decisions.md:999")
        self.assertIn("three layers", got.stdout); self.assertIn("four files", got.stdout); self.assertIn("no entry", got.stdout)
        self.assertEqual(self.run_s("recall.sh", "index", "zzqx-nothing-matches").stdout.strip()[:7], "no hits")
        # the retrospect-written hook accepts what record.sh wrote
        self.assertEqual(hook("retrospect-written.sh", {"task_input": {"subject": "GOAL: memory"}}, cwd=TMP).returncode, 0)

    def test_recall_goal_flag_ranks(self):
        goal = bus.create_task("GOAL: jev rank flag", "spec", ["works"], ["skills/**"])
        kid = bus.create_task("Map jev rank flag work", "where", ["cites"], ["skills/**"], parent=goal["id"])
        bus.post_result(kid["id"], {"summary": "jev rank flag summary"})
        recall = self.load_recall()

        orig_ask = jev.ask
        self.addCleanup(setattr, jev, "ask", orig_ask)

        jev.ask = lambda state, questions, **kw: {"answers": {qid: {"noul": 0.9} for qid in questions}}
        out = io.StringIO()
        with redirect_stdout(out):
            recall.cmd_index(["jev", "rank", "flag", "--goal", "ship the jev rank flag"])
        text = out.getvalue()
        self.assertIn("· p", text.splitlines()[0])
        self.assertIn(f"bus:{kid['id']}", text)
        self.assertIn("0.90", text)
        self.assertNotIn("jev: off", text)

        jev.ask = lambda state, questions, **kw: None  # jev disabled/unavailable
        out = io.StringIO()
        with redirect_stdout(out):
            recall.cmd_index(["jev", "rank", "flag", "--goal", "ship the jev rank flag"])
        self.assertIn("jev: off", out.getvalue())

    def test_recall_stops_after_budget_hits_in_notes_layer(self):
        recall = self.load_recall()
        calls = {"bus": 0, "cmem": 0, "graph": 0}
        recall.index_notes = lambda terms: [(1, "mem:x:1", "", "mem", "one"), (1, "mem:x:2", "", "mem", "two")]
        recall.index_bus = lambda terms: calls.__setitem__("bus", calls["bus"] + 1) or []
        recall.index_cmem = lambda *args: calls.__setitem__("cmem", calls["cmem"] + 1) or []
        recall.index_graph = lambda terms: calls.__setitem__("graph", calls["graph"] + 1) or []
        result = recall.recall("one two", budget_hits=2)
        self.assertEqual(result["layers_consulted"], ["notes"])
        self.assertEqual(result["stopped_at"], "notes")
        self.assertEqual(calls, {"bus": 0, "cmem": 0, "graph": 0})

    def test_recall_skips_unavailable_layer_and_names_it(self):
        recall = self.load_recall()
        recall.index_notes = lambda terms: []
        recall.index_bus = lambda terms: []
        recall.CMEM = TMP / "missing.db"
        recall.LESSONS = TMP / "missing-lessons.md"
        result = recall.recall("nothing")
        self.assertEqual(result["layers_consulted"], ["notes", "bus", "claude-mem unavailable", "graph unavailable"])

    def test_recall_logs_memory_run_row_with_task_and_layers(self):
        task = bus.create_task("memory recall row", "s", ["a"], ["x.py"])
        recall = self.load_recall()
        recall.index_notes = lambda terms: [(1, "mem:x:1", "", "mem", "memory")]
        result = recall.recall("memory", layers=("notes",), task=task["id"])
        row = json.loads(sorted(bus.RUNS.glob("*.jsonl"))[-1].read_text().splitlines()[-1])
        self.assertEqual(row["role"], "memory")
        self.assertEqual(row["task"], task["id"])
        self.assertEqual(row["goal_id"], task["id"])
        self.assertEqual(row["layers_consulted"], result["layers_consulted"])

    def test_recall_progressive_cli_header(self):
        recall = self.load_recall()
        recall.index_notes = lambda terms: [(1, "mem:x:1", "", "mem", "memory")]
        out = io.StringIO()
        with redirect_stdout(out):
            recall.cmd_index(["--progressive", "memory"])
        self.assertIn("layers_consulted: notes", out.getvalue())

    def test_recall_uses_explicit_root_not_import_time_cwd(self):
        with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as elsewhere_dir:
            root, elsewhere = Path(root_dir), Path(elsewhere_dir)
            memory = root / ".orchestrator" / "memory"
            tasks = root / ".orchestrator" / "tasks"
            memory.mkdir(parents=True)
            tasks.mkdir(parents=True)
            (memory / "gotchas.md").write_text("## 2026-09-20 Root-only gotcha\nwidget cache fact\n")
            (tasks / "T-root.json").write_text(json.dumps({
                "id": "T-root", "role": "execute", "status": "done", "title": "Root-only task",
                "spec": "widget cache result", "result": {"summary": "root-only result"}, "events": []}))
            old_cwd, old_root = Path.cwd(), os.environ.get("ORCH_ROOT")
            try:
                os.environ["ORCH_ROOT"] = str(elsewhere)
                os.chdir(elsewhere)
                recall = self.load_recall()
                result = recall.recall("root-only widget cache", root=root, layers=("notes", "bus"))
            finally:
                os.chdir(old_cwd)
                if old_root is None:
                    os.environ.pop("ORCH_ROOT", None)
                else:
                    os.environ["ORCH_ROOT"] = old_root
            self.assertEqual({hit["layer"] for hit in result["hits"]}, {"mem", "bus"})
            self.assertTrue(all("root-only" in hit["title"].lower() for hit in result["hits"]))


if __name__ == "__main__":
    unittest.main()
