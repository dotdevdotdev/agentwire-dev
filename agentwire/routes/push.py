"""Portal routes — push domain (Web Push subscriptions, #483).

Handlers moved verbatim from ``AgentWireServer`` for the #560 server.py split.
``_fanout_push`` deliberately stays on the base class: it's shared by the
desktop notification path and the service watchdog, not part of this domain's
HTTP surface.
"""

from aiohttp import web


class PushRoutesMixin:
    async def api_push_config(self, request: web.Request) -> web.Response:
        """GET /api/push/config — public-key + enabled flag for the push client (#483)."""
        from ..channels.push import _get_push_config, push_ready

        cfg = _get_push_config()
        ready, _reason = push_ready()
        return web.json_response(
            {"enabled": bool(ready), "vapidPublicKey": cfg.vapid_public_key or ""}
        )

    async def api_push_subscribe(self, request: web.Request) -> web.Response:
        """POST /api/push/subscribe — persist a browser's Web Push subscription (#483)."""
        from .. import push_store

        try:
            data = await request.json()
        except Exception:
            data = {}
        endpoint = (data.get("endpoint") or "").strip()
        keys = data.get("keys") or {}
        if not endpoint or not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
            return web.json_response(
                {"success": False, "error": "endpoint and keys{p256dh,auth} required"},
                status=400,
            )
        push_store.add(endpoint=endpoint, keys=keys, device=str(data.get("device", "")))
        return web.json_response({"success": True})

    async def api_push_unsubscribe(self, request: web.Request) -> web.Response:
        """POST /api/push/unsubscribe — drop a stored subscription (#483)."""
        from .. import push_store

        try:
            data = await request.json()
        except Exception:
            data = {}
        endpoint = (data.get("endpoint") or "").strip()
        if not endpoint:
            return web.json_response({"success": False, "error": "endpoint required"}, status=400)
        removed = push_store.remove(endpoint)
        return web.json_response({"success": True, "removed": removed})


def register_push_routes(server, app):
    """Wire the push domain's routes onto ``app``."""
    app.router.add_get("/api/push/config", server.api_push_config)
    app.router.add_post("/api/push/subscribe", server.api_push_subscribe)
    app.router.add_post("/api/push/unsubscribe", server.api_push_unsubscribe)
