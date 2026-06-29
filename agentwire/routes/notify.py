"""Portal routes — notify domain (tmux hook lifecycle events).

Part of the #560 server.py split. The ``api_notify`` handler was moved
verbatim from ``AgentWireServer``; it depends only on ``self`` helpers
(``broadcast_dashboard``, ``_get_sessions_data``, ``session_client_counts``,
``dashboard_clients``) which resolve through the MRO of the composed server
class.

Note: ``_post_toast`` deliberately STAYS on the base class — it has callers
across backends/services/sessions and is not part of this route domain.
"""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


class NotifyRoutesMixin:
    async def api_notify(self, request: web.Request) -> web.Response:
        """POST /api/notify - Receive tmux hook notifications.

        Called by tmux hooks (via agentwire notify) when sessions/panes change.
        Broadcasts the event to all connected dashboard clients.

        Request body:
            event: Event type:
                - session_closed, session_created: Session lifecycle
                - pane_died, pane_created: Pane lifecycle
                - client_attached, client_detached: Presence tracking
                - session_renamed: Session name changes (old_name, new_name)
                - pane_focused: Active pane tracking (pane_id)
                - window_activity: Activity in monitored window
            session: Session name
            pane: Pane index (optional, for pane events)
            pane_id: Pane ID (optional, for pane events)
            old_name: Previous session name (for session_renamed)
            new_name: New session name (for session_renamed)

        Response:
            {success: true}
        """
        try:
            data = await request.json()
            event = data.get("event")
            session = data.get("session")

            if not event:
                return web.json_response(
                    {"error": "event is required"},
                    status=400
                )

            logger.info(f"Received notify: event={event}, session={session}")

            # Broadcast to dashboard clients based on event type
            if event == "session_closed":
                await self.broadcast_dashboard("session_closed", {"session": session})
                # Clean up stale state for this session
                self.session_client_counts.pop(session, None)
                # Also send sessions_update with refreshed list
                sessions_data = await self._get_sessions_data()
                await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

            elif event == "session_created":
                await self.broadcast_dashboard("session_created", {"session": session})
                sessions_data = await self._get_sessions_data()
                await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

            elif event == "pane_died":
                pane = data.get("pane")
                pane_id = data.get("pane_id")
                await self.broadcast_dashboard("pane_died", {"session": session, "pane": pane, "pane_id": pane_id})
                # Also send sessions_update to refresh pane counts
                sessions_data = await self._get_sessions_data()
                await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

            elif event == "pane_created":
                pane = data.get("pane")
                pane_id = data.get("pane_id")
                await self.broadcast_dashboard("pane_created", {"session": session, "pane": pane, "pane_id": pane_id})
                # Also send sessions_update to refresh pane counts
                sessions_data = await self._get_sessions_data()
                await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

            elif event == "client_attached":
                # Increment attached client count for this session
                self.session_client_counts[session] = self.session_client_counts.get(session, 0) + 1
                await self.broadcast_dashboard("client_attached", {
                    "session": session,
                    "client_count": self.session_client_counts[session]
                })
                # Also send sessions_update to refresh client counts
                sessions_data = await self._get_sessions_data()
                await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

            elif event == "client_detached":
                # Decrement attached client count for this session
                count = self.session_client_counts.get(session, 1)
                self.session_client_counts[session] = max(0, count - 1)
                await self.broadcast_dashboard("client_detached", {
                    "session": session,
                    "client_count": self.session_client_counts[session]
                })
                # Also send sessions_update to refresh client counts
                sessions_data = await self._get_sessions_data()
                await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

            elif event == "session_renamed":
                # Handle session rename - old_name and new_name in data
                old_name = data.get("old_name")
                new_name = data.get("new_name") or session
                # Transfer client count to new name
                if old_name and old_name in self.session_client_counts:
                    self.session_client_counts[new_name] = self.session_client_counts.pop(old_name)
                await self.broadcast_dashboard("session_renamed", {
                    "old_name": old_name,
                    "new_name": new_name
                })
                sessions_data = await self._get_sessions_data()
                await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

            elif event == "pane_focused":
                # Track which pane is focused in a session
                pane_id = data.get("pane_id")
                await self.broadcast_dashboard("pane_focused", {
                    "session": session,
                    "pane_id": pane_id
                })

            elif event == "window_activity":
                # Activity detected in a monitored window
                await self.broadcast_dashboard("window_activity", {"session": session})

            elif event == "scheduler_state":
                # Full scheduler state push — broadcast live state to dashboards
                await self.broadcast_dashboard("scheduler_state", data)

            elif event == "agent_progress":
                # Live agent progress — broadcast to dashboards
                await self.broadcast_dashboard("agent_progress", data)

            elif event == "scheduler_task_complete":
                # Scheduler task finished — broadcast to dashboards
                await self.broadcast_dashboard("scheduler_update", {
                    "task": data.get("task"),
                    "status": data.get("status"),
                    "duration": data.get("duration"),
                    "summary": data.get("summary"),
                })

            else:
                # Generic event - just broadcast it
                await self.broadcast_dashboard(event, data)

            # Report how many dashboards received the broadcast. A lifecycle
            # event is ephemeral (not persisted), so 0 clients means nobody saw
            # it — the caller should know that, not get a blind "broadcast" (#444).
            return web.json_response({"success": True, "clients": len(self.dashboard_clients)})

        except Exception as e:
            logger.error(f"Notify API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)


def register_notify_routes(server, app):
    """Wire the notify domain's routes onto ``app``."""
    app.router.add_post("/api/notify", server.api_notify)
