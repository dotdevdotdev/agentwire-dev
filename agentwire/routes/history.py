"""Portal routes — history domain (session history list/detail/resume).

Handlers moved verbatim from ``AgentWireServer`` for the #560 server.py split
(#585). Thin wrappers over ``self.run_agentwire_cmd`` (the CLI is the SSOT),
resolved through the composed class MRO.
"""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


class HistoryRoutesMixin:
    async def api_history_list(self, request: web.Request) -> web.Response:
        """GET /api/history - List session history.

        Query params:
            project: Project path (required)
            machine: Machine ID (default "local")
            limit: Max number of entries (default 20)

        Response:
            {history: [{sessionId, firstMessage, lastSummary, timestamp, messageCount}, ...]}
        """
        try:
            project = request.query.get("project")
            if not project:
                return web.json_response(
                    {"error": "project parameter is required"},
                    status=400
                )

            machine = request.query.get("machine", "local")
            limit = request.query.get("limit", "20")

            args = [
                "history", "list",
                "--project", project,
                "--machine", machine,
                "--limit", str(limit)
            ]

            success, result = await self.run_agentwire_cmd(args)
            if not success:
                error_msg = result.get("error", "Failed to list history") if isinstance(result, dict) else "Failed to list history"
                return web.json_response({"error": error_msg}, status=500)

            # CLI returns list directly, wrap it
            history = result if isinstance(result, list) else result.get("history", [])
            return web.json_response({"history": history})

        except Exception as e:
            logger.error(f"History list API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_history_detail(self, request: web.Request) -> web.Response:
        """GET /api/history/{session_id} - Get session history details.

        URL params:
            session_id: The session ID to get details for

        Query params:
            machine: Machine ID (default "local")

        Response:
            {sessionId, summaries: [], firstMessage, timestamps: {start, end}, gitBranch, messageCount}
        """
        try:
            session_id = request.match_info["session_id"]
            machine = request.query.get("machine", "local")

            args = [
                "history", "show",
                session_id,
                "--machine", machine
            ]

            success, result = await self.run_agentwire_cmd(args)
            if not success:
                error_msg = result.get("error", "Failed to get history detail") if isinstance(result, dict) else "Failed to get history detail"
                return web.json_response({"error": error_msg}, status=500)

            return web.json_response(result)

        except Exception as e:
            logger.error(f"History detail API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_history_resume(self, request: web.Request) -> web.Response:
        """POST /api/history/{session_id}/resume - Resume a session from history.

        URL params:
            session_id: The session ID to resume

        Request body:
            name: Optional custom session name
            projectPath: Project path (required)
            machine: Machine ID (required)

        Response:
            {session: "<new-tmux-session-name>"}
        """
        try:
            session_id = request.match_info["session_id"]
            data = await request.json()

            project_path = data.get("projectPath")
            if not project_path:
                return web.json_response(
                    {"error": "projectPath is required"},
                    status=400
                )

            machine = data.get("machine", "local")
            name = data.get("name")

            args = [
                "history", "resume",
                session_id,
                "--project", project_path,
                "--machine", machine
            ]
            if name:
                args.extend(["--name", name])

            success, result = await self.run_agentwire_cmd(args)
            if not success:
                error_msg = result.get("error", "Failed to resume session") if isinstance(result, dict) else "Failed to resume session"
                return web.json_response({"error": error_msg}, status=500)

            session_name = result.get("session") if isinstance(result, dict) else None
            return web.json_response({"session": session_name})

        except Exception as e:
            logger.error(f"History resume API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)


def register_history_routes(server, app):
    """Wire the history domain's routes onto ``app``."""
    app.router.add_get("/api/history", server.api_history_list)
    app.router.add_get("/api/history/{session_id}", server.api_history_detail)
    app.router.add_post("/api/history/{session_id}/resume", server.api_history_resume)
