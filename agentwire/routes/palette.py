"""Portal routes — user-defined command-palette items (#676).

Thin wrappers over ``agentwire palette list|run`` (the CLI is the SSOT),
resolved through the composed class MRO via ``self.run_agentwire_cmd``.
"""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


class PaletteRoutesMixin:
    async def api_palette_list(self, request: web.Request) -> web.Response:
        """GET /api/palette - List user-defined palette items.

        Response:
            {items: [{id, label, icon, keywords, fields: [...]}, ...], errors: [...]}

        The ``run`` template is stripped from the response — the frontend
        never needs it, and it stays server-side.
        """
        try:
            success, result = await self.run_agentwire_cmd(["palette", "list"])
            if not success:
                error_msg = result.get("error", "Failed to list palette items") if isinstance(result, dict) else "Failed to list palette items"
                return web.json_response({"error": error_msg}, status=500)
            items = [
                {k: v for k, v in item.items() if k != "run"}
                for item in result.get("items", [])
            ]
            return web.json_response({"items": items, "errors": result.get("errors", [])})
        except Exception as e:
            logger.error(f"Palette list API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_palette_run(self, request: web.Request) -> web.Response:
        """POST /api/palette/run - Run a user-defined palette item.

        Body:
            {id: "quicktask", fields: {name: "...", project: "..."}}

        Response:
            {success, exit_code, output}
        """
        try:
            data = await request.json()
            item_id = str(data.get("id") or "").strip()
            if not item_id:
                return web.json_response({"error": "id is required"}, status=400)
            fields = data.get("fields") or {}
            if not isinstance(fields, dict):
                return web.json_response({"error": "fields must be an object"}, status=400)

            args = ["palette", "run", item_id]
            for name, value in fields.items():
                args += ["--field", f"{name}={value}"]

            success, result = await self.run_agentwire_cmd(args)
            if not isinstance(result, dict):
                result = {}
            if not success and "output" not in result:
                return web.json_response(
                    {"error": result.get("error", "Palette run failed")}, status=400)
            return web.json_response({
                "success": bool(result.get("success", success)),
                "exit_code": result.get("exit_code", 0 if success else 1),
                "output": result.get("output", ""),
            })
        except Exception as e:
            logger.error(f"Palette run API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)


def register_palette_routes(server, app):
    """Wire the palette domain's routes onto ``app``."""
    app.router.add_get("/api/palette", server.api_palette_list)
    app.router.add_post("/api/palette/run", server.api_palette_run)
