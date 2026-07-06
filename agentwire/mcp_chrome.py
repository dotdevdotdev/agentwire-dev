"""MCP tools — claude-in-chrome tab bookkeeping (#717).

agentwire cannot call `tabs_close_mcp` itself: that MCP server runs inside
the CALLING agent's own client, not agentwire's process. These tools are
pure bookkeeping so a crashed session's orphaned verification tabs can still
be identified and closed by whoever tears the session down — `worktree_remove`
checks this registry during teardown and reports anything still tracked.
"""

from .mcp_core import get_caller_session, mcp, run_agentwire_cmd


@mcp.tool()
def chrome_tab_track(tab_id: str, url: str = "", session: str = "") -> str:
    """Record a claude-in-chrome tab you just opened, so it can be found and
    closed later even if this session dies before you close it yourself.

    Call this right after `tabs_create_mcp` when opening a tab to verify your
    work (dev server, screenshots). The NORMAL path is still to close the tab
    yourself with `tabs_close_mcp` and call `chrome_tab_untrack` before you
    finish — this tracking is the BACKSTOP: `worktree_remove` checks it during
    teardown and reports any tab you never got around to closing.

    Args:
        tab_id: The tab id returned by `tabs_create_mcp`.
        url: The tab's URL (informational, shown in `chrome_tab_list`).
        session: Owning session (default: the calling session).

    Returns:
        Confirmation, or an error if the session couldn't be determined.
    """
    owner = session or get_caller_session()
    if not owner:
        return "Could not determine the calling session — pass session= explicitly."
    args = ["tabs", "track", "--session", owner, "--tab-id", tab_id]
    if url:
        args += ["--url", url]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to track tab: {data.get('error', 'Unknown error')}"
    return f"Tracked tab {tab_id} for '{owner}'."


@mcp.tool()
def chrome_tab_untrack(tab_id: str, session: str = "") -> str:
    """Drop a tracked tab after you've closed it yourself with `tabs_close_mcp`.

    Args:
        tab_id: The tab id you closed.
        session: Owning session (default: the calling session).

    Returns:
        Confirmation, or a note if it wasn't tracked.
    """
    owner = session or get_caller_session()
    if not owner:
        return "Could not determine the calling session — pass session= explicitly."
    data = run_agentwire_cmd(["tabs", "untrack", "--session", owner, "--tab-id", tab_id])
    if not data.get("success"):
        return f"Failed to untrack tab: {data.get('error', 'Unknown error')}"
    if data.get("removed"):
        return f"Untracked tab {tab_id} for '{owner}'."
    return f"Tab {tab_id} was not tracked for '{owner}'."


@mcp.tool()
def chrome_tab_list(session: str = "") -> str:
    """List tracked claude-in-chrome tabs — debugging aid for leaked verification tabs.

    Args:
        session: Limit to one session (default: every session with tracked tabs).

    Returns:
        Formatted list, or a message if none are tracked.
    """
    args = ["tabs", "list"]
    if session:
        args += ["--session", session]
    data = run_agentwire_cmd(args)
    if not data.get("success"):
        return f"Failed to list tracked tabs: {data.get('error', 'Unknown error')}"
    tabs = data.get("tabs") or {}
    lines = []
    for sess, entries in tabs.items():
        for t in entries:
            url_bit = f" ({t.get('url')})" if t.get("url") else ""
            lines.append(f"  {sess}: tab {t.get('tab_id')}{url_bit}")
    if not lines:
        return "No tracked claude-in-chrome tabs."
    return "Tracked tabs:\n" + "\n".join(lines)
