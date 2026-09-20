"""MCP server `orchestrator`: scheduler for the Planner. Spawns run in background threads; results land on the bus."""
import atexit, json, os, sys, tempfile, threading, time
from mcp.server.mcpserver import MCPServer
from . import STATE, bus, daemon, executor, goals, merge as mq, spawn
from .pool import Pool, fallback_tier

srv = MCPServer("orchestrator")


def _planner_session_path():
    return STATE / "planner_session.json"


def register_planner_session():
    """This MCP server lives exactly as long as one interactive Planner session (pool.toml [daemon] autostart
    comment): mark that with ORCH_DAEMON_HOST=mcp for anything spawned as a child of this process, and with
    .orchestrator/planner_session.json for anything (e.g. an autonomous daemon.tick()) that only shares the
    filesystem, not the environment. Written atomically (temp file + os.replace) so a reader never observes a
    half-written file. A reader treats a stale file -- one whose pid fails goals.identity_of -- as absent."""
    os.environ["ORCH_DAEMON_HOST"] = "mcp"
    path = _planner_session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"pid": os.getpid(), "pid_start": goals._proc_start(os.getpid()), "started_at": time.time(), "host": "mcp"}
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".planner_session.json.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))
        os.replace(tmp_name, path)
    except Exception:
        os.unlink(tmp_name)
        raise
    return path


def deregister_planner_session():
    """atexit hook: remove planner_session.json only while it still names this process -- a later MCP server that
    started after this one exited already overwrote it with its own registration, and this process must never
    delete that newer one out from under it."""
    path = _planner_session_path()
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if data.get("pid") == os.getpid():
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _bg(task_id):
    threading.Thread(target=spawn.run_worker, args=(task_id,), daemon=True).start()
    return {"task": task_id, "status": "spawned; result arrives on the bus"}


def _wrong_role(task_id, *allowed):
    """None when task_id's role is one of `allowed`; else an {"error": ...} dict naming the mismatch, so a spawn_*
    tool handed the wrong kind of task id (e.g. spawn_spec_review given an execute id, gotcha 2026-09-18) reports
    it instead of silently running that task under the wrong role."""
    t = bus.get(task_id)
    if t["role"] not in allowed:
        return {"error": f"task {task_id} has role {t['role']}; this tool takes {' or '.join(allowed)}"}
    return None


@srv.tool()
def spawn_scout(task_id: str) -> dict:
    """Run a queued scout/triage task as a `claude -p` worker on whichever account has headroom (held if none)."""
    return _wrong_role(task_id, "scout", "triage") or _bg(task_id)


@srv.tool()
def spawn_review(task_id: str) -> dict:
    """Run a review task. inputs[0] must be the task id whose diff is under review."""
    return _wrong_role(task_id, "review") or _bg(task_id)


@srv.tool()
def spawn_challenge(task_id: str) -> dict:
    """Try to refute a low-confidence finding on the other account. inputs[0] = {claim, evidence, confidence}."""
    return _wrong_role(task_id, "challenge") or _bg(task_id)


@srv.tool()
def spawn_spec_review(task_id: str) -> dict:
    """Review an execute task's spec before any code is written. inputs[0] = the execute task whose spec is under review."""
    return _wrong_role(task_id, "spec_review") or _bg(task_id)


@srv.tool()
def codex(task_id: str, prompt: str) -> dict:
    """Executor: start a fresh GPT-6 Astra thread (`codex exec`) for one atomic execute task in its worktree. Returns thread id + final message; held if Codex is cooling."""
    result = executor.start(task_id, prompt)
    posted, reason = executor.post_tool_result(task_id, result)
    return {**result, "posted": posted, "posted_reason": reason}


@srv.tool()
def codex_reply(task_id: str, delta: str) -> dict:
    """Fix-loop round on the task's existing thread (`codex exec resume`). Send deltas only: failing test names + assertion lines. Max 5 rounds."""
    result = executor.reply(task_id, delta)
    posted, reason = executor.post_tool_result(task_id, result, replace_result=True)
    return {**result, "posted": posted, "posted_reason": reason}


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


def main():
    """Server entrypoint: register this session (ORCH_DAEMON_HOST=mcp + planner_session.json) before the pipeline
    daemon starts, so an autonomous decision the daemon's very first tick might launch still sees a session
    attached; then autostart the daemon (dispatch/gate/review/merge -- a failure here must never take the server
    down, since the Planner still needs `f orch` to come up even if the daemon can't get its lock), then serve.
    Never run at import time: importing this module (e.g. for register_planner_session/deregister_planner_session
    in tests) must have zero side effects -- no env var set, no file written, no daemon thread started."""
    register_planner_session()
    atexit.register(deregister_planner_session)
    try:
        daemon.start_background(Pool().cfg)
    except Exception as e:
        print(f"[daemon] autostart failed: {e}", file=sys.stderr)
    srv.run()


if __name__ == "__main__":
    main()
