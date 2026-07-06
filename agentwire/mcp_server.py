"""AgentWire MCP Server.

Exposes AgentWire capabilities as MCP tools for external agents.
This allows tools like MoltBot, Claude Desktop, etc. to manage
tmux sessions, remote machines, and voice features.

The tool surface is split per-domain across ``mcp_<domain>`` modules (mirrors
the #495 CLI split). Importing each domain module runs its tool
decorators, registering every tool on the shared ``mcp`` instance.

Usage:
    agentwire mcp  # Starts MCP server on stdio
"""

from . import (
    mcp_channels,  # noqa: F401
    mcp_chrome,  # noqa: F401
    mcp_council,  # noqa: F401
    mcp_desktop,  # noqa: F401
    mcp_handoff,  # noqa: F401
    mcp_history,  # noqa: F401
    mcp_listen,  # noqa: F401
    mcp_lock,  # noqa: F401
    mcp_machine,  # noqa: F401
    mcp_msg,  # noqa: F401
    mcp_notify,  # noqa: F401
    mcp_pane,  # noqa: F401
    mcp_projects,  # noqa: F401
    mcp_scheduler,  # noqa: F401
    mcp_scratchpad,  # noqa: F401
    mcp_services,  # noqa: F401
    mcp_session,  # noqa: F401
    mcp_status,  # noqa: F401
    mcp_task,  # noqa: F401
    mcp_tunnels,  # noqa: F401
    mcp_voice,  # noqa: F401
    mcp_wiki,  # noqa: F401
    mcp_worktree,  # noqa: F401
)
from .mcp_core import get_portal_url, logger, mcp


def run_server():
    """Run the MCP server on stdio transport."""
    logger.info("Starting AgentWire MCP server")
    logger.info(f"Portal URL: {get_portal_url()}")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_server()
