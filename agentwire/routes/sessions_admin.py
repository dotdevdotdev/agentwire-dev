"""Portal routes — sessions domain, MUTATE handlers (PR-B of 2).

Part of the #560 / #593 server.py split. Handlers moved verbatim from
``AgentWireServer``; they depend on core attributes and helpers
(``self.run_agentwire_cmd``, ``self.active_sessions``, ``self.broadcast_dashboard``,
``self._get_sessions_data``, ``self._get_session_config``, ``self._get_session_cwd``,
``self._read_agentwire_yaml`` / ``self._write_agentwire_yaml``,
``self._deliver_first_message``, ``self._wait_for_pane_ready``,
``self._get_system_session_names`` / ``self._is_system_session``, ``self.agent``),
which stay on the base server and resolve through the MRO of the composed server
class. ``Session`` is imported lazily from ``..server`` inside the handler that
needs it to avoid a circular import at module load. Only the mutate handlers
move here; the read handlers live in ``sessions.py`` (PR-A).
"""

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path

from aiohttp import web

from ..worktree import parse_session_name

logger = logging.getLogger(__name__)


class SessionsAdminRoutesMixin:
    async def api_active_session(self, request: web.Request) -> web.Response:
        """Record which session the portal desktop is currently focused on.

        The frontend POSTs the focused session name whenever a session window
        gains focus. We mirror it to ``~/.agentwire/active-session`` so external
        tools (e.g. the Hammerspoon ⌥Space "tab target" hotkey) can read which
        session voice input should land in — "voice follows the focused tab".

        Body:
            {"session": "agentwire-dev"}

        Response:
            {"success": true, "session": "..."}
            {"success": false, "error": "..."}
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON body"}, status=400)

        session = (data.get("session") or "").strip()
        if not session:
            return web.json_response({"success": False, "error": "session is required"}, status=400)

        try:
            target = Path.home() / ".agentwire" / "active-session"
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: temp file in the same dir + os.replace so a reader
            # never sees a half-written or empty file.
            tmp = target.with_suffix(".tmp")
            tmp.write_text(session + "\n", encoding="utf-8")
            os.replace(tmp, target)
        except OSError as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

        return web.json_response({"success": True, "session": session})

    async def api_create_session(self, request: web.Request) -> web.Response:
        """Create a new agent session via CLI.

        Request body:
            name: Base session/project name (required)
            path: Custom project path (optional, ignored if worktree=true)
            voice: TTS voice for this session
            type: Session type (claude-bypass | claude-bypass | ...)
            roles: Comma-separated list of roles (e.g., "agentwire,worker")
            machine: Machine ID ('local' or remote machine ID)
            worktree: Whether to create a worktree session
            branch: Branch name for worktree sessions
            first_message: Deliver this as the agent's first message once it
                boots (background, verified paste; local sessions only)

        Session naming:
            - worktree + branch: project/branch (or project/branch@machine)
            - just machine: name@machine
            - neither: just name
        """
        try:
            data = await request.json()
            name = data.get("name", "").strip()
            custom_path = data.get("path")
            voice = data.get("voice", self.config.tts.default_voice)
            # Posture is the canonical axis; a legacy fused `type` is still
            # accepted. When posture is present it wins.
            posture = (data.get("posture") or "").strip()
            session_type = data.get("type") if not posture else None
            roles = data.get("roles")
            machine = data.get("machine", "local")
            worktree = data.get("worktree", False)
            branch = data.get("branch", "").strip()
            base = (data.get("base") or "main").strip() or "main"
            pull_first = bool(data.get("pull_first", True))
            first_message = (data.get("first_message") or "").strip()

            if not name:
                return web.json_response({"error": "Session name is required"})

            if first_message and machine and machine != "local":
                # Explicit reject, not silent skip — readiness capture is local-only
                return web.json_response({"error": "first_message is only supported on local sessions"})

            # Build session name for CLI based on parameters
            if machine and machine != "local":
                # Remote session
                if worktree and branch:
                    cli_session = f"{name}/{branch}@{machine}"
                else:
                    cli_session = f"{name}@{machine}"
            else:
                # Local session
                if worktree and branch:
                    cli_session = f"{name}/{branch}"
                else:
                    cli_session = name

            # Build CLI args
            args = ["new", "-s", cli_session]
            # Pass -p when provided (CLI uses it to locate repo for worktree creation)
            if custom_path:
                args.extend(["-p", custom_path])
            # Session type: posture when given, else legacy --type.
            if posture:
                args.extend(["--posture", posture])
            if session_type:
                args.extend(["--type", session_type])
            # Worktree-only flags: base branch + pull-first behaviour
            if worktree and branch:
                args.extend(["--base", base])
                args.append("--pull-first" if pull_first else "--no-pull-first")
            # Set roles if provided (handle both array and string formats)
            if roles:
                # Validate roles exist before passing to CLI
                if isinstance(roles, list):
                    roles_list = roles
                else:
                    roles_list = [r.strip() for r in roles.split(",") if r.strip()]

                # Get available roles
                success, result = await self.run_agentwire_cmd(["roles", "list"])
                available_roles = set()
                if success:
                    for role in result.get("roles", []):
                        available_roles.add(role.get("name"))

                # Filter to only valid roles. If none survive, pass nothing —
                # the CLI injects the verb's intrinsic etiquette (orchestrator)
                # on its own; there is no global default-role to fall back to.
                valid_roles = [r for r in roles_list if r in available_roles]
                if not valid_roles and roles_list:
                    logger.warning(f"No valid roles found in {roles_list}, deferring to intrinsic etiquette")

                if valid_roles:
                    args.extend(["--roles", ",".join(valid_roles)])

            # Call CLI
            logger.info(f"Creating session with args: {args}")
            success, result = await self.run_agentwire_cmd(args)
            logger.info(f"CLI result: success={success}, result={result}")

            if not success:
                error_msg = result.get("error", "Failed to create session")
                return web.json_response({"error": error_msg})

            session_name = result.get("session", cli_session)
            session_path = result.get("path")

            # CLI writes .agentwire.yml with type
            # If user explicitly selected a voice, update it
            if session_path and voice != self.config.tts.default_voice:
                # Parse session name for machine
                machine_id = None
                if "@" in session_name:
                    _, machine_id = session_name.rsplit("@", 1)

                # Read and update .agentwire.yml
                yaml_config = await self._read_agentwire_yaml(session_path, machine_id) or {}
                yaml_config["voice"] = voice
                await self._write_agentwire_yaml(session_path, yaml_config, machine_id)

            # Broadcast session created to dashboard clients
            await self.broadcast_dashboard("session_created", {"session": session_name})
            sessions_data = await self._get_sessions_data()
            await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

            # Wait until the tmux pane has actually rendered something. The CLI
            # returns the moment `tmux send-keys` *queues* the agent command, so
            # the WS attach can race the agent's startup and show a disconnect
            # overlay even though the session is healthy. Polling `capture-pane`
            # is event-driven (we return the instant there's output) and bounded
            # at ~2s so we never block the UI for long. Skip on remote sessions
            # — tmux lives on the other side of SSH there.
            if "@" not in session_name:
                await self._wait_for_pane_ready(session_name)

            # First-message delivery happens in the background so the window
            # can open immediately — the user watches the idea land live.
            if first_message:
                task = asyncio.create_task(
                    self._deliver_first_message(session_name, first_message))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

            return web.json_response({
                "success": True,
                "name": session_name,
                "first_message": "pending" if first_message else None,
            })

        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            return web.json_response({"error": str(e)})

    async def api_close_session(self, request: web.Request) -> web.Response:
        """Close/kill a session."""
        name = request.match_info["name"]
        try:
            # Kill the tmux session via CLI (handles local and remote)
            success, result = await self.run_agentwire_cmd(["kill", "-s", name])
            if not success:
                error_msg = result.get("error", "Failed to close session")
                return web.json_response({"error": error_msg})

            # Clean up session if exists
            if name in self.active_sessions:
                session = self.active_sessions[name]
                if session.output_task:
                    session.output_task.cancel()
                del self.active_sessions[name]

            # Broadcast session closed to dashboard clients
            await self.broadcast_dashboard("session_closed", {"session": name})
            sessions_data = await self._get_sessions_data()
            await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Failed to close session: {e}")
            return web.json_response({"error": str(e)})

    async def api_session_config(self, request: web.Request) -> web.Response:
        """Update session configuration (voice only).

        Edits the project's .agentwire.yml directly.
        """
        name = request.match_info["name"]
        try:
            data = await request.json()

            # Only voice is configurable via UI now
            if "voice" not in data:
                return web.json_response({"error": "No voice specified"}, status=400)

            voice = data["voice"]

            # Parse session name for machine
            machine_id = None
            base_name = name
            if "@" in name:
                base_name, machine_id = name.rsplit("@", 1)

            # Get session's working directory
            cwd = await self._get_session_cwd(base_name, machine_id)
            if not cwd:
                return web.json_response({"error": "Session working directory not found"}, status=404)

            # Read existing .agentwire.yml (or create new)
            yaml_config = await self._read_agentwire_yaml(cwd, machine_id) or {}

            # Update voice
            yaml_config["voice"] = voice

            # Write back
            if not await self._write_agentwire_yaml(cwd, yaml_config, machine_id):
                return web.json_response({"error": "Failed to write .agentwire.yml"}, status=500)

            # Update live session if exists
            if name in self.active_sessions:
                self.active_sessions[name].config.voice = voice

            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_refresh_sessions(self, request: web.Request) -> web.Response:
        """Refresh sessions and broadcast update to all dashboard clients.

        Called by CLI commands (like `agentwire kill`) to notify portal of changes.
        """
        try:
            sessions_data = await self._get_sessions_data()
            await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})
            return web.json_response({
                "success": True,
                "sessions": len(sessions_data),
            })
        except Exception as e:
            logger.error(f"Failed to refresh sessions: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_recreate_session(self, request: web.Request) -> web.Response:
        """POST /api/session/{name}/recreate - Destroy session/worktree and create fresh one via CLI.

        Inherits session type from existing session config.
        Supported types: claude-bypass | claude-prompted | claude-restricted | claude-auto | bare
        """
        name = request.match_info["name"]
        try:
            logger.info(f"[{name}] Recreating session...")

            # Get old config for inheriting settings (before CLI deletes it)
            old_config = await self._get_session_config(name)

            # Build CLI args
            args = ["recreate", "-s", name]
            # Set session type via --type flag
            args.extend(["--type", old_config.type])

            # Call CLI - handles kill, worktree removal, git pull, new worktree, new session
            success, result = await self.run_agentwire_cmd(args)

            if not success:
                error_msg = result.get("error", "Failed to recreate session")
                return web.json_response({"error": error_msg}, status=500)

            new_session_name = result.get("session", name)
            session_path = result.get("path")

            # Clean up old session state
            if name in self.active_sessions:
                session = self.active_sessions[name]
                if session.output_task:
                    session.output_task.cancel()
                del self.active_sessions[name]

            # CLI writes .agentwire.yml with type; update voice if the old session had one
            if session_path and old_config.voice != self.config.tts.default_voice:
                machine_id = None
                if "@" in new_session_name:
                    _, machine_id = new_session_name.rsplit("@", 1)
                yaml_config = await self._read_agentwire_yaml(session_path, machine_id) or {}
                yaml_config["voice"] = old_config.voice
                await self._write_agentwire_yaml(session_path, yaml_config, machine_id)

            logger.info(f"[{name}] Session recreated as '{new_session_name}'")
            return web.json_response({"success": True, "session": new_session_name})

        except Exception as e:
            logger.error(f"Recreate session API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_spawn_sibling(self, request: web.Request) -> web.Response:
        """POST /api/session/{name}/spawn-sibling - Create a new session in same project via CLI.

        Creates a parallel session in a new worktree without destroying the current one.
        Useful for working on multiple features in the same project simultaneously.

        Inherits session type from existing session config.
        Supported types: claude-bypass | claude-prompted | claude-restricted | claude-auto | bare
        """
        name = request.match_info["name"]
        try:
            logger.info(f"[{name}] Spawning sibling session...")

            # Parse session name to get project and machine
            project, _, machine = parse_session_name(name)

            # Get old config for inheriting settings
            old_config = await self._get_session_config(name)

            # Build new session name: project/session-<timestamp>[@machine]
            new_branch = f"session-{int(time.time())}"
            new_session_name = f"{project}/{new_branch}"
            if machine:
                new_session_name = f"{new_session_name}@{machine}"

            # Build CLI args - use `agentwire new` with the sibling session name
            args = ["new", "-s", new_session_name]
            # Set session type via --type flag
            args.extend(["--type", old_config.type])

            # Call CLI - handles worktree creation and session setup
            success, result = await self.run_agentwire_cmd(args)

            if not success:
                error_msg = result.get("error", "Failed to create sibling session")
                return web.json_response({"error": error_msg}, status=500)

            session_name = result.get("session", new_session_name)
            session_path = result.get("path")

            # CLI writes .agentwire.yml with type; update voice if the old session had one
            if session_path and old_config.voice != self.config.tts.default_voice:
                machine_id = machine
                yaml_config = await self._read_agentwire_yaml(session_path, machine_id) or {}
                yaml_config["voice"] = old_config.voice
                await self._write_agentwire_yaml(session_path, yaml_config, machine_id)

            logger.info(f"[{name}] Sibling session created: '{session_name}'")
            return web.json_response({"success": True, "session": session_name})

        except Exception as e:
            logger.error(f"Spawn sibling API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_fork_session(self, request: web.Request) -> web.Response:
        """POST /api/session/{name}/fork - Fork the Claude Code session via CLI.

        Creates a new session that continues from the current conversation context.

        Inherits session type from existing session config.
        Supported types: claude-bypass | claude-prompted | claude-restricted | claude-auto | bare
        """
        name = request.match_info["name"]
        try:
            # Get current session config for inheriting settings
            session_config = await self._get_session_config(name)

            logger.info(f"[{name}] Forking session...")

            # Parse session name to get project and machine
            project, _, machine = parse_session_name(name)

            # Find next available fork number for target name
            # Just check if tmux session exists (no cache to check)
            fork_num = 1
            while True:
                candidate = f"{project}-fork-{fork_num}"
                if machine:
                    candidate = f"{candidate}@{machine}"
                if not self.agent.session_exists(candidate):
                    break
                fork_num += 1

            # Build target session name: project/fork-N[@machine]
            new_branch = f"fork-{fork_num}"
            target_session = f"{project}/{new_branch}"
            if machine:
                target_session = f"{target_session}@{machine}"

            # Build CLI args
            args = ["fork", "-s", name, "-t", target_session]
            # Set session type via --type flag
            args.extend(["--type", session_config.type])

            # Call CLI - handles worktree creation and session setup
            success, result = await self.run_agentwire_cmd(args)

            if not success:
                error_msg = result.get("error", "Failed to fork session")
                return web.json_response({"error": error_msg}, status=500)

            session_name = result.get("session", target_session)
            session_path = result.get("path")

            # CLI writes .agentwire.yml with type; update voice if the old session had one
            if session_path and session_config.voice != self.config.tts.default_voice:
                machine_id = machine
                yaml_config = await self._read_agentwire_yaml(session_path, machine_id) or {}
                yaml_config["voice"] = session_config.voice
                await self._write_agentwire_yaml(session_path, yaml_config, machine_id)

            logger.info(f"[{name}] Session forked as '{session_name}'")
            return web.json_response({"success": True, "session": session_name})

        except Exception as e:
            logger.error(f"Fork session API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_session_broadcast(self, request: web.Request) -> web.Response:
        """POST /api/session/{name}/broadcast - Broadcast event to session WebSocket clients.

        Used by channels (Discord, Slack) to receive outbound events from sessions.

        Request body: JSON with at least a "type" field.
        Common types: "alert" (text), "question" (question, options), "audio" (audio base64).
        """
        name = request.match_info["name"]
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        # Find or create a session object to broadcast through
        session = await self._get_or_create_session(name)

        await self._broadcast(session, data)
        return web.json_response({"success": True})

    async def api_restart_service(self, request: web.Request) -> web.Response:
        """POST /api/session/{name}/restart-service - Restart a system service.

        For system sessions (portal, tts, main), this properly restarts the service.
        Session names are configurable via services.*.session_name in config.
        """
        name = request.match_info["name"]
        base_name = name.split("@")[0]
        session_names = self._get_system_session_names()

        if not self._is_system_session(name):
            return web.json_response(
                {"error": f"'{name}' is not a system session"},
                status=400
            )

        try:
            logger.info(f"[{name}] Restarting service...")
            portal_session = session_names["portal"]
            tts_session = session_names["tts"]
            main_session = session_names["main"]

            if base_name == portal_session:
                # Special case: we are the portal, need to restart ourselves
                # Schedule restart after responding
                # Can't use `agentwire portal start` as it tries to attach to terminal
                async def delayed_restart():
                    await asyncio.sleep(1)
                    logger.info("Portal restarting...")
                    # Kill the tmux session (which kills us)
                    await self._run_subprocess(
                        ["tmux", "kill-session", "-t", portal_session]
                    )
                    await asyncio.sleep(0.5)
                    # Create new tmux session with portal serve command
                    await self._run_subprocess(
                        ["tmux", "new-session", "-d", "-s", portal_session]
                    )
                    await self._run_subprocess(
                        ["tmux", "send-keys", "-t", portal_session,
                         "agentwire portal serve", "Enter"]
                    )

                asyncio.create_task(delayed_restart())
                return web.json_response({
                    "success": True,
                    "message": "Portal restarting in 1 second..."
                })

            elif base_name == tts_session:
                # Restart TTS server
                await self._run_subprocess(["agentwire", "tts", "stop"])
                await asyncio.sleep(0.5)
                subprocess.Popen(
                    ["agentwire", "tts", "start"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return web.json_response({
                    "success": True,
                    "message": "TTS server restarted"
                })

            elif base_name == main_session:
                # Restart the agentwire session - kill Claude and restart it
                self.agent.send_keys(name, "/exit")
                await asyncio.sleep(1)

                # Send the agent command to restart Claude
                agent_cmd = self.agent.agent_command
                self.agent.send_input(name, agent_cmd)

                return web.json_response({
                    "success": True,
                    "message": "Agentwire session restarted"
                })

            return web.json_response({"error": "Unknown system session"}, status=400)

        except Exception as e:
            logger.error(f"Restart service API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)


def register_sessions_admin_routes(server, app):
    """Wire the sessions domain's mutate routes onto ``app``."""
    app.router.add_post("/api/create", server.api_create_session)
    app.router.add_post("/api/active-session", server.api_active_session)
    app.router.add_post("/api/session/{name:.+}/config", server.api_session_config)
    app.router.add_post("/api/session/{name:.+}/recreate", server.api_recreate_session)
    app.router.add_post("/api/session/{name:.+}/spawn-sibling", server.api_spawn_sibling)
    app.router.add_post("/api/session/{name:.+}/fork", server.api_fork_session)
    app.router.add_post("/api/session/{name:.+}/restart-service", server.api_restart_service)
    app.router.add_post("/api/session/{name:.+}/broadcast", server.api_session_broadcast)
    app.router.add_delete("/api/sessions/{name:.+}", server.api_close_session)
    app.router.add_post("/api/sessions/refresh", server.api_refresh_sessions)
