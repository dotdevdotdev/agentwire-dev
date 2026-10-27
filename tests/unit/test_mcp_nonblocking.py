"""MCP tools must not block the event loop (#1089)."""

import asyncio
import threading
from unittest.mock import patch


from agentwire import core, mcp_server  # noqa: F401  (registers every tool)
from agentwire.mcp_core import mcp


def test_concurrent_tool_call_not_blocked_by_slow_one():
    """A fast tool completes while another is still blocked in its subprocess."""
    release = threading.Event()
    # Failsafe so a regression fails instead of hanging the suite: on the old
    # inline-sync path the blocked tool freezes the loop and nothing else runs.
    failsafe = threading.Timer(10, release.set)
    failsafe.start()

    def slow(*_a, **_k):
        release.wait()
        return {"success": True, "sessions": []}

    async def scenario():
        with (
            patch("agentwire.mcp_session.run_agentwire_cmd", side_effect=slow),
            patch(
                "agentwire.mcp_projects.run_agentwire_cmd",
                return_value={"success": True, "projects": []},
            ),
        ):
            blocked = asyncio.ensure_future(mcp.call_tool("sessions_list", {}))
            await asyncio.sleep(0)
            fast_done_while_blocked = False
            try:
                await asyncio.wait_for(mcp.call_tool("projects_list", {}), timeout=5)
                fast_done_while_blocked = not release.is_set() and not blocked.done()
            finally:
                release.set()
                await blocked
            return fast_done_while_blocked

    try:
        assert asyncio.run(scenario()), "fast tool did not complete while the slow one was blocked"
    finally:
        failsafe.cancel()
        release.set()
