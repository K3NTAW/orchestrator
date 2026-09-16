"""Account pool: per-account 5h window / daily budget / cooldown, least-loaded-with-headroom selection. State persists to
pool_state.json so MCP server restarts don't forget cooldowns."""
import json, os, re, time, tomllib
from dataclasses import dataclass, field, asdict
from datetime import date
from . import STATE

WINDOW_S = 5 * 3600
CFG = STATE / "pool.toml"
PERSIST = STATE / "pool_state.json"


def config():
    return tomllib.loads(CFG.read_text())


@dataclass
class Account:
    id: str
    config_dir: str
    affinity: list
    reserve: float = 0.0
    daily_budget: int = 0
    cooldown_until: float = 0.0
    window_tokens: int = 0
    window_started: float = field(default_factory=time.time)
    day_tokens: int = 0
    day: str = field(default_factory=lambda: date.today().isoformat())
    hold_reason: str = ""

    def utilization(self, cap):
        if time.time() - self.window_started > WINDOW_S:
            self.window_tokens, self.window_started = 0, time.time()
        if self.day != date.today().isoformat():
            self.day_tokens, self.day = 0, date.today().isoformat()
        return self.window_tokens / cap

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


class Pool:
    def __init__(self, cfg=None):
        self.cfg = cfg or config()
        self.cap = self.cfg.get("window_cap_tokens", 2_000_000)
        self.accounts = [Account(a["id"], a["config_dir"], a["role_affinity"], a.get("reserve_for_planner", 0.0),
                                 a.get("daily_budget_tokens", 0)) for a in self.cfg["claude_accounts"]]
        self.codex = Codex()
        self._load()

    # persistence -------------------------------------------------------------------------------
    def _load(self):
        if not PERSIST.exists():
            return
        st = json.loads(PERSIST.read_text())
        for a in self.accounts:
            a.__dict__.update({k: v for k, v in st.get("accounts", {}).get(a.id, {}).items()
                               if k not in {"id", "config_dir", "affinity", "reserve", "daily_budget"}})
        self.codex.__dict__.update(st.get("codex", {}))

    def save(self):
        PERSIST.write_text(json.dumps({"accounts": {a.id: asdict(a) for a in self.accounts}, "codex": asdict(self.codex)}, indent=1))

    # selection ---------------------------------------------------------------------------------
    def pick(self, role):
        """least-loaded-with-headroom. None -> caller must HOLD the task, never fail it."""
        ok = []
        for a in self.accounts:
            if role not in a.affinity or a.cooling():
                continue
            u = a.utilization(self.cap)
            if a.daily_budget and a.day_tokens >= a.daily_budget:
                continue
            ceiling = 1.0 if role == "planner" else 1.0 - a.reserve
            if u < ceiling:
                ok.append(a)
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

    def codex_available(self):
        c, cfg = self.codex, self.cfg["codex"]
        if c.day != date.today().isoformat():
            c.day_tasks, c.day = 0, date.today().isoformat()
        return not c.cooling() and c.running < cfg["max_parallel"] and c.day_tasks < cfg["daily_budget_tasks"]

    def both_cooling_minutes(self):
        """Minutes both Claude accounts have been simultaneously cooling; 0 if not."""
        if not all(a.cooling() for a in self.accounts):
            return 0
        return max(0, min(a.cooldown_until for a in self.accounts) - time.time()) / 60

    def status(self):
        return {"accounts": [{"id": a.id, "utilization": round(a.utilization(self.cap), 3), "day_tokens": a.day_tokens,
                              "daily_budget": a.daily_budget, "cooling_s": max(0, int(a.cooldown_until - time.time())),
                              "reason": a.hold_reason} for a in self.accounts],
                "codex": {"available": self.codex_available(), "running": self.codex.running, "day_tasks": self.codex.day_tasks,
                          "cooling_s": max(0, int(self.codex.cooldown_until - time.time())),
                          "on_exhausted": self.cfg["codex"]["on_exhausted"]}}


RATE_LIMIT = re.compile(r"rate.?limit|usage.?limit|hit your limit|limit reached|too many requests|429", re.I)


def is_rate_limited(text):
    return bool(RATE_LIMIT.search(text or ""))


def parse_reset_hint(text, default=1800):
    """Best-effort: 'resets in 2h 15m', 'try again in 30 minutes', 'retry after 900'. Else default. Calibrate against real error text (§13)."""
    t = text or ""
    m = re.search(r"(?:reset|try again|retry)[^\d]{0,30}(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?", t, re.I)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    m = re.search(r"retry[- ]after[:\s]+(\d+)", t, re.I)
    return int(m.group(1)) if m else default


def fallback_tier(complexity):
    """Executor exhausted + on_exhausted=fallback_claude: sonnet <=5, opus 6-8, hold >=9 (§4.10)."""
    return "sonnet" if complexity <= 5 else "opus" if complexity <= 8 else None
