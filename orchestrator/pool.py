"""Account pool: per-account 5h window / daily budget / cooldown, least-loaded-with-headroom selection. State persists to
pool_state.json so MCP server restarts don't forget cooldowns."""
import json, os, re, sys, time, tomllib
from dataclasses import dataclass, field, asdict, fields
from datetime import date, datetime
from pathlib import Path
from . import ROOT, STATE

WINDOW_S = 5 * 3600
CFG = STATE / "pool.toml"
PERSIST = STATE / "pool_state.json"


def encode_project_dir(path: str) -> str:
    """Reproduces Claude Code's projects/ directory naming: every character that is not [A-Za-z0-9] becomes '-'
    (so '/' and '.' both map to '-'; '/Users/k3ntaw/.claude-mem' -> '-Users-k3ntaw--claude-mem'). Verified against
    live ~/.claude/projects/ and ~/.claude-a/projects/ directory names on 2026-09-18."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def config():
    return tomllib.loads(CFG.read_text())


@dataclass
class Account:
    id: str
    config_dir: str
    affinity: list
    reserve: float = 0.0
    daily_budget: int = 0
    oauth_token_env: str = ""
    cooldown_until: float = 0.0
    window_tokens: int = 0
    window_started: float = field(default_factory=time.time)
    day_tokens: int = 0
    day: str = field(default_factory=lambda: date.today().isoformat())
    hold_reason: str = ""
    planner_window_tokens: int = 0
    planner_day_tokens: int = 0
    planner_offsets: dict = field(default_factory=dict)

    def utilization(self, cap):
        if time.time() - self.window_started > WINDOW_S:
            self.window_tokens, self.window_started = 0, time.time()
            self.planner_window_tokens = 0
        if self.day != date.today().isoformat():
            self.day_tokens, self.day = 0, date.today().isoformat()
            self.planner_day_tokens = 0
        return (self.window_tokens + self.planner_window_tokens) / cap

    def cooling(self):
        return self.cooldown_until > time.time()


@dataclass
class Codex:
    cooldown_until: float = 0.0
    day_tasks: int = 0
    day: str = field(default_factory=lambda: date.today().isoformat())
    running: int = 0

    def cooling(self):
        return self.cooldown_until > time.time()


@dataclass
class Executor:
    """One routable model in the [[executors]] table. Config fields first, runtime state last."""
    id: str
    provider: str
    model: str
    roles: list
    complexity_min: int = 1
    complexity_max: int = 10
    max_parallel: int = 1
    daily_budget_tasks: int = 0
    quota_group: str = ""
    weight: float = 1.0
    enabled: bool = True
    cooldown_until: float = 0.0
    day_tasks: int = 0
    running: int = 0
    day: str = ""
    hold_reason: str = ""

    def cooling(self):
        return self.cooldown_until > time.time()

    def roll_day(self):
        today = date.today().isoformat()
        if self.day != today:
            self.day_tasks, self.day = 0, today


EXEC_FIELDS = {f.name for f in fields(Executor)}
EXEC_STATE_FIELDS = {"cooldown_until", "day_tasks", "running", "day", "hold_reason"}
LEGACY_EXECUTOR_ID = "astra"


class Pool:
    def __init__(self, cfg=None):
        self.cfg = cfg or config()
        self.cap = self.cfg.get("window_cap_tokens", 2_000_000)
        self.accounts = [Account(a["id"], a["config_dir"], a["role_affinity"], a.get("reserve_for_planner", 0.0),
                                 a.get("daily_budget_tokens", 0), a.get("oauth_token_env", ""))
                         for a in self.cfg["claude_accounts"]]
        self.codex = Codex()
        self.executors = self._read_executors()
        self._load()
        self._sync_legacy_codex()

    def _read_executors(self):
        """[[executors]] rows -> {id: Executor}. No table (old config) -> one row synthesized from [codex]."""
        rows = self.cfg.get("executors")
        if not rows:
            c = self.cfg.get("codex", {})
            rows = [{"id": LEGACY_EXECUTOR_ID, "provider": "codex", "model": c.get("model", "gpt-6-astra"),
                     "roles": ["execute"], "max_parallel": c.get("max_parallel", 1),
                     "daily_budget_tasks": c.get("daily_budget_tasks", 0), "quota_group": "chatgpt"}]
        out = {}
        for r in rows:
            ex = Executor(**{k: v for k, v in r.items() if k in EXEC_FIELDS})
            out[ex.id] = ex
        return out

    # persistence -------------------------------------------------------------------------------
    def _load(self):
        if not PERSIST.exists():
            return
        st = json.loads(PERSIST.read_text())
        for a in self.accounts:
            a.__dict__.update({k: v for k, v in st.get("accounts", {}).get(a.id, {}).items()
                               if k not in {"id", "config_dir", "affinity", "reserve", "daily_budget", "oauth_token_env"}})
        self.codex.__dict__.update(st.get("codex", {}))
        for eid, ex in self.executors.items():
            ex.__dict__.update({k: v for k, v in st.get("executors", {}).get(eid, {}).items() if k in EXEC_STATE_FIELDS})

    def save(self):
        PERSIST.write_text(json.dumps({"accounts": {a.id: asdict(a) for a in self.accounts}, "codex": asdict(self.codex),
                                       "executors": {eid: {k: getattr(ex, k) for k in sorted(EXEC_STATE_FIELDS)}
                                                     for eid, ex in self.executors.items()}}, indent=1))

    # selection ---------------------------------------------------------------------------------
    def pick(self, role, avoid=None):
        """least-loaded-with-headroom. avoid: for role="review", the account id that executed the task under
        review; skipped so a fallback execution doesn't get reviewed on the same account, unless it's the only
        one with headroom. None -> caller must HOLD the task, never fail it."""
        ok = []
        for a in self.accounts:
            if role not in a.affinity or a.cooling():
                continue
            u = a.utilization(self.cap)
            if a.daily_budget and (a.day_tokens + a.planner_day_tokens) >= a.daily_budget:
                continue
            ceiling = 1.0 if role == "planner" else 1.0 - a.reserve
            if u < ceiling:
                ok.append(a)
        if role == "review" and avoid:
            without_avoid = [a for a in ok if a.id != avoid]
            if without_avoid:
                ok = without_avoid
        return min(ok, key=lambda a: a.utilization(self.cap)) if ok else None

    def record(self, acct, tokens):
        acct.utilization(self.cap)
        acct.window_tokens += tokens; acct.day_tokens += tokens; self.save()

    def cooldown(self, acct, seconds, reason="rate_limit"):
        acct.cooldown_until = time.time() + seconds; acct.hold_reason = reason; self.save()

    def resume(self, acct_id):
        a = self.get(acct_id); a.cooldown_until = 0; a.hold_reason = ""; self.save()

    def get(self, acct_id):
        return next(a for a in self.accounts if a.id == acct_id)

    # planner usage --------------------------------------------------------------------------
    def tally_planner(self, now=None):
        """The Planner session (interactive `claude`, not a worker spawned by run_claude) never posts a JSON
        result, so its token usage is otherwise invisible to the pool. Read straight from Claude Code's own
        transcript files for this project under each account's config_dir instead: assistant turns in the
        current day and 5h window, summed the same way run_claude sums a worker's usage. Incremental: each file's
        byte offset persists in pool_state so a tick only reads what a prior tick had not yet seen."""
        now = now if now is not None else time.time()
        today = datetime.fromtimestamp(now).astimezone().date().isoformat()
        for a in self.accounts:
            proj_dir = Path(os.path.expanduser(a.config_dir)) / "projects" / encode_project_dir(str(ROOT.resolve()))
            if not proj_dir.exists():
                print(f"planner transcripts not found for {a.id} at {proj_dir}", file=sys.stderr)
                continue
            a.utilization(self.cap)  # roll window/day (and the planner counters with it) before filtering
            window_lo, window_hi = a.window_started, a.window_started + WINDOW_S
            seen = set()
            for f in sorted(proj_dir.glob("*.jsonl")):
                seen.add(f.name)
                size = f.stat().st_size
                off = a.planner_offsets.get(f.name, 0)
                if off > size:
                    off = 0  # shrunk file: start over
                if off >= size:
                    continue
                pos = off
                with f.open("rb") as fh:
                    fh.seek(off)
                    for raw in fh:
                        if not raw.endswith(b"\n"):
                            break  # partial line still being written; pick it up on a later tick
                        pos += len(raw)
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("type") != "assistant":
                            continue
                        try:
                            dt = datetime.fromisoformat((rec.get("timestamp") or "").replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if dt.astimezone().date().isoformat() != today or not (window_lo <= dt.timestamp() < window_hi):
                            continue
                        usage = ((rec.get("message") or {}).get("usage")) or {}
                        n = usage.get("input_tokens", 0) + usage.get("output_tokens", 0) + usage.get("cache_read_input_tokens", 0) // 10
                        a.planner_window_tokens += n
                        a.planner_day_tokens += n
                a.planner_offsets[f.name] = pos
            for name in list(a.planner_offsets):
                if name not in seen:
                    del a.planner_offsets[name]
        self.save()

    # executors ---------------------------------------------------------------------------------
    def pick_executor(self, role, complexity, scores=None):
        """Highest weight x score wins; ties to the fewest tasks today, then id. None -> caller holds the task."""
        scores = scores or {}
        ok = []
        for ex in self.executors.values():
            if not ex.enabled or role not in ex.roles or ex.cooling():
                continue
            if not ex.complexity_min <= complexity <= ex.complexity_max:
                continue
            ex.roll_day()
            if ex.running >= ex.max_parallel:
                continue
            if ex.daily_budget_tasks and ex.day_tasks >= ex.daily_budget_tasks:
                continue
            ok.append(ex)
        if not ok:
            return None
        return sorted(ok, key=lambda e: (-(e.weight * scores.get(e.id, 1.0)), e.day_tasks, e.id))[0]

    def cooldown_executor(self, ex_id, secs, reason=""):
        """A usage limit is charged to the quota group, not the model: cool every enabled member of it."""
        ex = self.executors[ex_id]
        self._cool_group(ex, time.time() + secs, reason)
        self.save()
        return ex

    def _cool_group(self, ex, until, reason=""):
        for other in self.executors.values():
            if other is ex or (other.enabled and other.quota_group and other.quota_group == ex.quota_group):
                if until > other.cooldown_until:
                    other.cooldown_until, other.hold_reason = until, reason

    def _legacy_executor(self):
        """The row executor.py's [codex] model resolves to; the synthesized row when there is no table."""
        want = self.cfg.get("codex", {}).get("model")
        return next((e for e in self.executors.values() if e.provider == "codex" and e.model == want),
                    self.executors.get(LEGACY_EXECUTOR_ID))

    def _sync_legacy_codex(self):
        """Bridge until executor.py routes through executors (B2): it still writes self.codex, so fold that
        state into the row it belongs to. running is mirrored, not maxed, because executor.py builds a fresh
        Pool() per call and its decrements must be able to bring the row back down. day_tasks stays a max
        (monotonic within a day) and only applies when the legacy day matches today. This whole method goes
        away once B2 routes executor.py through the executors table directly."""
        ex = self._legacy_executor()
        if ex is None:
            return
        if self.codex.cooldown_until > ex.cooldown_until:
            self._cool_group(ex, self.codex.cooldown_until, "codex usage limit")
        ex.running = self.codex.running
        if self.codex.day == date.today().isoformat():
            ex.roll_day(); ex.day_tasks = max(ex.day_tasks, self.codex.day_tasks)

    def codex_available(self, complexity: int = 1) -> bool:
        """True iff some enabled executor can take an "execute" task at this complexity right now. Default
        complexity=1 keeps pre-B2 callers (mcp.status, cli, executor.start) working; B2 must pass the task's
        real complexity so a busy/exhausted high-complexity executor doesn't get masked by idle low-band ones."""
        c = self.codex
        if c.day != date.today().isoformat():
            c.day_tasks, c.day = 0, date.today().isoformat()
        self._sync_legacy_codex()
        ex = self.pick_executor("execute", complexity)
        return bool(ex and ex.provider == "codex")

    def both_cooling_minutes(self):
        """Minutes both Claude accounts have been simultaneously cooling; 0 if not."""
        if not all(a.cooling() for a in self.accounts):
            return 0
        return max(0, min(a.cooldown_until for a in self.accounts) - time.time()) / 60

    def status(self):
        avail = self.codex_available()
        legacy = self._legacy_executor() or Executor(LEGACY_EXECUTOR_ID, "codex", "", ["execute"])
        return {"accounts": [{"id": a.id, "utilization": round(a.utilization(self.cap), 3), "day_tokens": a.day_tokens,
                              "planner_day_tokens": a.planner_day_tokens,
                              "daily_budget": a.daily_budget, "cooling_s": max(0, int(a.cooldown_until - time.time())),
                              "reason": a.hold_reason} for a in self.accounts],
                "executors": [{"id": e.id, "model": e.model, "enabled": e.enabled,
                               "cooling_s": max(0, int(e.cooldown_until - time.time())), "running": e.running,
                               "day_tasks": e.day_tasks} for e in self.executors.values()],
                "codex": {"available": avail, "running": legacy.running, "day_tasks": legacy.day_tasks,
                          "cooling_s": max(0, int(legacy.cooldown_until - time.time())),
                          "on_exhausted": self.cfg["codex"]["on_exhausted"]}}


