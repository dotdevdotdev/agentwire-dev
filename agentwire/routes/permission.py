"""Portal routes — permission + review domain.

Part of the #560 server.py split. Handlers moved verbatim from
``AgentWireServer``; they depend on stdlib helpers plus ``prompt_router`` /
``parse_session_name`` and on core attributes (``self.active_sessions``,
``self._get_session_config``, ``self._broadcast``, ``self._say_to_room``,
``self.run_agentwire_cmd``), which resolve through the MRO of the composed
server class.

Review and permission are grouped because the Review window's ``_live_prompt``
helper is unique to ``api_review``. ``PendingPermission`` /
``_is_allowed_in_restricted_mode`` live on the base server module and are
imported lazily inside the handlers to avoid a circular import.
"""

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from .. import prompt_router
from ..worktree import parse_session_name

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

        In restricted mode, only say commands are auto-allowed,
        everything else is auto-denied silently.
        """
        from ..server import PendingPermission, _is_allowed_in_restricted_mode

        name = request.match_info["name"]
        try:
            data = await request.json()
            tool_name = data.get("tool_name", "unknown")
            tool_input = data.get("tool_input", {})
            message = data.get("message", "")

            logger.info(f"[{name}] Permission request: {tool_name}")

            # Ensure session exists
            session = await self._get_or_create_session(name)

            # Check restricted mode - auto-handle without user interaction
            if session.config.type == "claude-restricted":
                # Parse session name to handle local vs remote
                project, branch, machine = parse_session_name(name)
                if branch:
                    tmux_session = f"{project}/{branch}".replace(".", "_")
                else:
                    tmux_session = project.replace(".", "_")

                if _is_allowed_in_restricted_mode(tool_name, tool_input):
                    # Auto-allow
                    logger.info(f"[{name}] Restricted mode: auto-allowing {tool_name}")
                    # Only send keystroke for Bash commands (say)
                    # AskUserQuestion doesn't need permission keystroke
                    if tool_name == "Bash":
                        # Use CLI for consistent behavior (handles local and remote)
                        # Send "1" to select "Yes" option in permission prompt
                        session_target = f"{tmux_session}@{machine}" if machine else tmux_session
                        result = await self._run_subprocess(
                            ["agentwire", "send-keys", "-s", session_target, "1"]
                        )
                        if result is None or result[0] != 0:
                            logger.error(f"[{name}] Failed to send allow keystroke")
                    return web.json_response({"decision": "allow_always"})
                else:
                    # Auto-deny: send "Escape" keystroke (deny silently)
                    logger.info(f"[{name}] Restricted mode: auto-denying {tool_name}")
                    # Use CLI for consistent behavior (handles local and remote)
                    session_target = f"{tmux_session}@{machine}" if machine else tmux_session
                    result = await self._run_subprocess(
                        ["agentwire", "send-keys", "-s", session_target, "Escape"]
                    )
                    if result is None or result[0] != 0:
                        logger.error(f"[{name}] Failed to send deny keystroke")
                    return web.json_response({
                        "decision": "deny",
                        "message": "Restricted mode: only say commands are allowed"
                    })

            # Create pending permission request (normal/prompted mode)
            try:
                pane_index = int(data.get("pane_index") or 0)
            except (TypeError, ValueError):
                pane_index = 0
            session.pending_permission = PendingPermission(request=data, pane_index=pane_index)

            # Route to the parent/orchestrator session, if one resolves
            # (#276). Best-effort and non-blocking for the dialog itself;
            # placed AFTER the restricted-mode branch so auto-denied tools
            # never spam a parent. The hook payload's tmux_session (the real
            # tmux name) beats the URL name, which may be a project alias.
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
            })

            # Generate TTS announcement (Task 3.6)
            await self._announce_permission_request(
                name, tool_name, tool_input, parent_notified=parent_notified
            )

            # Wait for user decision with 5 minute timeout
            try:
                await asyncio.wait_for(session.pending_permission.event.wait(), timeout=300)
            except asyncio.TimeoutError:
                logger.warning(f"[{name}] Permission request timed out")
                session.pending_permission = None
                await self._broadcast(session, {"type": "permission_timeout"})
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: prompt_router.clear_marker(tmux_name, pane_index)
                )
                return web.json_response({
                    "decision": "deny",
                    "message": "Permission request timed out (5 minutes)"
                })

            # Return the decision to the hook script
            decision = session.pending_permission.decision
            session.pending_permission = None
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: prompt_router.clear_marker(tmux_name, pane_index)
            )

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

            if not session.pending_permission:
                return web.json_response({"error": "No pending permission request"}, status=400)

            # Capture pane/session context BEFORE signaling — the waiting
            # request handler nulls pending_permission as soon as it wakes.
            pane_index = session.pending_permission.pane_index
            pending_request = session.pending_permission.request
            tmux_name = str(pending_request.get("tmux_session") or "") or name

            # Store decision and signal the waiting request
            session.pending_permission.decision = {"decision": decision}
            if decision == "deny":
                session.pending_permission.decision["message"] = data.get("message", "User denied permission")
            session.pending_permission.event.set()

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

            await asyncio.get_event_loop().run_in_executor(
                None, lambda: prompt_router.clear_marker(tmux_name, pane_index)
            )

            # Broadcast permission_resolved to all clients (Task 3.7)
            await self._broadcast(session, {
                "type": "permission_resolved",
                "decision": decision,
            })

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Permission respond failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _announce_permission_request(
        self, session_name: str, tool_name: str, tool_input: dict,
        parent_notified: "str | None" = None,
    ):
        """Generate TTS announcement for permission request (Task 3.6)."""
        # Build a natural announcement message
        if tool_name == "Edit":
            file_path = tool_input.get("file_path", "a file")
            # Extract just the filename for brevity
            filename = Path(file_path).name if file_path else "a file"
            text = f"Claude wants to edit {filename}"
        elif tool_name == "Write":
            file_path = tool_input.get("file_path", "a file")
            filename = Path(file_path).name if file_path else "a file"
            text = f"Claude wants to write to {filename}"
        elif tool_name == "Bash":
            command = tool_input.get("command", "")
            # Truncate long commands
            if len(command) > 50:
                command = command[:47] + "..."
            text = f"Claude wants to run a command: {command}"
        else:
            text = f"Claude wants to use {tool_name}"

        if parent_notified:
            # The human knows an agent may handle it before they get there.
            text += f". Also routed to {parent_notified}"

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
    app.router.add_post("/api/permission/{name:.+}/respond", server.api_permission_respond)
    app.router.add_post("/api/permission/{name:.+}", server.api_permission_request)
