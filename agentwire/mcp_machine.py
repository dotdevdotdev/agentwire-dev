"""MCP tools — machine domain."""

from .mcp_core import (
    format_machines,
    mcp,
    run_agentwire_cmd,
)


@mcp.tool()
def machines_list() -> str:
    """List all configured remote machines.

    Returns:
        List of machines with their connection details and status.
    """
    data = run_agentwire_cmd(["machine", "list"])
    if not data.get("success"):
        return f"Failed to list machines: {data.get('error', 'Unknown error')}"
    return format_machines(data)


@mcp.tool()
def machine_add(machine_id: str, host: str, user: str, port: int = 22) -> str:
    """Add a new remote machine.

    Args:
        machine_id: Unique identifier for the machine
        host: Hostname or IP address
        user: SSH username
        port: SSH port (default: 22)

    Returns:
        Success message or error description.
    """
    args = ["machine", "add", machine_id, "--host", host, "--user", user]
    if port != 22:
        args.extend(["--port", str(port)])

    # machine add doesn't support --json
    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return f"Machine '{machine_id}' added successfully."
    return f"Failed to add machine: {data.get('error', 'Unknown error')}"


@mcp.tool()
def machine_remove(machine_id: str) -> str:
    """Remove a remote machine.

    Args:
        machine_id: Machine identifier to remove

    Returns:
        Success message or error description.
    """
    args = ["machine", "remove", machine_id]
    # machine remove doesn't support --json
    data = run_agentwire_cmd(args, json_output=False)
    if data.get("success"):
        return f"Machine '{machine_id}' removed."
    return f"Failed to remove machine: {data.get('error', 'Unknown error')}"
