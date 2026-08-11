"""MCP tools — projects domain."""

from .core import run_agentwire_cmd
from .mcp_core import (
    format_projects,
    format_roles,
    mcp,
)


@mcp.tool()
def projects_list() -> str:
    """Discover available projects.

    Scans the configured projects directory for projects that can
    be used to create new sessions.

    Returns:
        List of projects with their paths and configuration status.
    """
    data = run_agentwire_cmd(["projects", "list"])
    if not data.get("success"):
        return f"Failed to list projects: {data.get('error', 'Unknown error')}"
    return format_projects(data)


@mcp.tool()
def roles_list() -> str:
    """List available roles.

    Roles define agent behavior and capabilities. They can be applied
    when creating sessions or spawning workers.

    Returns:
        List of roles with their descriptions.
    """
    data = run_agentwire_cmd(["roles", "list"])
    if not data.get("success"):
        return f"Failed to list roles: {data.get('error', 'Unknown error')}"
    return format_roles(data)


@mcp.tool()
def role_show(name: str) -> str:
    """Get detailed information about a role.

    Args:
        name: Role name to look up

    Returns:
        Role details including description, tools, and instructions.
    """
    data = run_agentwire_cmd(["roles", "show", name])
    if not data.get("success"):
        return f"Failed to show role: {data.get('error', 'Unknown error')}"

    lines = [f"Role: {name}"]
    if desc := data.get("description"):
        lines.append(f"  Description: {desc}")
    if tools := data.get("tools"):
        lines.append(f"  Tools: {', '.join(tools)}")
    if model := data.get("model"):
        lines.append(f"  Model: {model}")
    if instructions := data.get("instructions"):
        # Truncate long instructions
        preview = instructions[:200] + "..." if len(instructions) > 200 else instructions
        lines.append(f"  Instructions: {preview}")

    return "\n".join(lines)
