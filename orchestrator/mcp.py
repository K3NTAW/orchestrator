"""MCP server `orchestrator`: scheduler for the Planner. Spawns run in background threads; results land on the bus."""
import threading
from mcp.server.mcpserver import MCPServer
from . import bus, merge as mq, spawn
from .pool import Pool, fallback_tier

srv = MCPServer("orchestrator")


def _bg(task_id):
    threading.Thread(target=spawn.run_worker, args=(task_id,), daemon=True).start()
    return {"task": task_id, "status": "spawned; result arrives on the bus"}


@srv.tool()
def spawn_scout(task_id: str) -> dict:
    """Run a queued scout/triage task as a `claude -p` worker on whichever account has headroom (held if none)."""
    return _bg(task_id)


@srv.tool()
def spawn_review(task_id: str) -> dict:
    """Run a review task. inputs[0] must be the task id whose diff is under review."""
    return _bg(task_id)


@srv.tool()
def spawn_challenge(task_id: str) -> dict:
    """Try to refute a low-confidence finding on the other account. inputs[0] = {claim, evidence, confidence}."""
    return _bg(task_id)


@srv.tool()
def merge(task_id: str, target: str | None = None) -> dict:
    """Serial merge queue: rebase -> tests-green -> fast-forward goal/<parent> (or given target). Never main."""
    return mq.merge(task_id, target)


@srv.tool()
def status() -> dict:
    """Account utilization, cooldowns, budgets, Codex availability, queue depth."""
    p = Pool()
    return {**p.status(), "queue": {s: len(bus.read(status=s)) for s in ("queued", "held", "running")}}


@srv.tool()
def executor_fallback(complexity: int) -> dict:
    """When Codex is exhausted: which Claude tier may execute (per on_exhausted policy), or hold."""
    p = Pool()
    if p.codex_available():
        return {"use": "codex"}
    pol = p.cfg["codex"]["on_exhausted"]
    if pol == "fallback_claude" and (tier := fallback_tier(complexity)):
        return {"use": f"claude:{tier}", "review": "other account, different model; label PR same-family-review"}
    return {"use": "hold", "policy": pol, "hint": "refill pipeline: scouts, specs, reviews, retrospect"}


@srv.tool()
def hold_account(account_id: str, minutes: int = 30, reason: str = "manual") -> dict:
    """Take an account out of rotation."""
    p = Pool(); p.cooldown(p.get(account_id), minutes * 60, reason); return p.status()


@srv.tool()
def resume_account(account_id: str) -> dict:
    """Return an account to rotation."""
    p = Pool(); p.resume(account_id); return p.status()


if __name__ == "__main__":
    srv.run()
