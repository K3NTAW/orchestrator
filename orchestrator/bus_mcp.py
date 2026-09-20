"""MCP server `bus`: the only way workers report back. Results are <=~1,500 tokens of JSON."""
from mcp.server.mcpserver import MCPServer
from . import bus

srv = MCPServer("bus")


@srv.tool()
def bus_create_task(title: str, spec: str, acceptance: list[str], scope: list[str], role: str = "scout",
                    tier: str = "sonnet", complexity: int = 3, parent: str | None = None, inputs: list | None = None,
                    depends_on: list[str] | None = None, constraints: dict | None = None) -> dict:
    """Planner only. Create a task; rejected without acceptance criteria and scope paths."""
    return bus.create_task(title, spec, acceptance, scope, role, tier, complexity, parent, inputs, constraints, depends_on)


@srv.tool()
def bus_claim(task_id: str, assigned_to: str, worktree: str | None = None) -> dict:
    """Mark a task running for a worker (e.g. 'claude:B', 'codex')."""
    return bus.claim(task_id, assigned_to, worktree)


@srv.tool()
def bus_post_result(task_id: str, result: dict, status: str = "done") -> dict:
    """Post a structured result (summary, findings, artifacts, confidence, provenance). status: done|failed|held. Keep it under ~1,500 tokens."""
    return bus.post_result(task_id, result, status)


@srv.tool()
def bus_read(task_id: str | None = None, status: str | None = None, status_not: str | None = None,
             role: str | None = None, compact: bool = True, full: bool = False) -> list | dict:
    """Read one task, or filter tasks by status / status_not / role.
    task_id always returns that one task in full (spec, acceptance, scope, events, result -- one task is small
    enough). A filtered read defaults to compact rows -- {id, parent, role, status, complexity, tier, title
    (first 90 chars), depends_on, hold_reason, reason (first 120 chars), merged_into, assigned_to, has_result,
    result_summary (first 160 chars of result.summary)} -- so scanning many tasks doesn't pull every spec and
    event into context. Pass full=True (or compact=False) for the old shape, then bus_read(task_id=...) on the
    ones you need to inspect closely."""
    if task_id:
        return bus.read(task_id)
    return bus.read(status=status, status_not=status_not, role=role, compact=compact and not full)


@srv.tool()
def bus_events(since: int = 0, limit: int = 200, role: str | None = None,
               task_ids: list[str] | None = None) -> dict:
    """Events after `since`, optionally by role or task id, with cursor metadata."""
    raw = bus.events(since, limit)
    events = bus.events(since, limit, role=role, task_ids=task_ids)
    # The cursor advances past the last sequence examined, even when role/task
    # filters omit events, so callers never reread the unfiltered stream.
    next_since = raw[-1]["seq"] if raw else since
    return {"events": events, "next_since": next_since, "truncated": len(raw) == limit}


if __name__ == "__main__":
    srv.run()
