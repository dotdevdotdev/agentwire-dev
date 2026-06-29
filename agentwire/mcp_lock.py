"""MCP tools — lock domain."""

from .mcp_core import (
    mcp,
    run_agentwire_cmd,
)


@mcp.tool()
def lock_list() -> str:
    """List all active task locks.

    Returns:
        List of locks with session names and timestamps.
    """
    data = run_agentwire_cmd(["lock", "list"])
    if not data.get("success"):
        return f"Failed to list locks: {data.get('error', 'Unknown error')}"

    locks = data.get("locks", [])
    if not locks:
        return "No active locks."

    lines = ["Active locks:"]
    for lock in locks:
        session = lock.get("session", "unknown")
        acquired = lock.get("acquired", "")
        pid = lock.get("pid", "")
        lines.append(f"  - {session}: acquired {acquired} (pid: {pid})")

    return "\n".join(lines)


@mcp.tool()
def lock_clean() -> str:
    """Remove stale locks (from dead processes).

    Returns:
        Number of stale locks removed or error.
    """
    data = run_agentwire_cmd(["lock", "clean"])
    if data.get("success"):
        removed = data.get("removed", [])
        count = data.get("count", len(removed) if isinstance(removed, list) else removed)
        if isinstance(removed, list) and removed:
            return f"Cleaned {count} stale lock(s): {', '.join(removed)}"
        return f"Cleaned {count} stale lock(s)."
    return f"Failed to clean locks: {data.get('error', 'Unknown error')}"


@mcp.tool()
def lock_remove(session: str) -> str:
    """Force-remove a specific lock.

    Args:
        session: Session name whose lock to remove

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["lock", "remove", session])
    if data.get("success"):
        return f"Lock for '{session}' removed."
    return f"Failed to remove lock: {data.get('error', 'Unknown error')}"
