"""Account pool: per-account 5h window / daily budget / cooldown, least-loaded-with-headroom selection. State persists to
pool_state.json so MCP server restarts don't forget cooldowns."""
import fcntl, json, os, re, statistics, sys, time, tomllib
from dataclasses import dataclass, field, asdict, fields
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from . import ROOT, STATE, bus

WINDOW_S = 5 * 3600
TZ = ZoneInfo("Europe/Zurich")
CFG = STATE / "pool.toml"
PERSIST = STATE / "pool_state.json"
PLANNER_USAGE = STATE / "planner_usage.json"
PLANNER_ACCOUNT_FIELDS = {"planner_window_tokens", "planner_day_tokens", "planner_offsets"}
_WARNED_MISSING_DIRS = set()


def encode_project_dir(path: str) -> str:
    """Reproduces Claude Code's projects/ directory naming: every character that is not [A-Za-z0-9] becomes '-'
    (so '/' and '.' both map to '-'; '/Users/k3ntaw/.claude-mem' -> '-Users-k3ntaw--claude-mem'). Verified against
    live ~/.claude/projects/ and ~/.claude-a/projects/ directory names on 2026-09-18."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def config():
    return tomllib.loads(CFG.read_text())


def _load_planner_usage():
    try:
        return json.loads(PLANNER_USAGE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_planner_account(acct_id, window_tokens, day_tokens, offsets, window_started=None, day=None):
    """Read-modify-write .orchestrator/planner_usage.json under an flock on the file itself, so a concurrent
    tally (another account's loop iteration in this process, or another process entirely) can't clobber this
    account's entry. Pool.save() never touches this file; only tally_planner writes it. window_started_at/day
    anchor the counters to the window/day they were accumulated in, so a reader doesn't have to guess; they
    default to now when omitted (tally_planner always passes its own now-derived values explicitly)."""
    if window_started is None or day is None:
        now = time.time()
        window_started = window_started if window_started is not None else now
        day = day if day is not None else datetime.fromtimestamp(now, tz=TZ).date().isoformat()
    PLANNER_USAGE.parent.mkdir(parents=True, exist_ok=True)
    with open(PLANNER_USAGE, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}
            data[acct_id] = {"window_tokens": window_tokens, "day_tokens": day_tokens, "offsets": offsets,
                              "window_started_at": datetime.fromtimestamp(window_started, tz=TZ).isoformat(),
                              "day": day}
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(data, indent=1))
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


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

    def utilization(self, cap, now=None):
        now = now if now is not None else time.time()
        if now - self.window_started > WINDOW_S:
            self.window_tokens, self.window_started = 0, now
            self.planner_window_tokens = 0
        today = datetime.fromtimestamp(now).astimezone().date().isoformat()
        if self.day != today:
            self.day_tokens, self.day = 0, today
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
EXEC_STATE_FIELDS = {"cooldown_until", "day_tasks", "day", "hold_reason"}
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
        self.reservations = {}
        self.reservation_history = {"tokens": 0, "usd": 0.0, "roles": {}, "goals": {}}
        self.notified_state = {}
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
        if PERSIST.exists():
            st = json.loads(PERSIST.read_text())
            for a in self.accounts:
                a.__dict__.update({k: v for k, v in st.get("accounts", {}).get(a.id, {}).items()
                                   if k not in {"id", "config_dir", "affinity", "reserve", "daily_budget",
                                                "oauth_token_env", *PLANNER_ACCOUNT_FIELDS}})
            self.codex.__dict__.update(st.get("codex", {}))
            self.reservations = st.get("reservations", {})
            self.reservation_history = st.get("reservation_history", self.reservation_history)
            self.notified_state = st.get("notified_state", {})
            for eid, ex in self.executors.items():
                ex.__dict__.update({k: v for k, v in st.get("executors", {}).get(eid, {}).items() if k in EXEC_STATE_FIELDS})
        for ex in self.executors.values():
            ex.running = sum(1 for task in bus.read(status="running", role="execute")
                             if task.get("executor", task.get("tier")) == ex.id)
        pu = _load_planner_usage()
        for a in self.accounts:
            entry = pu.get(a.id, {})
            a.planner_window_tokens = entry.get("window_tokens", 0)
            a.planner_day_tokens = entry.get("day_tokens", 0)
            a.planner_offsets = entry.get("offsets", {})

    def save(self):
        PERSIST.write_text(json.dumps({"accounts": {a.id: {k: v for k, v in asdict(a).items()
                                                            if k not in PLANNER_ACCOUNT_FIELDS}
                                                     for a in self.accounts},
                                       "codex": {k: v for k, v in asdict(self.codex).items() if k != "running"},
                                       "executors": {eid: {k: getattr(ex, k) for k in sorted(EXEC_STATE_FIELDS)}
                                                     for eid, ex in self.executors.items()},
                                       "reservations": self.reservations,
                                       "reservation_history": self.reservation_history,
                                       "notified_state": self.notified_state}, indent=1))

    def _reservation_estimate(self, account_id, role):
        limit = float(self.cfg.get("limits", {}).get("max_budget_usd", {}).get(role, 0))
        rows = []
        for task in bus.read(role=role)[-20:]:
            result = task.get("result") or {}
            usage = result.get("usage") or {}
            tokens = self._usage_tokens(usage)
            if usage:
                usd = float(result.get("total_cost_usd", usage.get("usd", usage.get("total_cost_usd", 0))) or 0)
                rows.append((tokens, usd))
        if len(rows) >= 5:
            return int(statistics.median(row[0] for row in rows)), float(statistics.median(row[1] for row in rows))
        source = next((a for a in self.cfg.get("claude_accounts", []) if a.get("id") == account_id), None)
        if source is None:
            source = next((e for e in self.cfg.get("executors", []) if e.get("id") == account_id), {})
        ratio = (source or {}).get("usd_per_token") or (source or {}).get("usd-per-token")
        if ratio:
            return int(limit / float(ratio)), limit
        return 0, limit

    @staticmethod
    def _usage_tokens(usage):
        if not isinstance(usage, dict):
            return 0
        return int(sum(usage.get(k, 0) or 0 for k in
                       ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens", "cached_input_tokens")))

    def _reservation_file(self, mutate):
        PERSIST.parent.mkdir(parents=True, exist_ok=True)
        with open(PERSIST, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            try:
                state = json.loads(fh.read() or "{}")
            except json.JSONDecodeError:
                state = {}
            result = mutate(state)
            fh.seek(0); fh.truncate(); fh.write(json.dumps(state, indent=1))
            fcntl.flock(fh, fcntl.LOCK_UN)
        self.reservations = state.get("reservations", {})
        self.reservation_history = state.get("reservation_history", self.reservation_history)
        self.notified_state = state.get("notified_state", self.notified_state)
        return result

    def reserve(self, run_key, account_id, role, task):
        """Atomically reserve a run's worst-case budget; repeated calls for the same run are idempotent."""
        if self.cfg.get("limits", {}).get("reservations", True) is False:
            return {"run_key": run_key, "disabled": True}
        task_row = task if isinstance(task, dict) else bus.get(task)
        est_tokens, est_usd = self._reservation_estimate(account_id, role)
        now = time.time()
        lease_s = self.cfg.get("daemon", {}).get("stage_lease_s", 900)
        account = next((a for a in self.accounts if a.id == account_id), None)
        daily = account.daily_budget if account else 0
        day_used = (account.day_tokens + account.planner_day_tokens) if account else 0
        goal = task_row.get("parent") or task_row.get("id")

        def mutate(state):
            reservations = state.setdefault("reservations", {})
            if run_key in reservations:
                return reservations[run_key]
            live_tokens = sum(int(r.get("est_tokens", 0)) for r in reservations.values()
                              if not daily or r.get("account") == account_id)
            if daily and day_used + live_tokens + est_tokens > daily:
                return None
            role_cap = self.cfg.get("limits", {}).get("goal_budget_usd", {}).get(role)
            if role_cap is None and role == "review":
                role_cap = self.cfg.get("review", {}).get("budget_usd")
            history = state.setdefault("reservation_history", {"tokens": 0, "usd": 0.0, "roles": {}, "goals": {}})
            goal_spend = history.get("goals", {}).get(goal, {}).get(role, {}).get("usd", 0)
            goal_reserved = sum(float(r.get("est_usd", 0)) for r in reservations.values()
                                if r.get("goal") == goal and r.get("role") == role)
            if role_cap and goal_spend + goal_reserved + est_usd > role_cap:
                return None
            row = {"account": account_id, "role": role, "task": task_row.get("id"), "goal": goal,
                   "est_tokens": est_tokens, "est_usd": est_usd, "claimed_at": now,
                   "lease_until": now + lease_s}
            reservations[run_key] = row
            return row
        return self._reservation_file(mutate)

    def release(self, run_key, actual_usage=None):
        def mutate(state):
            row = state.setdefault("reservations", {}).pop(run_key, None)
            if row is None:
                return None
            usage = actual_usage or {}
            if isinstance(usage, dict) and isinstance(usage.get("output"), dict):
                usage = {**usage.get("output", {}).get("usage", {}),
                         "usd": usage.get("output", {}).get("total_cost_usd", 0)}
            tokens = self._usage_tokens(usage)
            usd = float(usage.get("usd", usage.get("total_cost_usd", 0)) or 0) if isinstance(usage, dict) else 0
            history = state.setdefault("reservation_history", {"tokens": 0, "usd": 0.0, "roles": {}, "goals": {}})
            history["tokens"] = history.get("tokens", 0) + tokens
            history["usd"] = history.get("usd", 0) + usd
            role = history.setdefault("roles", {}).setdefault(row["role"], {"tokens": 0, "usd": 0.0})
            role["tokens"] += tokens; role["usd"] += usd
            goal = history.setdefault("goals", {}).setdefault(row["goal"], {}).setdefault(row["role"],
                                                                                         {"tokens": 0, "usd": 0.0})
            goal["tokens"] += tokens; goal["usd"] += usd
            return row
        return self._reservation_file(mutate)

    def heartbeat(self, run_key):
        def mutate(state):
            row = state.setdefault("reservations", {}).get(run_key)
            if row:
                row["lease_until"] = time.time() + self.cfg.get("daemon", {}).get("stage_lease_s", 900)
            return row
        return self._reservation_file(mutate)

    def sweep_reservations(self, now=None):
        now = now or time.time()
        dropped = []
        def mutate(state):
            reservations = state.setdefault("reservations", {})
            for key, row in list(reservations.items()):
                if row.get("lease_until", 0) >= now:
                    continue
                try:
                    task = bus.get(row.get("task") or key)
                except KeyError:
                    task = {}
                pid = task.get("pid")
                live = False
                if pid:
                    try:
                        os.kill(pid, 0); live = True
                    except (OSError, TypeError):
                        pass
                if live and task.get("status") == "running":
                    continue
                dropped.append(key); del reservations[key]
            return dropped
        self._reservation_file(mutate)
        return dropped

    def notification_transition(self, key, active):
        """Persist condition state and report whether its boolean changed."""
        def mutate(state):
            notified = state.setdefault("notified_state", {})
            previous = bool(notified.get(key, False))
            notified[key] = bool(active)
            return previous != bool(active)
        return self._reservation_file(mutate)

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
        current day and 5h window, summed the same way run_claude sums a worker's usage. Day and window are
        gated independently per line (a same-day line outside the window still counts toward the day, and vice
        versa, both anchored to the Europe/Zurich calendar day). Incremental: each file's byte offset persists in
        planner_usage.json so a tick only reads what a prior tick had not yet seen; a missing transcripts
        directory warns once per account per process rather than on every tick, but the window/day rollover
        (and its anchors) is still saved for that account before moving on. Only planner_usage.json is written
        here -- pool_state.json is never touched, so a tally tick can't clobber another process's concurrent
        cooldown/budget change with a stale full-pool save."""
        now = now if now is not None else time.time()
        today = datetime.fromtimestamp(now, tz=TZ).date().isoformat()
        for a in self.accounts:
            proj_dir = Path(os.path.expanduser(a.config_dir)) / "projects" / encode_project_dir(str(ROOT.resolve()))
            a.utilization(self.cap, now)  # roll window/day (and the planner counters with it) before filtering
            if not proj_dir.exists():
                key = str(proj_dir)
                if key not in _WARNED_MISSING_DIRS:
                    print(f"planner transcripts not found for {a.id} at {proj_dir}", file=sys.stderr)
                    _WARNED_MISSING_DIRS.add(key)
                _save_planner_account(a.id, a.planner_window_tokens, a.planner_day_tokens, a.planner_offsets,
                                       a.window_started, today)
                continue
            window_lo, window_hi = a.window_started, a.window_started + WINDOW_S
            seen = set()
            for f in sorted(proj_dir.glob("*.jsonl")):
                try:
                    size = f.stat().st_size
                except FileNotFoundError:
                    continue  # vanished between glob and read
                seen.add(f.name)
                off = a.planner_offsets.get(f.name, 0)
                if off > size:
                    off = 0  # shrunk file: start over
                if off >= size:
                    continue
                pos = off
                try:
                    fh = f.open("rb")
                except FileNotFoundError:
                    continue  # vanished between glob and read
                with fh:
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
                        ts = rec.get("timestamp")
                        if not isinstance(ts, str):
                            continue
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        in_day = dt.astimezone(TZ).date().isoformat() == today
                        in_window = window_lo <= dt.timestamp() < window_hi
                        if not in_day and not in_window:
                            continue
                        usage = ((rec.get("message") or {}).get("usage")) or {}
                        n = ((usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
                             + (usage.get("cache_read_input_tokens") or 0) // 10)
                        if in_day:
                            a.planner_day_tokens += n
                        if in_window:
                            a.planner_window_tokens += n
                a.planner_offsets[f.name] = pos
            for name in list(a.planner_offsets):
                if name not in seen:
                    del a.planner_offsets[name]
            _save_planner_account(a.id, a.planner_window_tokens, a.planner_day_tokens, a.planner_offsets,
                                   a.window_started, today)

    # executors ---------------------------------------------------------------------------------
    def pick_executor(self, role, complexity, scores=None, task=None):
        """Choose the cheapest proven-safe executor, otherwise retain the established score ranking."""
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
        if task is not None:
            from . import scorecard
            task_class_name = scorecard.task_class(task)
            floor = self.cfg.get("models", {}).get("success_floor", 0.6)
            measured = []
            for ex in ok:
                cost = scorecard.expected_cost(ex.id, task_class_name)
                if cost is None:
                    continue
                success = scorecard.class_success(ex.id, task_class_name)
                measured.append((ex, cost, success))
            safe = [(ex, cost) for ex, cost, success in measured
                    if success is not None and success >= floor]
            if safe:
                return sorted(safe, key=lambda item: (item[1], item[0].day_tasks, item[0].id))[0][0]
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
        """Bridge legacy Codex cooldown and daily usage into its executor row."""
        ex = self._legacy_executor()
        if ex is None:
            return
        if self.codex.cooldown_until > ex.cooldown_until:
            self._cool_group(ex, self.codex.cooldown_until, "codex usage limit")
        if self.codex.day == date.today().isoformat():
            ex.roll_day(); ex.day_tasks = max(ex.day_tasks, self.codex.day_tasks)

    def codex_available(self, complexity: int = 1, task=None) -> bool:
        """True iff some enabled executor can take an "execute" task at this complexity right now. Default
        complexity=1 keeps pre-B2 callers (mcp.status, cli, executor.start) working; B2 must pass the task's
        real complexity so a busy/exhausted high-complexity executor doesn't get masked by idle low-band ones."""
        c = self.codex
        if c.day != date.today().isoformat():
            c.day_tasks, c.day = 0, date.today().isoformat()
        self._sync_legacy_codex()
        ex = self.pick_executor("execute", complexity, task=task)
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
                               "day_tasks": e.day_tasks,
                               "expected_cost": self._expected_costs(e.id)} for e in self.executors.values()],
                "codex": {"available": avail, "running": legacy.running, "day_tasks": legacy.day_tasks,
                          "cooling_s": max(0, int(legacy.cooldown_until - time.time())),
                          "on_exhausted": self.cfg["codex"]["on_exhausted"]}}

    def _expected_costs(self, executor_id):
        from . import scorecard
        return {name: cost for name in scorecard.TASK_CLASSES
                if (cost := scorecard.expected_cost(executor_id, name)) is not None}


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
