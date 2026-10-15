"""MCP tools must not block the event loop (#1089)."""

import asyncio
import inspect
import os
import subprocess
import sys
import textwrap
import threading
from unittest.mock import patch

import pytest

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
        with patch("agentwire.mcp_session.run_agentwire_cmd", side_effect=slow), \
             patch("agentwire.mcp_projects.run_agentwire_cmd",
                   return_value={"success": True, "projects": []}):
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


def test_registered_schemas_match_the_unwrapped_functions():
    """Threading wrapper leaves name, description and schemas untouched, and
    no sync tool is left registered inline."""
    from mcp.server.fastmcp.tools.base import Tool

    bad = []
    for name, tool in mcp._tool_manager._tools.items():
        if not tool.is_async:
            bad.append(f"{name}: still sync")
            continue
        original = getattr(tool.fn, "__wrapped__", None)
        if original is None:
            continue  # natively async tool, registered as-is
        ref = Tool.from_function(original)
        got = (tool.name, tool.parameters, tool.output_schema)
        want = (ref.name, ref.parameters, ref.output_schema)
        if got != want:
            bad.append(f"{name}: schema drifted")
        # Description is compared via the docstring: a tool may pass an explicit
        # ``description=`` override (e.g. ``say``), which a bare reference
        # build can't know about.
        if tool.fn.__doc__ != original.__doc__:
            bad.append(f"{name}: docstring drifted")
        if inspect.signature(tool.fn) != inspect.signature(original):
            bad.append(f"{name}: signature drifted")
    assert len(mcp._tool_manager._tools) > 50
    assert not bad, bad


def test_timeout_result_shape_unchanged():
    with patch.object(core, "_run_bounded",
                      side_effect=subprocess.TimeoutExpired(["agentwire"], 1)):
        assert core.run_agentwire_cmd(["x"]) == {"success": False, "error": "Command timed out"}


@pytest.mark.slow
@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_timeout_kills_grandchild_holding_the_pipe(tmp_path):
    """A grandchild inheriting stdout must not hang the caller past the timeout."""
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "parent.py"
    script.write_text(textwrap.dedent(f"""
        import os, subprocess, sys
        r, w = os.pipe()  # never written to; the grandchild keeps `w` open too,
        # so it only ever ends by being killed, not by its parent dying
        gc = subprocess.Popen([sys.executable, "-c", "import os; os.read({{}}, 1)".format(r)],
                              pass_fds=[r, w])
        open({str(pidfile)!r}, "w").write(str(gc.pid))
        os.read(r, 1)
    """))

    outcome = {}

    def run():
        try:
            core._run_bounded([sys.executable, str(script)], timeout=3)
        except subprocess.TimeoutExpired:
            outcome["timed_out"] = True

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(30)
    gc_pid = int(pidfile.read_text()) if pidfile.exists() else None
    try:
        assert not t.is_alive(), "caller hung past the timeout (grandchild held the pipe)"
        assert outcome.get("timed_out")
        assert gc_pid is not None
        for _ in range(100):
            try:
                os.kill(gc_pid, 0)
            except ProcessLookupError:
                break
            threading.Event().wait(0.05)
        else:
            pytest.fail("grandchild survived the group kill")
    finally:
        if gc_pid:
            try:
                os.kill(gc_pid, 9)
            except ProcessLookupError:
                pass
