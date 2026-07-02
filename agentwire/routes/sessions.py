"""Portal routes — sessions domain, READ handlers (PR-A of 2).

Part of the #560 / #593 server.py split. Handlers moved verbatim from
``AgentWireServer``; they depend on core attributes and helpers
(``self.run_agentwire_cmd``, ``self._get_global_session_activity``,
``self._compute_session_states``, ``self.remote_sessions_checker``,
``self.active_sessions``, ``self.config``) which resolve through the MRO of
the composed server class. The shared session state and helpers
(``active_sessions``, ``_get_sessions_data``, ``_get_session_config``,
``_fetch_remote_machine_sessions``, the yaml read/write helpers, …) stay on
the base server; only the read handlers move here. The mutate handlers ship
separately in ``sessions_admin.py`` (PR-B).
"""

import asyncio
import json
import logging
import socket
import time

from aiohttp import web

logger = logging.getLogger(__name__)


class SessionsRoutesMixin:
    async def api_sessions(self, request: web.Request) -> web.Response:
        """List all active sessions grouped by machine (in-process, cached)."""
        try:
            # In-process, TTL-cached listings shared with the monitor loop (#627)
            self._last_sessions_poll = time.monotonic()
            all_sessions = await self._list_local_sessions()
            for machine_sessions in (await self._list_remote_sessions()).values():
                all_sessions.extend(machine_sessions)
            for s in all_sessions:
                s["activity"] = self._get_global_session_activity(s.get("name", ""))

            # Group sessions by machine
            machine_sessions = {}
            for s in all_sessions:
                machine_id = s.get("machine") or "local"  # Handle null/None
                if machine_id not in machine_sessions:
                    machine_sessions[machine_id] = []
                machine_sessions[machine_id].append(s)

            # Build machine list
            machines = []
            for machine_id, sessions_list in machine_sessions.items():
                machines.append({
                    "id": machine_id,
                    "host": machine_id,
                    "status": "online",  # If we got sessions, machine is online
                    "session_count": len(sessions_list),
                    "sessions": sessions_list,
                })

            # Sort machines: local first, then others alphabetically
            machines.sort(key=lambda m: (m["id"] != "local" and not m["id"].endswith(socket.gethostname().split('.')[0]), m["id"]))

            return web.json_response({"machines": machines})
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            return web.json_response({"machines": []})

    async def api_worktrees(self, request: web.Request) -> web.Response:
        """List worktree sessions (across all repos) with read-only git status.

        Thin wrapper over `agentwire worktree --list --all --json`; the CLI
        folds in local-only git status (dirty/ahead/behind/pushed) per entry so
        the sidebar can badge worktree sessions without per-session round-trips.
        """
        try:
            success, result = await self.run_agentwire_cmd(["worktree", "--list", "--all"])
            entries = result.get("entries", []) if success else []
            return web.json_response({"entries": entries})
        except Exception as e:
            logger.error(f"Failed to list worktrees: {e}")
            return web.json_response({"entries": []})

    async def api_sessions_local(self, request: web.Request) -> web.Response:
        """Fast endpoint for local sessions only (no SSH checks)."""
        try:
            # In-process, TTL-cached listing shared with the monitor loop (#627);
            # the poll timestamp keeps the monitor ticking for HTTP-only clients.
            self._last_sessions_poll = time.monotonic()
            sessions = await self._list_local_sessions()
            # Add activity status
            for s in sessions:
                s["activity"] = self._get_global_session_activity(s.get("name", ""))

            # Computed state (off/needs_input/working/idle) — shells out to
            # tmux per session, so keep it off the event loop.
            await asyncio.to_thread(self._compute_session_states, sessions)

            return web.json_response({"sessions": sessions})
        except Exception as e:
            logger.error(f"Failed to list local sessions: {e}")
            return web.json_response({"sessions": []})

    async def api_sessions_remote(self, request: web.Request) -> web.Response:
        """Endpoint for remote sessions grouped by machine (progressive loading)."""
        try:
            # Get list of configured machines
            machines_file = self.config.machines.file
            if not machines_file.exists():
                return web.json_response({"machines": []})

            with open(machines_file) as f:
                data = json.load(f)
                remote_machines = [
                    {"id": m.get("id"), "host": m.get("host")}
                    for m in data.get("machines", [])
                ]

            # Progressive loading: returns cached or "checking" status
            machines = await self.remote_sessions_checker.get_with_status(
                remote_machines,
                check_fn=self._fetch_remote_machine_sessions,
                id_field='id'
            )

            return web.json_response({"machines": machines})
        except Exception as e:
            logger.error(f"Failed to list remote sessions: {e}")
            return web.json_response({"machines": []})

    async def api_session_connections(self, request: web.Request) -> web.Response:
        """GET /api/sessions/{session}/connections - Check if session has active browser connections."""
        name = request.match_info["name"]
        try:
            has_connections = False
            connection_count = 0

            if name in self.active_sessions:
                session = self.active_sessions[name]
                connection_count = len(session.clients)
                has_connections = connection_count > 0

            return web.json_response({
                "has_connections": has_connections,
                "connection_count": connection_count
            })

        except Exception as e:
            logger.error(f"Session connections check failed: {e}")
            return web.json_response({"error": str(e)}, status=500)


def register_sessions_routes(server, app):
    """Wire the sessions domain's read routes onto ``app``."""
    app.router.add_get("/api/sessions", server.api_sessions)
    app.router.add_get("/api/sessions/local", server.api_sessions_local)
    app.router.add_get("/api/sessions/remote", server.api_sessions_remote)
    app.router.add_get("/api/worktrees", server.api_worktrees)
    app.router.add_get("/api/sessions/{name:.+}/connections", server.api_session_connections)
