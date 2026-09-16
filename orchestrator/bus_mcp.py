"""MCP server `bus`: the only way workers report back. Results are <=~1,500 tokens of JSON."""
from mcp.server.mcpserver import MCPServer
from . import bus

srv = MCPServer("bus")


@srv.tool()
def bus_create_task(title: str, spec: str, acceptance: list[str], scope: list[str], role: str = "scout",
                    tier: str = "sonnet", complexity: int = 3, parent: str | None = None, inputs: list | None = None) -> dict:
    """Planner only. Create a task; rejected without acceptance criteria and scope paths."""
    return bus.create_task(title, spec, acceptance, scope, role, tier, complexity, parent, inputs)


@srv.tool()
def bus_claim(task_id: str, assigned_to: str, worktree: str | None = None) -> dict:
    """Mark a task running for a worker (e.g. 'claude:B', 'codex')."""
    return bus.claim(task_id, assigned_to, worktree)


@srv.tool()
def bus_post_result(task_id: str, result: dict, status: str = "done") -> dict:
    """Post a structured result (summary, findings, artifacts, confidence, provenance). status: done|failed|held. Keep it under ~1,500 tokens."""
    return bus.post_result(task_id, result, status)


@srv.tool()
def bus_read(task_id: str | None = None, status: str | None = None, status_not: str | None = None, role: str | None = None) -> list | dict:
    """Read one task, or filter tasks by status / status_not / role."""
    return bus.read(task_id, status, status_not, role)


@srv.tool()
def bus_events(since: int = 0, limit: int = 200) -> list:
    """Events after sequence number `since`. Batch this instead of polling tasks."""
    return bus.events(since, limit)


if __name__ == "__main__":
    srv.run()
