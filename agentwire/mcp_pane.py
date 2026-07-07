"""MCP tools — pane domain."""

from .mcp_core import (
    _delivery_result,
    mcp,
    run_agentwire_cmd,
)


@mcp.tool()
def pane_spawn(
    session: str | None = None,
    roles: str | None = None,
    posture: str | None = None,
) -> str:
    """Spawn a worker pane in a session.

    Workers share the orchestrator's working directory. For isolated commits
    with git worktrees, use CLI: agentwire spawn --branch <name>

    Args:
        session: Session name (defaults to current session if in tmux)
        roles: Comma-separated list of roles for the worker
        posture: Permission mode: bypass | prompted | auto (optional; default bypass)

    Returns:
        Pane index of the spawned worker or error description.
    """
    args = ["spawn"]

    if session:
        args.extend(["-s", session])
    if roles:
        args.extend(["--roles", roles])
    if posture:
        args.extend(["--posture", posture])

    # Spawn can take a while to initialize the agent, use longer timeout
    data = run_agentwire_cmd(args, timeout=120)
    if data.get("success"):
        pane_idx = data.get("pane_index", data.get("pane", "?"))
        return f"Worker pane {pane_idx} spawned successfully."
    return f"Failed to spawn pane: {data.get('error', 'Unknown error')}"


@mcp.tool()
def pane_send(pane: int, message: str, session: str | None = None) -> str:
    """Send a message to a specific pane.

    Args:
        pane: Pane index (0 = orchestrator, 1+ = workers)
        message: The message to send
        session: Session name (defaults to current session if in tmux)

    Returns:
        Success message or error description.
    """
    args = ["send", "--pane", str(pane), "--verify", message]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to send to pane: {data.get('error', 'Unknown error')}"
    return _delivery_result(data, f"to pane {pane}")


@mcp.tool()
def pane_output(pane: int, session: str | None = None, lines: int = 50) -> str:
    """Capture output from a specific pane.

    Args:
        pane: Pane index
        session: Session name (defaults to current session if in tmux)
        lines: Number of lines to capture (default: 50)

    Returns:
        The captured output from the pane.
    """
    args = ["output", "--pane", str(pane), "-n", str(lines)]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return data.get("output", "")
    return f"Failed to capture pane output: {data.get('error', 'Unknown error')}"


@mcp.tool()
def pane_kill(pane: int, session: str | None = None) -> str:
    """Kill a specific pane.

    Args:
        pane: Pane index to kill
        session: Session name (defaults to current session if in tmux)

    Returns:
        Success message or error description.
    """
    args = ["kill", "--pane", str(pane)]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return f"Pane {pane} terminated."
    return f"Failed to kill pane: {data.get('error', 'Unknown error')}"


@mcp.tool()
def panes_list(session: str | None = None) -> str:
    """List panes in a session.

    Args:
        session: Session name (defaults to current session if in tmux)

    Returns:
        List of panes with their indices, commands, and status.
    """
    # Use 'info' command which returns pane information
    args = ["info"]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to list panes: {data.get('error', 'Unknown error')}"

    # Extract panes from info response
    panes = data.get("panes", [])
    session_name = session or data.get("session", "current")

    if not panes:
        return f"No panes found in session '{session_name}'."

    lines = [f"Panes in session '{session_name}':"]
    for p in panes:
        idx = p.get("index", 0)
        cmd = p.get("command", "unknown")
        active = " (active)" if p.get("active") else ""
        role = "orchestrator" if idx == 0 else "worker"
        lines.append(f"  - Pane {idx} [{role}]: {cmd}{active}")

    return "\n".join(lines)


@mcp.tool()
def pane_split(session: str | None = None, count: int = 1) -> str:
    """Add terminal pane(s) to a session with even vertical layout.

    Args:
        session: Session name (defaults to current session if in tmux)
        count: Number of panes to add (default: 1)

    Returns:
        Success message or error description.
    """
    args = ["split", "-n", str(count)]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return f"Added {count} terminal pane(s)."
    return f"Failed to split panes: {data.get('error') or data.get('output') or 'Unknown error'}"


@mcp.tool()
def pane_detach(session: str, pane: int, target: str) -> str:
    """Move a pane to its own session.

    Detaches a pane from its current session and creates a new
    session for it.

    Args:
        session: Source session name
        pane: Pane index to detach
        target: Target session name (created if doesn't exist)

    Returns:
        Success message or error description.
    """
    args = ["detach", "--pane", str(pane), "-s", target, "--source", session]
    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return f"Pane {pane} detached from '{session}' to '{target}'."
    return f"Failed to detach pane: {data.get('error') or data.get('output') or 'Unknown error'}"


@mcp.tool()
def pane_jump(session: str | None = None, pane: int = 0) -> str:
    """Focus a specific pane in tmux.

    Args:
        session: Session name (defaults to current session if in tmux)
        pane: Pane index to focus (default: 0)

    Returns:
        Success message or error description.
    """
    args = ["jump", "--pane", str(pane)]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return f"Focused pane {pane}."
    return f"Failed to focus pane: {data.get('error', 'Unknown error')}"


@mcp.tool()
def pane_resize(session: str | None = None) -> str:
    """Re-fit tmux window to its attached clients per the window-size policy.

    Clears any manual size pin so the configured policy (largest/latest/
    smallest) governs again.

    Args:
        session: Session name (defaults to current session if in tmux)

    Returns:
        Success message or error description.
    """
    args = ["resize"]
    if session:
        args.extend(["-s", session])

    data = run_agentwire_cmd(args)
    if data.get("success"):
        return "Window re-fit to attached clients per window-size policy."
    return f"Failed to resize: {data.get('error', 'Unknown error')}"