RATE_LIMIT = re.compile(r"rate.?limit|usage.?limit|hit your limit|limit reached|out of usage credits|too many requests|429", re.I)


def is_rate_limited(text):
    return bool(RATE_LIMIT.search(text or ""))


def parse_reset_hint(text, default=1800):
    """Best-effort. Observed: Codex 'try again at Sep 19th, 2026 2:00 PM' (local time). Also 'resets in 2h 15m',
    'try again in 30 minutes', 'retry-after: 900'. Else default."""
    t = text or ""
    m = re.search(r"(?:at|until)\s+([A-Z][a-z]{2,8} \d{1,2})(?:st|nd|rd|th)?,? (\d{4})[, ]+(\d{1,2}:\d{2}\s*[AP]M)", t)
    if m:
        from datetime import datetime
        for fmt in ("%b %d %Y %I:%M %p", "%B %d %Y %I:%M %p"):
            try:
                when = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3).upper().replace(' ', '')}", fmt.replace(" %p", "%p"))
                return max(60, int((when - datetime.now()).total_seconds()))
            except ValueError:
                pass
    m = re.search(r"(?:reset|try again|retry)[^\d]{0,30}(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?", t, re.I)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    m = re.search(r"retry[- ]after[:\s]+(\d+)", t, re.I)
    return int(m.group(1)) if m else default


def fallback_tier(complexity):
    """Executor exhausted + on_exhausted=fallback_claude: sonnet <=5, opus 6-8, hold >=9 (§4.10)."""
    return "sonnet" if complexity <= 5 else "opus" if complexity <= 8 else None
