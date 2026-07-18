"""Portal routes — desktop domain (window control + toast notifications).

Handlers moved verbatim from ``AgentWireServer`` for the #560 server.py split.
They read/write ``self.active_notifications`` and call ``self.broadcast_dashboard``
/ ``self._fanout_push`` — all of which stay on the base class and resolve
through the composed server's MRO.
"""


from aiohttp import web


class DesktopRoutesMixin:
    async def api_desktop_windows(self, request):
        """GET /api/desktop/windows — query browser clients for open windows."""
        # We don't track window state server-side; broadcast a request
        # and let the browser respond. For now, return what we can infer
        # from recent broadcasts. A simple approach: ask clients to report.
        import asyncio
        import uuid

        request_id = str(uuid.uuid4())[:8]

        # Set up a future to collect responses
        if not hasattr(self, '_desktop_window_responses'):
            self._desktop_window_responses = {}

        future = asyncio.get_event_loop().create_future()
        self._desktop_window_responses[request_id] = future

        # Ask all dashboard clients to report their windows
        await self.broadcast_dashboard("desktop_report_windows", {
            "request_id": request_id,
        })

        # Wait for a response (first client to respond wins)
        try:
            windows = await asyncio.wait_for(future, timeout=3.0)
        except asyncio.TimeoutError:
            windows = []
        finally:
            self._desktop_window_responses.pop(request_id, None)

        return web.json_response({"success": True, "windows": windows})

    async def api_desktop_open(self, request):
        """POST /api/desktop/window/open — open a window in the portal.

        Session and panel windows only. Artifacts deliberately have no
        force-open path (#817): producers POST /api/desktop/notification with
        an ``artifact`` target and the window opens when the human clicks.
        """
        data = await request.json()
        window_type = data.get("type", "session")
        window_id = None

        if window_type == "session":
            session = data.get("session")
            mode = data.get("mode", "monitor")
            if not session:
                return web.json_response({"success": False, "error": "session required"}, status=400)
            window_id = session
            await self.broadcast_dashboard("desktop_open_window", {
                "window_type": "session",
                "session": session,
                "mode": mode,
            })
        elif window_type == "panel":
            panel = data.get("panel")
            if not panel:
                return web.json_response({"success": False, "error": "panel required"}, status=400)
            window_id = panel
            await self.broadcast_dashboard("desktop_open_window", {
                "window_type": "panel",
                "panel": panel,
            })
        else:
            return web.json_response({"success": False, "error": f"unknown type: {window_type}"}, status=400)

        return web.json_response({"success": True, "window_id": window_id})

    async def api_desktop_close(self, request):
        """POST /api/desktop/window/close — close a window."""
        data = await request.json()
        window_id = data.get("window_id")
        if not window_id:
            return web.json_response({"success": False, "error": "window_id required"}, status=400)

        await self.broadcast_dashboard("desktop_close_window", {
            "window_id": window_id,
        })
        return web.json_response({"success": True})

    async def api_desktop_focus(self, request):
        """POST /api/desktop/window/focus — bring a window to front."""
        data = await request.json()
        window_id = data.get("window_id")
        if not window_id:
            return web.json_response({"success": False, "error": "window_id required"}, status=400)

        await self.broadcast_dashboard("desktop_focus_window", {
            "window_id": window_id,
        })
        return web.json_response({"success": True})

    async def api_desktop_tile(self, request):
        """POST /api/desktop/window/tile — tile a window to a zone."""
        data = await request.json()
        window_id = data.get("window_id")
        zone = data.get("zone")
        if not window_id or not zone:
            return web.json_response({"success": False, "error": "window_id and zone required"}, status=400)

        valid_zones = ["left", "right", "top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right"]
        if zone not in valid_zones:
            return web.json_response({"success": False, "error": f"invalid zone: {zone}. Valid: {valid_zones}"}, status=400)

        await self.broadcast_dashboard("desktop_tile_window", {
            "window_id": window_id,
            "zone": zone,
        })
        return web.json_response({"success": True})

    async def api_desktop_minimize_all(self, request):
        """POST /api/desktop/window/minimize-all — minimize all windows."""
        await self.broadcast_dashboard("desktop_minimize_all", {})
        return web.json_response({"success": True})

    async def api_desktop_collage(self, request):
        """POST /api/desktop/collage — toggle the window collage overlay."""
        await self.broadcast_dashboard("desktop_collage", {})
        return web.json_response({"success": True})

    async def api_desktop_layout(self, request):
        """POST /api/desktop/layout — apply a multi-window layout."""
        data = await request.json()
        windows = data.get("windows", [])
        if not windows:
            return web.json_response({"success": False, "error": "windows list required"}, status=400)

        await self.broadcast_dashboard("desktop_apply_layout", {
            "windows": windows,
        })
        return web.json_response({"success": True})

    # =========================================================================
    # Desktop Notifications API
    # =========================================================================

    async def api_desktop_notification(self, request):
        """POST /api/desktop/notification — post a toast notification to the portal.

        One toast per session: if a toast with the same `session` is already
        active, it is dismissed before the new one is posted. Keeps the nagger
        from stacking N toasts for the same idle session across nag cycles.

        Optional ``artifact`` ``{url, title, artifact_id}`` (#817) makes the
        notification a click-to-open artifact notice — the sole portal-side
        path for announcing a background-produced artifact. ``text`` may be
        omitted then (a default is synthesized from the title); ``artifact_id``
        defaults to the same url-derived window id the frontend uses.
        """
        data = await request.json()
        text = data.get("text", "")

        artifact = data.get("artifact")
        if artifact is not None:
            url = (artifact or {}).get("url")
            if not url:
                return web.json_response({"success": False, "error": "artifact.url required"}, status=400)
            title = artifact.get("title") or "Artifact"
            artifact = {
                "url": url,
                "title": title,
                "artifact_id": artifact.get("artifact_id")
                or f"artifact-{url.replace('/', '-').replace('.', '-')}",
            }
            if not text:
                text = f"**{title}** is ready — click to open"

        if not text:
            return web.json_response({"success": False, "error": "text required"}, status=400)

        timeout = data.get("timeout")
        if timeout is not None:
            try:
                timeout = max(0.0, float(timeout))
            except (TypeError, ValueError):
                timeout = None

        clients = len(self.dashboard_clients)
        notification_id = await self._post_toast(
            text,
            session=data.get("session"),
            priority=data.get("priority", "normal"),
            notification_id=data.get("id"),
            timeout=timeout,
            artifact=artifact,
        )

        # Report how many dashboards saw it live. 0 isn't a failure — the toast
        # is persisted in active_notifications and restored on the next page
        # load — but the caller deserves to know nobody is watching right now.
        return web.json_response({"success": True, "id": notification_id, "clients": clients})

    async def api_desktop_notification_dismiss(self, request):
        """POST /api/desktop/notification/dismiss — dismiss a notification."""
        data = await request.json()
        notification_id = data.get("id")
        if not notification_id:
            return web.json_response({"success": False, "error": "id required"}, status=400)

        self.active_notifications.pop(notification_id, None)

        await self.broadcast_dashboard("notification_dismiss", {"id": notification_id})

        return web.json_response({"success": True})

    async def api_desktop_notifications_list(self, request):
        """GET /api/desktop/notifications — list active notifications (for page load restore)."""
        return web.json_response({
            "success": True,
            "notifications": list(self.active_notifications.values()),
        })


def register_desktop_routes(server, app):
    """Wire the desktop domain's routes onto ``app``."""
    # Desktop UI control (for MCP agents)
    app.router.add_get("/api/desktop/windows", server.api_desktop_windows)
    app.router.add_post("/api/desktop/window/open", server.api_desktop_open)
    app.router.add_post("/api/desktop/window/close", server.api_desktop_close)
    app.router.add_post("/api/desktop/window/focus", server.api_desktop_focus)
    app.router.add_post("/api/desktop/window/tile", server.api_desktop_tile)
    app.router.add_post("/api/desktop/window/minimize-all", server.api_desktop_minimize_all)
    app.router.add_post("/api/desktop/collage", server.api_desktop_collage)
    app.router.add_post("/api/desktop/layout", server.api_desktop_layout)
    # Desktop notifications
    app.router.add_post("/api/desktop/notification", server.api_desktop_notification)
    app.router.add_post("/api/desktop/notification/dismiss", server.api_desktop_notification_dismiss)
    app.router.add_get("/api/desktop/notifications", server.api_desktop_notifications_list)
