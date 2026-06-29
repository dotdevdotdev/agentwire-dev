"""MCP tools — tunnels domain."""

from .mcp_core import (
    mcp,
    run_agentwire_cmd,
)


@mcp.tool()
def tunnels_up() -> str:
    """Create all required SSH tunnels for remote services.

    Reads tunnel requirements from config and creates SSH tunnels
    to reach remote services (TTS, portal, etc.).

    Returns:
        Status of tunnel creation.
    """
    data = run_agentwire_cmd(["tunnels", "up"], json_output=False, timeout=60)
    if data.get("success"):
        return data.get("output", "Tunnels created.")
    return f"Failed to create tunnels: {data.get('error', 'Unknown error')}"


@mcp.tool()
def tunnels_down() -> str:
    """Tear down all SSH tunnels.

    Returns:
        Success message or error description.
    """
    data = run_agentwire_cmd(["tunnels", "down"], json_output=False)
    if data.get("success"):
        return data.get("output", "Tunnels torn down.")
    return f"Failed to tear down tunnels: {data.get('error', 'Unknown error')}"


@mcp.tool()
def tunnels_status() -> str:
    """Show SSH tunnel health.

    Returns:
        Status of all configured tunnels.
    """
    data = run_agentwire_cmd(["tunnels", "status"], json_output=False)
    if data.get("success"):
        return data.get("output", "No tunnels configured.")
    return f"Failed to check tunnel status: {data.get('error', 'Unknown error')}"
