"""Portal routes — scheduler domain (daemon control + board/events).

Part of the #560 server.py split. Handlers moved verbatim from
``AgentWireServer``; they depend on the ``agentwire.scheduler`` module, the
core ``self.run_agentwire_cmd`` / ``self.agent`` helpers, and tmux, all of
which resolve through the MRO of the composed server class.
``_start_scheduler_daemon`` is also called at portal startup (autostart).
"""

import asyncio
import logging

from aiohttp import web

logger = logging.getLogger(__name__)


class SchedulerRoutesMixin:
    async def api_scheduler_live(self, request: web.Request) -> web.Response:
        """GET /api/scheduler/live - Live scheduler state.

        Checks if the scheduler tmux session is actually running.
        Returns 404 with running=false if the daemon isn't active,
        even if a stale state file exists.
        """
        try:
            # Check if scheduler tmux session is alive
            is_running = await self._is_scheduler_running()
            if not is_running:
                return web.json_response({"running": False}, status=404)

            from ..scheduler import read_live_state
            state = read_live_state()
            if state is None:
                return web.json_response({"running": False}, status=404)
            state["running"] = True
            return web.json_response(state)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _is_scheduler_running(self) -> bool:
        """Check if the agentwire-scheduler tmux session exists."""
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", "=agentwire-scheduler",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    async def api_scheduler_events(self, request: web.Request) -> web.Response:
        """GET /api/scheduler/events - Recent scheduler events."""
        try:
            from ..scheduler import read_events
            tail = int(request.query.get("tail", "20"))
            task_filter = request.query.get("task") or None
            events = read_events(tail=tail, task_filter=task_filter)
            return web.json_response({"events": events})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_board(self, request: web.Request) -> web.Response:
        """GET /api/scheduler/board - Scheduler board data."""
        try:
            from ..scheduler import get_board_display, load_board
            board = load_board()
            rows = get_board_display(board)
            return web.json_response({"tasks": rows})
        except (FileNotFoundError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=404)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_task_enable(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/tasks/{name}/enable - Enable a task."""
        name = request.match_info["name"]
        try:
            success, result = await self.run_agentwire_cmd(["scheduler", "enable", name])
            if success:
                return web.json_response({"success": True, "task": name})
            return web.json_response({"error": result.get("error", "Enable failed")}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_task_disable(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/tasks/{name}/disable - Disable a task."""
        name = request.match_info["name"]
        try:
            success, result = await self.run_agentwire_cmd(["scheduler", "disable", name])
            if success:
                return web.json_response({"success": True, "task": name})
            return web.json_response({"error": result.get("error", "Disable failed")}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_task_run(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/tasks/{name}/run - Force-run a task (fire-and-forget)."""
        name = request.match_info["name"]
        try:
            # Fire-and-forget: start the task in background, completion comes via WebSocket
            asyncio.create_task(self.run_agentwire_cmd(["scheduler", "run", name]))
            return web.json_response({"success": True, "task": name, "status": "started"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _start_scheduler_daemon(self) -> bool:
        """Launch the scheduler daemon in a detached tmux session.

        No-op if it's already running. Returns True if it was started.
        """
        if await self._is_scheduler_running():
            return False
        # Create tmux session and launch scheduler serve (same as CLI but detached)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "new-session", "-d", "-s", "agentwire-scheduler",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        proc2 = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", "agentwire-scheduler",
            "agentwire scheduler serve", "Enter",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc2.wait()
        return True

    async def api_scheduler_start(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/start - Start the scheduler daemon in tmux."""
        try:
            started = await self._start_scheduler_daemon()
            return web.json_response(
                {"success": True, "status": "started" if started else "already_running"}
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_stop(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/stop - Stop the scheduler daemon."""
        try:
            if not await self._is_scheduler_running():
                return web.json_response({"success": True, "status": "already_stopped"})
            success, result = await self.run_agentwire_cmd(["scheduler", "stop"], json_output=False)
            if success:
                return web.json_response({"success": True, "status": "stopped"})
            return web.json_response({"error": result.get("error", "Unknown error")}, status=500)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_task_events(self, request: web.Request) -> web.Response:
        """GET /api/scheduler/tasks/{name}/events - Events for a specific task."""
        name = request.match_info["name"]
        try:
            from ..scheduler import read_events
            tail = int(request.query.get("tail", "100"))
            events = read_events(tail=tail, task_filter=name)
            return web.json_response({"events": events})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_session_output(self, request: web.Request) -> web.Response:
        """GET /api/scheduler/output?session=X&lines=30 - Get recent session output."""
        session = request.query.get("session")
        if not session:
            return web.json_response({"error": "session parameter required"}, status=400)
        lines = min(int(request.query.get("lines", "30")), 100)
        try:
            loop = asyncio.get_event_loop()
            output = await loop.run_in_executor(
                None, lambda: self.agent.get_output(session, lines=lines)
            )
            return web.json_response({"session": session, "output": output})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)


def register_scheduler_routes(server, app):
    """Wire the scheduler domain's routes onto ``app``."""
    app.router.add_get("/api/scheduler/live", server.api_scheduler_live)
    app.router.add_get("/api/scheduler/events", server.api_scheduler_events)
    app.router.add_get("/api/scheduler/board", server.api_scheduler_board)
    app.router.add_post("/api/scheduler/tasks/{name}/enable", server.api_scheduler_task_enable)
    app.router.add_post("/api/scheduler/tasks/{name}/disable", server.api_scheduler_task_disable)
    app.router.add_post("/api/scheduler/tasks/{name}/run", server.api_scheduler_task_run)
    app.router.add_get("/api/scheduler/tasks/{name}/events", server.api_scheduler_task_events)
    app.router.add_post("/api/scheduler/start", server.api_scheduler_start)
    app.router.add_post("/api/scheduler/stop", server.api_scheduler_stop)
    app.router.add_get("/api/scheduler/output", server.api_scheduler_session_output)
