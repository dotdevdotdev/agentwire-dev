"""CLI for the MCP server — ``agentwire mcp``.

Exposes AgentWire capabilities as MCP tools for external agents (MoltBot,
Claude Desktop, etc.) over stdio.
"""

from __future__ import annotations


def cmd_mcp(args) -> int:
    """Run the MCP server on stdio.

    Exposes AgentWire capabilities as MCP tools for external agents
    like MoltBot, Claude Desktop, etc.
    """
    from .mcp_server import run_server
    run_server()
    return 0


def register_mcp_parser(subparsers) -> None:
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Run MCP server for external agent integration",
        description="Expose AgentWire as an MCP server for tools like MoltBot, Claude Desktop, etc.",
    )
    mcp_parser.set_defaults(func=cmd_mcp)
