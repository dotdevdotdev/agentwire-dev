"""Portal routes — scratchpad domain (shared notes drawer).

Spike slice for the #560 server.py split. Handlers moved verbatim from
``AgentWireServer``; they depend only on the ``scratchpad`` module and the
core ``self.broadcast_dashboard`` helper, which resolves through the MRO of
the composed server class.
"""

from aiohttp import web


class ScratchpadRoutesMixin:
    async def _broadcast_scratchpad(self):
        from .. import scratchpad
        await self.broadcast_dashboard("scratchpad_updated", {"notes": scratchpad.load_notes()})

    async def api_scratchpad_list(self, request: web.Request) -> web.Response:
        """GET /api/scratchpad - All notes, newest first."""
        from .. import scratchpad
        return web.json_response({"notes": scratchpad.load_notes()})

    async def api_scratchpad_add(self, request: web.Request) -> web.Response:
        """POST /api/scratchpad/notes {text, source?} - Create a note."""
        from .. import scratchpad
        try:
            data = await request.json()
            note = scratchpad.add_note(data.get("text", ""), source=data.get("source"))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        await self._broadcast_scratchpad()
        return web.json_response({"success": True, "note": note})

    async def api_scratchpad_update(self, request: web.Request) -> web.Response:
        """PUT /api/scratchpad/notes/{note_id} {text} - Edit a note."""
        from .. import scratchpad
        try:
            data = await request.json()
            note = scratchpad.update_note(request.match_info["note_id"], data.get("text", ""))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        if note is None:
            return web.json_response({"error": "Note not found"}, status=404)
        await self._broadcast_scratchpad()
        return web.json_response({"success": True, "note": note})

    async def api_scratchpad_remove(self, request: web.Request) -> web.Response:
        """DELETE /api/scratchpad/notes/{note_id} - Delete a note."""
        from .. import scratchpad
        if not scratchpad.remove_note(request.match_info["note_id"]):
            return web.json_response({"error": "Note not found"}, status=404)
        await self._broadcast_scratchpad()
        return web.json_response({"success": True})

    async def api_scratchpad_changed(self, request: web.Request) -> web.Response:
        """POST /api/scratchpad/changed - External writer (CLI/MCP) ping.

        The file is already written; just rebroadcast so open drawers refresh.
        """
        await self._broadcast_scratchpad()
        return web.json_response({"success": True})


def register_scratchpad_routes(server, app):
    """Wire the scratchpad domain's routes onto ``app``."""
    app.router.add_get("/api/scratchpad", server.api_scratchpad_list)
    app.router.add_post("/api/scratchpad/notes", server.api_scratchpad_add)
    app.router.add_put("/api/scratchpad/notes/{note_id}", server.api_scratchpad_update)
    app.router.add_delete("/api/scratchpad/notes/{note_id}", server.api_scratchpad_remove)
    app.router.add_post("/api/scratchpad/changed", server.api_scratchpad_changed)
