"""Portal routes — council domain (multi-soul deliberation board).

Part of the #560 server.py split. Handlers moved verbatim from
``AgentWireServer``; they depend on the ``council`` package plus core
attributes (``self.agent``, ``self.run_agentwire_cmd``,
``self.broadcast_dashboard``, ``self.dashboard_clients``), which resolve
through the MRO of the composed server class. The ``council_watch_loop`` /
``_council_tick`` background pair stays here too — it is launched from the
base server's startup as ``server.council_watch_loop()``.
"""

import asyncio
import logging

from aiohttp import web

logger = logging.getLogger(__name__)


class CouncilRoutesMixin:
    async def _council_dead_souls(self, sitting) -> set:
        """Roster souls whose lens tmux session is gone (→ stalled, not pending).

        One ``tmux list-sessions`` off the event loop; council sessions are
        local so the ``@machine`` suffix is stripped before matching.
        """
        if not sitting or not sitting.sessions:
            return set()
        try:
            loop = asyncio.get_event_loop()
            live_raw = await loop.run_in_executor(None, self.agent.list_sessions)
        except Exception:
            return set()
        live = {s.split("@")[0] for s in live_raw}
        return {
            soul
            for soul, sess in sitting.sessions.items()
            if sess.split("@")[0] not in live
        }

    async def api_council_sittings(self, request: web.Request) -> web.Response:
        """GET /api/council/sittings - Names of every live council sitting."""
        try:
            from ..council import state as council_state
            return web.json_response({"sittings": council_state.list_sittings()})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_council_archive(self, request: web.Request) -> web.Response:
        """GET /api/council/archive - Dismissed threads, newest first.

        Each entry: ``{name, rounds, last_prompt_text, dismissed_at, cwd}`` —
        enough for the sidebar to list past deliberations; the board reads the
        full thread via ``/api/council/live?sitting=<name>``.
        """
        try:
            from ..council import inbox as council_inbox
            from ..council import state as council_state
            from ..council import view as council_view

            out = []
            for name in council_state.list_archive():
                ids = council_view.available_prompt_ids(name)
                last_text = ""
                if ids:
                    last_text = self._read_text_safe(
                        council_inbox.prompt_dir(name, ids[-1]) / "prompt.md"
                    )
                rec = council_state.read_archive_dict(name) or {}
                out.append(
                    {
                        "name": name,
                        "rounds": len(ids),
                        "last_prompt_text": last_text,
                        "dismissed_at": rec.get("dismissed_at", ""),
                        "cwd": rec.get("cwd", ""),
                    }
                )
            out.sort(key=lambda e: e.get("dismissed_at", ""), reverse=True)
            return web.json_response({"archive": out})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    @staticmethod
    def _read_text_safe(path) -> str:
        try:
            return path.read_text()
        except OSError:
            return ""

    async def api_council_live(self, request: web.Request) -> web.Response:
        """GET /api/council/live - Board snapshot for a sitting (mirrors
        /api/scheduler/live).

        Query: ``sitting`` (defaults to the sole live sitting), ``prompt_id``
        (defaults to the latest). 404 when the named sitting has no state.
        """
        try:
            from ..council import state as council_state
            from ..council import view as council_view

            name = request.query.get("sitting")
            if not name:
                live = council_state.list_sittings()
                if len(live) == 1:
                    name = live[0]
                elif not live:
                    return web.json_response(
                        {"running": False, "sittings": []}, status=404
                    )
                else:
                    # Ambiguous — let the client pick from the list.
                    return web.json_response(
                        {"running": False, "sittings": live}, status=409
                    )

            # A dismissed thread has no live sitting.json but is still a fully
            # readable artifact (archive.json + prompts/). Only 404 when there
            # is genuinely nothing on disk.
            live = council_state.read_sitting(name)
            prompt_id_raw = request.query.get("prompt_id")
            prompt_id = int(prompt_id_raw) if prompt_id_raw else None
            dead = await self._council_dead_souls(live) if live else set()
            snap = council_view.snapshot(name, prompt_id, dead_souls=dead)
            if snap is None:
                return web.json_response(
                    {"running": False, "sittings": council_state.list_sittings()},
                    status=404,
                )
            snap["running"] = live is not None
            snap["sittings"] = council_state.list_sittings()
            return web.json_response(snap)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_council_status(self, request: web.Request) -> web.Response:
        """GET /api/council/status - Per-soul session liveness for a sitting.

        Thin wrapper over ``council status`` (CLI is the SSOT). ``council status``
        exits 0 even when no sitting matches (``running: false``), so the JSON
        passes straight through with a 200.
        """
        try:
            args = ["council", "status"]
            name = request.query.get("sitting")
            if name:
                args += ["--name", name]
            success, result = await self.run_agentwire_cmd(args)
            return web.json_response(result, status=200 if success else 400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _council_body(self, request: web.Request) -> dict:
        """Best-effort JSON body — POSTs from the board may have no body."""
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def api_council_start(self, request: web.Request) -> web.Response:
        """POST /api/council/start - Seat a council (CLI: ``council start``).

        Body (all optional): ``sitting``/``name`` (default: cwd-repo-slug),
        ``roster`` (comma-separated lens names). Broadcasts a seating delta so
        the rail + sidebar go live without a manual refresh.
        """
        try:
            body = await self._council_body(request)
            args = ["council", "start"]
            name = body.get("sitting") or body.get("name")
            roster = body.get("roster")
            if name:
                args += ["--name", str(name)]
            if roster:
                args += ["--roster", str(roster)]
            success, result = await self.run_agentwire_cmd(args)
            if success:
                seated = result.get("council") or name
                if seated:
                    await self.broadcast_dashboard(
                        "council_update", {"sitting": seated, "seating": True}
                    )
                return web.json_response(result)
            return web.json_response(result, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_council_stop(self, request: web.Request) -> web.Response:
        """POST /api/council/stop - Dismiss a council (CLI: ``council stop``)."""
        try:
            body = await self._council_body(request)
            args = ["council", "stop"]
            name = body.get("sitting") or body.get("name")
            if name:
                args += ["--name", str(name)]
            success, result = await self.run_agentwire_cmd(args)
            if success:
                stopped = result.get("council") or name
                if stopped:
                    await self.broadcast_dashboard(
                        "council_update", {"sitting": stopped, "stopped": True}
                    )
                return web.json_response(result)
            return web.json_response(result, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_council_ask(self, request: web.Request) -> web.Response:
        """POST /api/council/ask - Fan a new prompt out (CLI: ``council ask``).

        Body: ``{sitting?, prompt}``. The prompt rides after a ``--`` so leading
        dashes can't be parsed as flags. Broadcasts a reset so the board switches
        to the new round (the watch loop would catch it within ~1.5s anyway).
        """
        try:
            body = await self._council_body(request)
            prompt = body.get("prompt")
            if not prompt or not str(prompt).strip():
                return web.json_response({"error": "prompt required"}, status=400)
            args = ["council", "ask"]
            name = body.get("sitting") or body.get("name")
            if name:
                args += ["--name", str(name)]
            args += ["--", str(prompt)]
            success, result = await self.run_agentwire_cmd(args)
            if success:
                seated = result.get("council") or name
                if seated:
                    await self.broadcast_dashboard(
                        "council_update",
                        {
                            "sitting": seated,
                            "prompt_id": result.get("prompt_id"),
                            "reset": True,
                        },
                    )
                return web.json_response(result)
            return web.json_response(result, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def council_watch_loop(self):
        """Poll live sittings' latest ``replies/`` dir and push ``council_update``
        deltas over the dashboard WS.

        A filesystem ``rename`` watch would be tighter, but a ~1.5s poll needs no
        new dependency and the producer-side atomic write (``inbox.py``) is what
        actually guarantees a reader never sees a half-written verdict. Each delta
        carries the fully-derived tile for the one soul that changed, so the
        browser swaps a single tile with no re-fetch and no flicker. A new prompt
        round emits ``{reset: True}`` so the board refetches its snapshot.
        """
        from ..council import inbox
        from ..council import state as council_state
        from ..council import view as council_view

        seen: dict[str, dict] = {}  # name -> {prompt_id, files: {name: mtime}}
        logger.info("[Council] Board watcher started")
        while True:
            try:
                if self.dashboard_clients:
                    live = set(council_state.list_sittings())
                    for stale in [n for n in seen if n not in live]:
                        seen.pop(stale, None)
                    for name in live:
                        await self._council_tick(name, seen, inbox, council_state, council_view)
            except asyncio.CancelledError:
                logger.info("[Council] Board watcher stopped")
                raise
            except Exception as e:
                logger.debug(f"[Council] watch tick failed: {e}")
            await asyncio.sleep(1.5)

    async def _council_tick(self, name, seen, inbox, council_state, council_view):
        pid = council_state.latest_prompt_id(name)
        if pid is None:
            return
        prev = seen.get(name)
        if prev is None or prev.get("prompt_id") != pid:
            # New prompt round — clear stale tile state, tell the board to refetch.
            seen[name] = {"prompt_id": pid, "files": {}}
            prev = seen[name]
            await self.broadcast_dashboard(
                "council_update", {"sitting": name, "prompt_id": pid, "reset": True}
            )
        rdir = inbox.replies_dir(name, pid)
        current: dict[str, float] = {}
        if rdir.is_dir():
            for p in rdir.glob("*.md"):
                try:
                    current[p.name] = p.stat().st_mtime
                except OSError:
                    pass
        changed_souls = {
            fname.split(".", 1)[0]
            for fname, mt in current.items()
            if prev["files"].get(fname) != mt
        }
        for soul in changed_souls:
            tile = council_view.derive_tile(name, pid, soul)
            await self.broadcast_dashboard(
                "council_update", {"sitting": name, "prompt_id": pid, "tile": tile}
            )
        seen[name] = {"prompt_id": pid, "files": current}


def register_council_routes(server, app):
    """Wire the council domain's routes onto ``app``."""
    app.router.add_get("/api/council/sittings", server.api_council_sittings)
    app.router.add_get("/api/council/archive", server.api_council_archive)
    app.router.add_get("/api/council/live", server.api_council_live)
    app.router.add_get("/api/council/status", server.api_council_status)
    app.router.add_post("/api/council/start", server.api_council_start)
    app.router.add_post("/api/council/stop", server.api_council_stop)
    app.router.add_post("/api/council/ask", server.api_council_ask)
