"""MCP tools — history domain."""

from .core import run_agentwire_cmd
from .mcp_core import (
    mcp,
)


@mcp.tool()
def history_list(project: str | None = None, limit: int = 20) -> str:
    """List conversation history for sessions.

    Args:
        project: Filter by project path (optional)
        limit: Maximum number of results (default: 20)

    Returns:
        List of past sessions with IDs and timestamps.
    """
    args = ["history", "list", "-n", str(limit)]
    if project:
        args.extend(["--project", project])

    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to list history: {data.get('error', 'Unknown error')}"

    # CLI returns a JSON array, which run_agentwire_cmd wraps as {"items": [...]}
    sessions = data.get("items", data.get("sessions", []))
    if not sessions:
        return "No session history found."

    lines = ["Session history:"]
    for s in sessions:
        sid = s.get("sessionId", s.get("id", "unknown"))
        first_msg = s.get("firstMessage", "")
        count = s.get("messageCount", 0)
        preview = (first_msg[:60] + "...") if len(first_msg) > 60 else first_msg
        lines.append(f"  - {sid}: {preview} ({count} messages)")

    return "\n".join(lines)


@mcp.tool()
def history_show(session_id: str) -> str:
    """Show details of a past session.

    Args:
        session_id: Session ID from history_list

    Returns:
        Session details including commands and duration.
    """
    data = run_agentwire_cmd(["history", "show", session_id])
    if not data.get("success"):
        return f"Failed to show session: {data.get('error', 'Unknown error')}"

    lines = [f"Session: {data.get('sessionId', session_id)}"]
    if first_msg := data.get("firstMessage"):
        preview = (first_msg[:80] + "...") if len(first_msg) > 80 else first_msg
        lines.append(f"  First message: {preview}")
    if branch := data.get("gitBranch"):
        lines.append(f"  Branch: {branch}")
    if count := data.get("messageCount"):
        lines.append(f"  Messages: {count}")
    if timestamps := data.get("timestamps"):
        if start := timestamps.get("start"):
            from datetime import datetime
            lines.append(f"  Started: {datetime.fromtimestamp(start / 1000).strftime('%Y-%m-%d %H:%M')}")
    if summaries := data.get("summaries"):
        lines.append(f"  Summaries: {len(summaries)}")

    return "\n".join(lines)


@mcp.tool()
def history_resume(session_id: str, project: str) -> str:
    """Resume a past session (always creates a fork).

    Args:
        session_id: Session ID from history_list
        project: Project path for the resumed session

    Returns:
        Success message with new session name or error.
    """
    data = run_agentwire_cmd(
        ["history", "resume", session_id, "--project", project],
        timeout=120,
    )
    if data.get("success"):
        new_session = data.get("session", "unknown")
        return f"Session resumed as '{new_session}'."
    return f"Failed to resume session: {data.get('error', 'Unknown error')}"
