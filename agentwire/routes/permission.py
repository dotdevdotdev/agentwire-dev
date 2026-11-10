"""Portal routes — permission + review domain.

Part of the #560 server.py split. Handlers moved verbatim from
``AgentWireServer``; they depend on stdlib helpers plus ``prompt_router`` and
on core attributes (``self.active_sessions``, ``self._get_session_config``,
``self._broadcast``, ``self._say_to_room``, ``self.run_agentwire_cmd``), which
resolve through the MRO of the composed server class.

Review and permission are grouped because the Review window's ``_live_prompt``
helper is unique to ``api_review``. ``PendingPermission`` lives on the base
server module and is imported lazily inside the handlers to avoid a circular
import.
"""

import asyncio
import logging
import uuid

from aiohttp import web

from .. import prompt_router
from ..permission_summary import summarize

logger = logging.getLogger(__name__)


class PermissionRoutesMixin:
    def _live_prompt(self, session: str, pane: int = 0) -> "dict | None":
        """The live interactive prompt on a pane, shaped for the Review window.

        Returns kind/question/options plus the ``expect`` hash the guarded
        answer path needs. Prefers the router marker's hash when present (hook-
        routed permission prompts carry a payload-derived hash that can't be
        recomputed from the screen; ``prompt_router.answer`` bridges it), else
        the screen-derived content hash.
        """
        visible = prompt_router._capture(f"{session}.{pane}")
        info = prompt_router.detect_prompt(visible)
        if info is None:
            return None
        marker = prompt_router.read_marker(session, pane)
        expect = (marker or {}).get("hash") or info.content_hash()
        return {
            "kind": info.kind,
            "question": info.question,
            "summary": info.summary,
            "options": info.options,
            "expect": expect,
            "pane": pane,
        }

    async def api_review(self, request: web.Request) -> web.Response:
        """GET /api/review/{session} — structured diff + any live prompt."""
        name = request.match_info["name"]
        ok, diff = await self.run_agentwire_cmd(["diff", "-s", name])
        if not ok:
            return web.json_response(
                {"error": diff.get("error", "Failed to load diff")}, status=502
            )
        try:
            prompt = await asyncio.get_event_loop().run_in_executor(
                None, self._live_prompt, name
            )
        except Exception as e:
            logger.warning(f"[{name}] Review prompt detection failed: {e}")
            prompt = None
        return web.json_response({"success": True, "diff": diff, "prompt": prompt})

    async def api_review_answer(self, request: web.Request) -> web.Response:
        """POST /api/review/{session}/answer — approve/deny the live prompt.

        Drives the existing guarded compare-and-send path so a stale tap (the
        prompt already answered, or a different one now live) is a safe no-op.
        Approve selects option 1 (allow / proceed); deny sends Escape, which
        cancels a permission, plan, or question dialog.
        """
        name = request.match_info["name"]
        try:
            data = await request.json()
            decision = (data.get("decision") or "").strip().lower()
            expect = (data.get("expect") or "").strip()
            pane = int(data.get("pane", 0) or 0)
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid request body"}, status=400)

        if decision not in ("approve", "deny"):
            return web.json_response(
                {"error": "decision must be 'approve' or 'deny'"}, status=400
            )
        if not expect:
            return web.json_response({"error": "Missing 'expect' hash"}, status=400)

        keys = ["1"] if decision == "approve" else ["Escape"]
        try:
            ok, message = await asyncio.get_event_loop().run_in_executor(
                None, prompt_router.answer, name, pane, expect, keys
            )
        except Exception as e:
            logger.error(f"[{name}] Review answer failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

        status = 200 if ok else 409
        return web.json_response({"success": ok, "message": message}, status=status)

    async def api_permission_request(self, request: web.Request) -> web.Response:
        """POST /api/permission/{session} - Handle permission request from Claude Code hook.

        This endpoint is called by the permission hook script when Claude Code
        needs permission for an action. It broadcasts the request to connected
        clients and waits for a response.
        """
        from ..server import PendingPermission

        name = request.match_info["name"]
        try:
            data = await request.json()
            tool_name = data.get("tool_name", "unknown")
            tool_input = data.get("tool_input", {})
            message = data.get("message", "")

            logger.info(f"[{name}] Permission request: {tool_name}")

            # Ensure session exists
            session = await self._get_or_create_session(name)

            # Create pending permission request (prompted mode)
            try:
                pane_index = int(data.get("pane_index") or 0)
            except (TypeError, ValueError):
                pane_index = 0
            request_id = str(data.get("tool_use_id") or "")
            if not request_id or request_id in session.pending_permissions:
                request_id = f"{request_id or 'perm'}-{uuid.uuid4().hex[:8]}"
            pending = PendingPermission(
                request=data, request_id=request_id, pane_index=pane_index
            )
            session.pending_permissions[request_id] = pending
            who = prompt_router.agent_label(data)

            # Route to the parent/orchestrator session, if one resolves
            # (#276). Best-effort and non-blocking for the dialog itself.
            # The hook payload's tmux_session (the real tmux name) beats the
            # URL name, which may be a project alias.
            tmux_name = str(data.get("tmux_session") or "") or name
            parent_notified = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: prompt_router.notify_permission_request(tmux_name, pane_index, data),
            )

            # Broadcast permission request to all clients (Task 3.1)
            await self._broadcast(session, {
                "type": "permission_request",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "message": message,
                "parent_notified": parent_notified,
                "request_id": request_id,
                "session": name,
                "agent": who,
                "summary": summarize(name, data),
                "agent_id": data.get("agent_id"),
                "agent_type": data.get("agent_type"),
            })

            # Generate TTS announcement (Task 3.6)
            await self._announce_permission_request(
                name, data, parent_notified=parent_notified
            )

            # Wait for user decision with 5 minute timeout
            try:
                await asyncio.wait_for(pending.event.wait(), timeout=300)
            except asyncio.TimeoutError:
                logger.warning(f"[{name}] Permission request {request_id} timed out")
                # Remove ONLY this ask — a newer one on the session stays live.
                session.pending_permissions.pop(request_id, None)
                await self._broadcast(
                    session, {"type": "permission_timeout", "request_id": request_id}
                )
                await self._clear_marker_if_unshared(
                    session, tmux_name, pane_index
                )
                return web.json_response({
                    "decision": "deny",
                    "message": "Permission request timed out (5 minutes)"
                })

            # Return the decision to the hook script
            decision = pending.decision
            session.pending_permissions.pop(request_id, None)
            await self._clear_marker_if_unshared(session, tmux_name, pane_index)

            logger.info(f"[{name}] Permission decision: {decision}")
            return web.json_response(decision)

        except Exception as e:
            logger.error(f"Permission request failed: {e}")
            return web.json_response(
                {"decision": "deny", "message": str(e)},
                status=500
            )

    async def api_permission_respond(self, request: web.Request) -> web.Response:
        """POST /api/permission/{session}/respond - User responds to permission request.

        Called by the portal UI when user clicks Allow or Deny.
        """
        name = request.match_info["name"]
        try:
            data = await request.json()
            decision = data.get("decision", "deny")

            logger.info(f"[{name}] Permission response: {decision}")

            if name not in self.active_sessions:
                return web.json_response({"error": "Session not found"}, status=404)

            session = self.active_sessions[name]

            if not session.pending_permissions:
                return web.json_response({"error": "No pending permission request"}, status=400)

            request_id = str(data.get("request_id") or "")
            if not request_id:
                if len(session.pending_permissions) > 1:
                    return web.json_response({
                        "error": "Several permission requests are pending; pass request_id",
                        "pending": sorted(session.pending_permissions),
                    }, status=400)
                request_id = next(iter(session.pending_permissions))
            pending = session.pending_permissions.get(request_id)
            if pending is None:
                return web.json_response(
                    {"error": f"No pending permission request {request_id}"}, status=400
                )

            # Capture pane/session context BEFORE signaling — the waiting
            # request handler pops its entry as soon as it wakes.
            pane_index = pending.pane_index
            tmux_name = str(pending.request.get("tmux_session") or "") or name

            # Store decision and signal the waiting request
            pending.decision = {"decision": decision}
            if decision == "deny":
                pending.decision["message"] = data.get("message", "User denied permission")
            pending.event.set()

            # Send keystroke to the pane the dialog is on — CONDITIONALLY.
            # The parent session may have answered via `agentwire prompts
            # answer` moments earlier: a late '1' would type into the freed
            # input box, and a late Escape would abort the child's next turn.
            # Re-capture and only send if a live menu is still on screen.
            try:
                dialog_live = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: prompt_router.screen_shows_live_menu(
                        prompt_router._capture(f"{tmux_name}.{pane_index}")
                    ),
                )
                if not dialog_live:
                    logger.info(
                        f"[{name}] Dialog already gone (answered elsewhere) — skipping keystroke"
                    )
                elif decision == "custom":
                    # Custom feedback: send "3", then message, then Enter
                    custom_message = data.get("message", "")
                    if custom_message:
                        # send-keys handles pauses between key groups
                        result = await self._run_subprocess(
                            ["agentwire", "send-keys", "-s", tmux_name,
                             "--pane", str(pane_index), "3", custom_message, "Enter"]
                        )
                        if result is None or result[0] != 0:
                            raise RuntimeError("send-keys failed")
                        logger.info(f"[{name}] Sent custom feedback: {custom_message[:50]}...")
                else:
                    # Map decision to keystroke: allow=1, allow_always=2, deny=Escape
                    keystroke_map = {
                        "allow": "1",
                        "allow_always": "2",
                        "deny": "Escape",
                    }
                    keystroke = keystroke_map.get(decision, "Escape")
                    result = await self._run_subprocess(
                        ["agentwire", "send-keys", "-s", tmux_name,
                         "--pane", str(pane_index), keystroke]
                    )
                    if result is None or result[0] != 0:
                        raise RuntimeError("send-keys failed")
                    logger.info(f"[{name}] Sent keystroke '{keystroke}' to {tmux_name}.{pane_index}")
            except Exception as e:
                logger.error(f"[{name}] Failed to send keystroke: {e}")

            # The waiting handler may not have popped its entry yet; exclude it
            # so the marker clears when this was the last ask on the pane.
            await self._clear_marker_if_unshared(
                session, tmux_name, pane_index, excluding=request_id
            )

            # Broadcast permission_resolved to all clients (Task 3.7)
            await self._broadcast(session, {
                "type": "permission_resolved",
                "decision": decision,
                "request_id": request_id,
            })

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Permission respond failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _clear_marker_if_unshared(
        self, session, tmux_name: str, pane_index: int, excluding: str = ""
    ) -> None:
        """Clear the pane's prompt marker unless another ask still uses it.

        Concurrent asks on one pane share one marker; resolving one must not
        drop the marker the others still need (#1343).
        """
        shared = any(
            rid != excluding and p.pane_index == pane_index
            for rid, p in session.pending_permissions.items()
        )
        if not shared:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: prompt_router.clear_marker(tmux_name, pane_index)
            )

    async def api_permissions_list(self, request: web.Request) -> web.Response:
        """GET /api/permissions - every pending approval, across sessions.

        Survives a missed ``permission_request`` broadcast: a client that
        connects late (or the owner who only heard a TTS line) can still see
        and answer what is waiting (#1343).
        """
        pending = [
            {
                "session": name,
                "request_id": p.request_id,
                "tool_name": p.request.get("tool_name", "unknown"),
                "tool_input": p.request.get("tool_input", {}),
                "agent": prompt_router.agent_label(p.request),
                "summary": summarize(name, p.request),
                "agent_id": p.request.get("agent_id"),
                "agent_type": p.request.get("agent_type"),
                "pane_index": p.pane_index,
            }
            for name, sess in self.active_sessions.items()
            for p in sess.pending_permissions.values()
        ]
        return web.json_response({"success": True, "pending": pending})

    async def _announce_permission_request(
        self, session_name: str, data: dict, parent_notified: "str | None" = None,
    ):
        """Speak the plain-language summary of the ask (#1343)."""
        text = summarize(session_name, data)
        if parent_notified:
            # The human knows an agent may handle it before they get there.
            text += f" Also routed to {parent_notified}."
        await self._say_to_room(session_name, text)


def register_permission_routes(server, app):
    """Wire the permission + review domain's routes onto ``app``.

    Route ordering is load-bearing: aiohttp matches in registration order, so
    the more-specific ``/respond`` and ``/answer`` routes MUST precede their
    bare ``{name:.+}`` siblings.
    """
    # Mobile Review window: structured diff + tap-to-approve/deny
    app.router.add_get("/api/review/{name:.+}", server.api_review)
    app.router.add_post("/api/review/{name:.+}/answer", server.api_review_answer)
    # Permission request handling (from Claude Code hook)
    # Note: respond route must come first as aiohttp matches in order
    app.router.add_get("/api/permissions", server.api_permissions_list)
    app.router.add_post("/api/permission/{name:.+}/respond", server.api_permission_respond)
    app.router.add_post("/api/permission/{name:.+}", server.api_permission_request)
