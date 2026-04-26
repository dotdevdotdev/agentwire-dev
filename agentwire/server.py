"""
AgentWire WebSocket server.

Multi-session voice web interface for AI coding agents.
"""

import asyncio
import base64
import fcntl
import json
import logging
import os
import pty
import random
import re
import shlex
import signal
import socket
import ssl
import struct
import subprocess
import tempfile
import termios
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import aiohttp_jinja2
import jinja2
from aiohttp import web

from .config import Config, load_config
from .worktree import parse_session_name
from .cached_status import CachedStatusChecker

__version__ = "1.3.0"

logger = logging.getLogger(__name__)

# Paste chunking: large inputs (pastes) are written in chunks with delays
# to avoid flooding the PTY buffer and freezing the agent session.
PASTE_THRESHOLD = 64    # bytes — above this, chunk the write
PASTE_CHUNK_SIZE = 128  # bytes per write
PASTE_CHUNK_DELAY = 0.01  # seconds between chunks


def _is_allowed_in_restricted_mode(tool_name: str, tool_input: dict) -> bool:
    """Check if command is allowed in restricted mode.

    Allows:
    - AskUserQuestion tool (for interactive prompts)
    - Bash: say "message"

    Rejects any shell operators, redirects, or multi-line commands.
    """
    # Allow AskUserQuestion tool
    if tool_name == "AskUserQuestion":
        return True

    if tool_name != "Bash":
        return False

    command = tool_input.get("command", "").strip()

    # Reject multi-line commands immediately
    if '\n' in command:
        return False

    # Match: say or agentwire say followed by quoted string (optional & for background)
    # Allows: say "hello world"
    #         say 'hello world'
    #         agentwire say "hello world"
    #         agentwire say "hello world" &
    #         agentwire say -s session "hello world"
    # Rejects: say "hi" && rm -rf /
    #          say "hi" > /tmp/log
    #          say $(cat /etc/passwd)
    pattern = r'^(?:agentwire\s+)?say\s+(?:-[sv]\s+\S+\s+)*(["\']).*\1\s*&?\s*$'

    return bool(re.match(pattern, command))


@dataclass
class SessionConfig:
    """Runtime configuration for a session."""

    voice: str = "default"
    exaggeration: float = 0.5
    cfg_weight: float = 0.5
    machine: str | None = None
    path: str | None = None
    claude_session_id: str | None = None  # Claude Code session UUID for forking
    type: str = "claude-bypass"  # Session type: bare | claude-bypass | claude-prompted | claude-restricted
    roles: list = None  # Composable roles array
    spawned_by: str | None = None  # Parent session (for worker sessions)

    def __post_init__(self):
        if self.roles is None:
            self.roles = []


@dataclass
class PendingPermission:
    """A permission request waiting for user decision."""

    request: dict  # The permission request from Claude Code
    event: asyncio.Event = field(default_factory=asyncio.Event)  # Signals when user responds
    decision: dict | None = None  # The user's decision


@dataclass
class Session:
    """Active session with connected clients."""

    name: str
    config: SessionConfig
    clients: set = field(default_factory=set)
    locked_by: str | None = None
    last_output: str = ""
    output_task: asyncio.Task | None = None
    played_says: set = field(default_factory=set)
    last_question: str | None = None  # Track AskUserQuestion to avoid duplicates
    pending_permission: PendingPermission | None = None  # Active permission request
    last_output_timestamp: float = 0.0  # Last time output changed (server-side activity tracking)
    is_active: bool = False  # Current active/idle state for transition detection


class AgentWireServer:
    """Main server managing sessions, WebSockets, and agent backends."""

    def __init__(self, config: Config):
        self.config = config
        self.active_sessions: dict[str, Session] = {}  # Active sessions with connected clients
        self.session_activity: dict[str, dict] = {}  # Global activity tracking for all sessions
        self.dashboard_clients: set = set()  # WebSocket clients for dashboard updates
        self.session_client_counts: dict[str, int] = {}  # Attached tmux client counts per session
        self.active_notifications: dict[str, dict] = {}  # id -> notification for persistence across refresh
        self.machine_status_checker = CachedStatusChecker(ttl_seconds=30)  # Progressive loading for machines
        self.remote_sessions_checker = CachedStatusChecker(ttl_seconds=20)  # Progressive loading for remote sessions
        self.projects_checker = CachedStatusChecker(ttl_seconds=30)  # Progressive loading for projects
        self.stt = None
        self.agent = None
        self._http_session: aiohttp.ClientSession | None = None  # For TTS HTTP calls
        self.app = web.Application()
        self._setup_jinja2()
        self._setup_routes()

    def _setup_jinja2(self):
        """Configure Jinja2 template environment."""
        templates_dir = Path(__file__).parent / "templates"
        aiohttp_jinja2.setup(
            self.app,
            loader=jinja2.FileSystemLoader(str(templates_dir)),
            autoescape=jinja2.select_autoescape(["html", "xml"]),
        )

    def _setup_routes(self):
        """Configure HTTP and WebSocket routes."""
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/ws", self.handle_dashboard_ws)
        self.app.router.add_get("/ws/{name:.+}", self.handle_websocket)
        self.app.router.add_get("/ws/terminal/{name:.+}", self.handle_terminal_ws)
        self.app.router.add_get("/api/sessions", self.api_sessions)
        self.app.router.add_get("/api/sessions/local", self.api_sessions_local)
        self.app.router.add_get("/api/sessions/remote", self.api_sessions_remote)
        self.app.router.add_get("/api/sdk-sessions", self.api_sdk_sessions)
        self.app.router.add_get("/ws/sdk-watch/{name}", self.handle_sdk_watch_ws)
        self.app.router.add_get("/api/projects", self.api_projects)
        self.app.router.add_post("/api/projects/delete", self.api_projects_delete)
        self.app.router.add_get("/api/roles", self.api_roles)
        self.app.router.add_get("/api/machine/{machine_id}/status", self.api_machine_status)
        self.app.router.add_get("/api/check-path", self.api_check_path)
        self.app.router.add_get("/api/check-branches", self.api_check_branches)
        self.app.router.add_post("/api/create", self.api_create_session)
        self.app.router.add_post("/api/session/{name:.+}/config", self.api_session_config)
        self.app.router.add_post("/transcribe", self.handle_transcribe)
        self.app.router.add_post("/upload", self.handle_upload)
        self.app.router.add_post("/send/{name:.+}", self.handle_send)
        self.app.router.add_post("/api/say/{name:.+}", self.api_say)
        self.app.router.add_get("/api/sessions/{name:.+}/connections", self.api_session_connections)
        self.app.router.add_post("/api/local-tts/{name:.+}", self.api_local_tts)
        self.app.router.add_post("/api/answer/{name:.+}", self.api_answer)
        self.app.router.add_post("/api/session/{name:.+}/recreate", self.api_recreate_session)
        self.app.router.add_post("/api/session/{name:.+}/spawn-sibling", self.api_spawn_sibling)
        self.app.router.add_post("/api/session/{name:.+}/fork", self.api_fork_session)
        self.app.router.add_post("/api/session/{name:.+}/restart-service", self.api_restart_service)
        self.app.router.add_post("/api/session/{name:.+}/broadcast", self.api_session_broadcast)
        self.app.router.add_get("/api/voices", self.api_voices)
        self.app.router.add_delete("/api/sessions/{name:.+}", self.api_close_session)
        self.app.router.add_get("/api/machines", self.api_machines)
        self.app.router.add_post("/api/machines", self.api_add_machine)
        self.app.router.add_delete("/api/machines/{machine_id}", self.api_remove_machine)
        self.app.router.add_get("/api/config", self.api_get_config)
        self.app.router.add_post("/api/config", self.api_save_config)
        self.app.router.add_post("/api/config/reload", self.api_reload_config)
        self.app.router.add_post("/api/sessions/refresh", self.api_refresh_sessions)
        # Icon listing for dynamic icon picker
        self.app.router.add_get("/api/icons/{category}", self.api_icons)
        # Permission request handling (from Claude Code hook)
        # Note: respond route must come first as aiohttp matches in order
        self.app.router.add_post("/api/permission/{name:.+}/respond", self.api_permission_respond)
        self.app.router.add_post("/api/permission/{name:.+}", self.api_permission_request)
        # History endpoints
        self.app.router.add_get("/api/history", self.api_history_list)
        self.app.router.add_get("/api/history/{session_id}", self.api_history_detail)
        self.app.router.add_post("/api/history/{session_id}/resume", self.api_history_resume)
        # Tmux hook notifications
        self.app.router.add_post("/api/notify", self.api_notify)
        # Desktop UI control (for MCP agents)
        self.app.router.add_get("/api/desktop/windows", self.api_desktop_windows)
        self.app.router.add_post("/api/desktop/window/open", self.api_desktop_open)
        self.app.router.add_post("/api/desktop/window/close", self.api_desktop_close)
        self.app.router.add_post("/api/desktop/window/focus", self.api_desktop_focus)
        self.app.router.add_post("/api/desktop/window/tile", self.api_desktop_tile)
        self.app.router.add_post("/api/desktop/window/minimize-all", self.api_desktop_minimize_all)
        self.app.router.add_post("/api/desktop/layout", self.api_desktop_layout)
        # Desktop notifications
        self.app.router.add_post("/api/desktop/notification", self.api_desktop_notification)
        self.app.router.add_post("/api/desktop/notification/dismiss", self.api_desktop_notification_dismiss)
        self.app.router.add_get("/api/desktop/notifications", self.api_desktop_notifications_list)
        # Scheduler monitoring endpoints
        self.app.router.add_get("/api/scheduler/live", self.api_scheduler_live)
        self.app.router.add_get("/api/scheduler/events", self.api_scheduler_events)
        self.app.router.add_get("/api/scheduler/board", self.api_scheduler_board)
        self.app.router.add_post("/api/scheduler/tasks/{name}/enable", self.api_scheduler_task_enable)
        self.app.router.add_post("/api/scheduler/tasks/{name}/disable", self.api_scheduler_task_disable)
        self.app.router.add_post("/api/scheduler/tasks/{name}/run", self.api_scheduler_task_run)
        self.app.router.add_get("/api/scheduler/tasks/{name}/events", self.api_scheduler_task_events)
        self.app.router.add_post("/api/scheduler/start", self.api_scheduler_start)
        self.app.router.add_post("/api/scheduler/stop", self.api_scheduler_stop)
        self.app.router.add_get("/api/scheduler/output", self.api_scheduler_session_output)
        # Workflow history endpoints
        self.app.router.add_get("/api/workflows/runs", self.api_workflows_runs_list)
        self.app.router.add_get("/api/workflows/runs/{run_id}", self.api_workflows_run_detail)
        # Artifact windows: upload and serve agent-generated HTML
        self.app.router.add_post("/api/artifacts/upload", self.api_artifacts_upload)
        self.app.router.add_get("/api/artifacts", self.api_artifacts_list)
        self.app.router.add_delete("/api/artifacts/{filename:.+}", self.api_artifacts_delete)
        artifacts_dir = self.config.artifacts.dir
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.app.router.add_static("/artifacts", artifacts_dir)
        self.app.router.add_static("/static", Path(__file__).parent / "static")

    async def init_backends(self):
        """Initialize TTS, STT, and agent backends."""
        # Convert config to dict for backend factories
        config_dict = {
            "tts": {
                "backend": self.config.tts.backend,
                "url": self.config.tts.url,
                "exaggeration": self.config.tts.exaggeration,
                "cfg_weight": self.config.tts.cfg_weight,
                "runpod_endpoint_id": self.config.tts.runpod_endpoint_id,
                "runpod_api_key": self.config.tts.runpod_api_key,
                "runpod_timeout": self.config.tts.runpod_timeout,
            },
            "stt": {
                "url": self.config.stt.url,
                "timeout": self.config.stt.timeout,
            },
            "agent": {
                "command": self.config.agent.command,
            },
            "machines": {
                "file": str(self.config.machines.file),
            },
            "projects": {
                "dir": str(self.config.projects.dir),
            },
        }

        # Import and initialize backends
        from .agents import get_agent_backend
        from .stt import get_stt_backend

        try:
            self.stt = get_stt_backend(self.config)
        except ValueError as e:
            logger.warning(f"STT backend not available: {e}")
            from .stt import NoSTT

            self.stt = NoSTT()
        self.agent = get_agent_backend(config_dict)

        # Create HTTP session for TTS server calls
        self._http_session = aiohttp.ClientSession()

        logger.info(f"TTS URL: {self.config.tts.url}")
        logger.info(f"STT backend: {type(self.stt).__name__}")

    async def close_backends(self):
        """Clean up backend resources."""
        if self._http_session:
            await self._http_session.close()

    async def _tts_generate(
        self,
        text: str,
        voice: str,
        exaggeration: float = 0.5,
        cfg_weight: float = 0.5,
    ) -> bytes | None:
        """Generate TTS audio via HTTP call to TTS server."""
        if not self._http_session:
            return None

        try:
            async with self._http_session.post(
                f"{self.config.tts.url}/tts",
                json={
                    "text": text,
                    "voice": voice,
                    "exaggeration": exaggeration,
                    "cfg_weight": cfg_weight,
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                else:
                    logger.warning(f"TTS request failed: {resp.status}")
                    return None
        except Exception as e:
            logger.warning(f"TTS request error: {e}")
            return None

    async def _tts_get_voices(self) -> list[str]:
        """Get available TTS voices via HTTP call to TTS server."""
        if not self._http_session:
            return [self.config.tts.default_voice]

        try:
            async with self._http_session.get(
                f"{self.config.tts.url}/voices",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("voices", [self.config.tts.default_voice])
                else:
                    return [self.config.tts.default_voice]
        except Exception as e:
            logger.warning(f"TTS voices request error: {e}")
            return [self.config.tts.default_voice]

    async def _resolve_voice(self, voice: str) -> str:
        """Resolve voice name, handling 'random' special value.

        Args:
            voice: Voice name or 'random' for random selection

        Returns:
            Resolved voice name (string)
        """
        if voice.lower() != "random":
            return voice

        # Get available voices
        voices_raw = await self._tts_get_voices()
        default_voice = self.config.tts.default_voice

        # Extract voice names (voices may be dicts with 'name' key or strings)
        def get_name(v):
            return v["name"] if isinstance(v, dict) else v

        voices = [get_name(v) for v in voices_raw]

        # Filter out default voice if others are available
        non_default = [v for v in voices if v != default_voice]

        if non_default:
            return random.choice(non_default)
        elif voices:
            return voices[0]
        else:
            return default_voice

    async def cleanup_old_uploads(self):
        """Delete uploads older than cleanup_days."""
        uploads_dir = self.config.uploads.dir
        cleanup_days = self.config.uploads.cleanup_days

        if cleanup_days <= 0 or not uploads_dir.exists():
            return

        cutoff = time.time() - (cleanup_days * 86400)
        cleaned = 0

        for f in uploads_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    cleaned += 1
                except Exception as e:
                    logger.warning(f"Failed to clean up {f}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} old upload(s)")

    def _get_session_config(self, name: str) -> SessionConfig:
        """Get session config dynamically from .agentwire.yml in session's working directory.

        Uses cached config from active_sessions if available, otherwise looks up
        the session's working directory from tmux and reads .agentwire.yml.
        """
        # Check active_sessions first for cached config
        if name in self.active_sessions:
            return self.active_sessions[name].config

        # Parse name for machine
        machine_id = None
        base_name = name
        if "@" in name:
            base_name, machine_id = name.rsplit("@", 1)

        # Get working directory from tmux
        cwd = self._get_session_cwd(base_name, machine_id)
        if not cwd:
            return SessionConfig(voice=self.config.tts.default_voice)

        # Read .agentwire.yml from that path
        yaml_config = self._read_agentwire_yaml(cwd, machine_id)
        if not yaml_config:
            return SessionConfig(voice=self.config.tts.default_voice)

        return SessionConfig(
            type=yaml_config.get("type", "claude-bypass"),
            roles=yaml_config.get("roles", []),
            voice=yaml_config.get("voice", self.config.tts.default_voice),
        )

    def _get_session_cwd(self, session_name: str, machine_id: str | None = None) -> str | None:
        """Get working directory of a tmux session.

        Args:
            session_name: Base session name (without @machine suffix)
            machine_id: Machine ID if remote, None for local

        Returns:
            Working directory path, or None if session not found
        """
        import socket
        local_hostname = socket.gethostname().split('.')[0]

        # Check if this is a local session
        is_local = machine_id is None or machine_id == "local" or machine_id == local_hostname

        if is_local:
            # Local tmux lookup
            result = subprocess.run(
                ["tmux", "display-message", "-t", session_name, "-p", "#{pane_current_path}"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return None
        else:
            # Remote tmux lookup via SSH
            machine = self._get_machine_config(machine_id)
            if not machine:
                return None

            host = machine.get("host", "")
            user = machine.get("user", "")
            ssh_target = f"{user}@{host}" if user else host

            try:
                cmd = f"tmux display-message -t {shlex.quote(session_name)} -p '#{{pane_current_path}}'"
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", ssh_target, cmd],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except (subprocess.TimeoutExpired, Exception):
                pass
            return None

    def _get_machine_config(self, machine_id: str) -> dict | None:
        """Get machine config by ID from machines.json."""
        if hasattr(self.agent, 'machines'):
            for m in self.agent.machines:
                if m.get('id') == machine_id:
                    return m
        return None

    def _read_agentwire_yaml(self, cwd: str, machine_id: str | None = None) -> dict | None:
        """Read .agentwire.yml from a directory.

        Args:
            cwd: Working directory path
            machine_id: Machine ID if remote, None for local

        Returns:
            Parsed YAML dict, or None if not found/invalid
        """
        import socket

        import yaml
        local_hostname = socket.gethostname().split('.')[0]

        is_local = machine_id is None or machine_id == "local" or machine_id == local_hostname

        if is_local:
            yaml_path = Path(cwd) / ".agentwire.yml"
            if yaml_path.exists():
                try:
                    with open(yaml_path) as f:
                        return yaml.safe_load(f) or {}
                except Exception:
                    pass
            return None
        else:
            # Remote read via SSH
            machine = self._get_machine_config(machine_id)
            if not machine:
                return None

            host = machine.get("host", "")
            user = machine.get("user", "")
            ssh_target = f"{user}@{host}" if user else host

            try:
                yaml_path = f"{cwd}/.agentwire.yml"
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", ssh_target, f"cat {shlex.quote(yaml_path)}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return yaml.safe_load(result.stdout) or {}
            except (subprocess.TimeoutExpired, Exception):
                pass
            return None

    def _write_agentwire_yaml(self, cwd: str, data: dict, machine_id: str | None = None) -> bool:
        """Write .agentwire.yml to a directory.

        Args:
            cwd: Working directory path
            data: YAML data to write
            machine_id: Machine ID if remote, None for local

        Returns:
            True if written successfully, False otherwise
        """
        import socket

        import yaml
        local_hostname = socket.gethostname().split('.')[0]

        is_local = machine_id is None or machine_id == "local" or machine_id == local_hostname

        if is_local:
            yaml_path = Path(cwd) / ".agentwire.yml"
            try:
                with open(yaml_path, "w") as f:
                    yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
                return True
            except Exception as e:
                logger.warning(f"Failed to write {yaml_path}: {e}")
                return False
        else:
            # Remote write via SSH
            machine = self._get_machine_config(machine_id)
            if not machine:
                return False

            host = machine.get("host", "")
            user = machine.get("user", "")
            ssh_target = f"{user}@{host}" if user else host

            try:
                yaml_content = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
                yaml_path = f"{cwd}/.agentwire.yml"
                # Use base64 encoding for safe content transmission (avoids heredoc injection)
                encoded = base64.b64encode(yaml_content.encode()).decode()
                cmd = f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(yaml_path)}"
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", ssh_target, cmd],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return result.returncode == 0
            except (subprocess.TimeoutExpired, Exception) as e:
                logger.warning(f"Failed to write remote yaml: {e}")
                return False

    async def _get_voices(self) -> list[str]:
        """Get available TTS voices."""
        return await self._tts_get_voices()

    async def run_agentwire_cmd(self, args: list[str], json_output: bool = True) -> tuple[bool, dict]:
        """Run agentwire CLI command and parse output.

        Args:
            args: Command arguments (e.g., ["new", "-s", "myapp/feature"])
            json_output: If True, appends --json and parses JSON output.
                If False, returns raw stdout/stderr without JSON parsing.

        Returns:
            Tuple of (success, result_dict). On success, result_dict contains
            the parsed JSON output (or raw output if json_output=False).
            On failure, result_dict contains an "error" key.
        """
        cmd = ["agentwire", *args]
        if json_output:
            cmd.append("--json")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if not json_output:
            out = stdout.decode().strip()
            err = stderr.decode().strip()
            if proc.returncode == 0:
                return True, {"output": out}
            return False, {"error": err or f"Command failed with exit code {proc.returncode}"}

        if proc.returncode == 0:
            try:
                return True, json.loads(stdout.decode())
            except json.JSONDecodeError as e:
                return False, {"error": f"Failed to parse JSON output: {e}"}
        # Try to parse stdout for JSON error response
        try:
            result = json.loads(stdout.decode())
            if "error" in result:
                return False, result
        except json.JSONDecodeError:
            pass
        return False, {"error": stderr.decode().strip() or f"Command failed with exit code {proc.returncode}"}

    async def _run_ssh_command(self, machine_id: str, command: str) -> str:
        """Run command on remote machine via SSH.

        Args:
            machine_id: The machine ID from machines.json
            command: Shell command to run remotely

        Returns:
            stdout output if successful, empty string on failure
        """
        machines_file = self.config.machines.file
        if not machines_file.exists():
            return ""

        try:
            with open(machines_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return ""

        machine = next((m for m in data.get("machines", []) if m.get("id") == machine_id), None)
        if not machine:
            return ""

        host = machine.get("host", "")
        user = machine.get("user", "")
        ssh_target = f"{user}@{host}" if user else host

        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                ssh_target, command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode() if proc.returncode == 0 else ""
        except Exception:
            return ""

    # HTTP Handlers

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint for network diagnostics."""
        return web.json_response({"status": "ok", "version": __version__})

    async def handle_index(self, request: web.Request) -> web.Response:
        """Serve the desktop UI."""
        voices = await self._get_voices()
        context = {
            "version": __version__,
            "voices": voices,
            "default_voice": self.config.tts.default_voice,
        }
        response = aiohttp_jinja2.render_template("desktop.html", request, context)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    async def handle_dashboard_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint for dashboard updates (sessions, machines, config)."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.dashboard_clients.add(ws)
        logger.info(f"Dashboard client connected (total: {len(self.dashboard_clients)})")

        # Send initial state
        try:
            sessions_data = await self._get_sessions_data()
            await ws.send_json({"type": "sessions_update", "sessions": sessions_data})

            machines_data = await self._get_machines_data()
            await ws.send_json({"type": "machines_update", "machines": machines_data})

            # Send current agentwire session activity state
            agentwire_activity = self.session_activity.get("agentwire", {})
            if agentwire_activity:
                last_timestamp = agentwire_activity.get("last_output_timestamp", 0.0)
                time_since = time.time() - last_timestamp if last_timestamp else float('inf')
                threshold = self.config.server.activity_threshold_seconds
                is_active = time_since <= threshold
                await ws.send_json({
                    "type": "session_activity",
                    "session": "agentwire",
                    "active": is_active
                })
                logger.info(f"[Dashboard] Sent initial agentwire activity: {'active' if is_active else 'idle'}")
        except Exception as e:
            logger.error(f"Failed to send initial dashboard state: {e}")

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_dashboard_message(ws, data)
                    except json.JSONDecodeError:
                        pass
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"Dashboard WebSocket error: {ws.exception()}")
        finally:
            self.dashboard_clients.discard(ws)
            logger.info(f"Dashboard client disconnected (total: {len(self.dashboard_clients)})")

        return ws

    async def _handle_dashboard_message(self, ws: web.WebSocketResponse, data: dict):
        """Handle messages from dashboard clients."""
        msg_type = data.get("type")

        if msg_type == "refresh_sessions":
            sessions_data = await self._get_sessions_data()
            await ws.send_json({"type": "sessions_update", "sessions": sessions_data})

        elif msg_type == "refresh_machines":
            machines_data = await self._get_machines_data()
            await ws.send_json({"type": "machines_update", "machines": machines_data})

        elif msg_type == "desktop_windows_report":
            # Response from a client with its window list
            request_id = data.get("request_id")
            windows = data.get("windows", [])
            if hasattr(self, '_desktop_window_responses') and request_id in self._desktop_window_responses:
                future = self._desktop_window_responses[request_id]
                if not future.done():
                    future.set_result(windows)

    async def _get_sessions_data(self) -> list:
        """Get all sessions list for dashboard (local + remote + SDK)."""
        try:
            # Get local sessions
            success, result = await self.run_agentwire_cmd(["list", "--local", "--sessions"])
            if not success:
                return []

            sessions = result.get("sessions", [])

            # Get remote sessions
            remote_success, remote_result = await self.run_agentwire_cmd(["list", "--remote", "--sessions"])
            if remote_success:
                remote_sessions = remote_result.get("sessions", [])
                sessions.extend(remote_sessions)

            session_names = set()
            for s in sessions:
                name = s.get("name", "")
                session_names.add(name)
                s["activity"] = self._get_global_session_activity(name)
                # Include attached client count for presence indicator
                s["client_count"] = self.session_client_counts.get(name, 0)

            # Clean up stale state for sessions that no longer exist
            stale = [k for k in self.session_client_counts if k not in session_names]
            for k in stale:
                del self.session_client_counts[k]

            return sessions
        except Exception as e:
            logger.error(f"Failed to get sessions data: {e}")
            return []

    async def _get_machines_data(self) -> list:
        """Get machines list (without slow SSH status checks)."""
        try:
            machines = []
            if hasattr(self.agent, 'machines'):
                for m in self.agent.machines:
                    machines.append({
                        "id": m.get("id"),
                        "host": m.get("host"),
                        "status": "unknown",  # Don't check SSH on initial load
                    })
            return machines
        except Exception as e:
            logger.error(f"Failed to get machines data: {e}")
            return []

    # =========================================================================
    # Desktop UI Control API (for MCP agents)
    # =========================================================================

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
        """POST /api/desktop/window/open — open a window in the portal."""
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
        elif window_type == "artifact":
            url = data.get("url")
            title = data.get("title", "Artifact")
            if not url:
                return web.json_response({"success": False, "error": "url required"}, status=400)
            window_id = data.get("artifact_id") or f"artifact-{url.replace('/', '-').replace('.', '-')}"
            await self.broadcast_dashboard("desktop_open_window", {
                "window_type": "artifact",
                "url": url,
                "title": title,
                "artifact_id": window_id,
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
        """POST /api/desktop/notification — post a toast notification to the portal."""
        data = await request.json()
        text = data.get("text", "")
        if not text:
            return web.json_response({"success": False, "error": "text required"}, status=400)

        import uuid
        notification_id = data.get("id") or str(uuid.uuid4())[:8]
        session = data.get("session")
        priority = data.get("priority", "normal")

        notification = {
            "id": notification_id,
            "text": text,
            "session": session,
            "priority": priority,
            "timestamp": time.time(),
        }

        self.active_notifications[notification_id] = notification

        await self.broadcast_dashboard("notification", notification)

        return web.json_response({"success": True, "id": notification_id})

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

    async def api_artifacts_upload(self, request):
        """POST /api/artifacts/upload — write HTML content to the artifacts directory."""
        try:
            data = await request.json()
            filename = data.get("filename")
            content = data.get("content")

            if not filename or not content:
                return web.json_response(
                    {"success": False, "error": "filename and content required"}, status=400
                )

            # Sanitize filename — only allow safe characters
            import re
            if not re.match(r'^[a-zA-Z0-9_\-][a-zA-Z0-9_\-\.]*\.html$', filename):
                return web.json_response(
                    {"success": False, "error": "filename must be alphanumeric with .html extension"},
                    status=400,
                )

            # Check size
            max_bytes = self.config.artifacts.max_size_mb * 1024 * 1024
            if len(content.encode('utf-8')) > max_bytes:
                return web.json_response(
                    {"success": False, "error": f"content too large (max {self.config.artifacts.max_size_mb}MB)"},
                    status=400,
                )

            # Ensure artifacts directory exists
            artifacts_dir = self.config.artifacts.dir
            artifacts_dir.mkdir(parents=True, exist_ok=True)

            # Write file atomically (write to temp, rename)
            filepath = artifacts_dir / filename
            tmp_path = filepath.with_suffix('.tmp')
            tmp_path.write_text(content, encoding='utf-8')
            tmp_path.rename(filepath)

            logger.info(f"Artifact written: {filepath}")
            return web.json_response({
                "success": True,
                "path": str(filepath),
                "url": f"/artifacts/{filename}",
            })

        except Exception as e:
            logger.error(f"Artifact upload failed: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def api_artifacts_list(self, request):
        """GET /api/artifacts — list files in the artifacts directory."""
        try:
            artifacts_dir = self.config.artifacts.dir
            if not artifacts_dir.exists():
                return web.json_response([])

            files = []
            for f in sorted(artifacts_dir.iterdir()):
                if f.is_file() and not f.name.startswith('.'):
                    stat = f.stat()
                    files.append({
                        "name": f.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    })
            return web.json_response(files)

        except Exception as e:
            logger.error(f"Artifacts list failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_artifacts_delete(self, request):
        """DELETE /api/artifacts/{filename} — delete a file from the artifacts directory."""
        import re
        filename = request.match_info["filename"]

        # Sanitize — prevent path traversal
        if not re.match(r'^[a-zA-Z0-9_\-][a-zA-Z0-9_\-\.]*$', filename):
            return web.json_response(
                {"success": False, "error": "invalid filename"}, status=400
            )

        filepath = self.config.artifacts.dir / filename
        if not filepath.exists():
            return web.json_response(
                {"success": False, "error": "file not found"}, status=404
            )

        try:
            filepath.unlink()
            logger.info(f"Artifact deleted: {filepath}")
            return web.json_response({"success": True})
        except Exception as e:
            logger.error(f"Artifact delete failed: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def broadcast_dashboard(self, msg_type: str, data: dict):
        """Broadcast a message to all connected dashboard clients."""
        if not self.dashboard_clients:
            return

        message = {"type": msg_type, **data}
        closed = []

        for ws in self.dashboard_clients:
            try:
                await ws.send_json(message)
            except Exception:
                closed.append(ws)

        for ws in closed:
            self.dashboard_clients.discard(ws)

    def _get_system_session_names(self) -> dict[str, str]:
        """Get system session names from config."""
        services = self.config.raw.get("services", {})
        return {
            "portal": services.get("portal", {}).get("session_name", "agentwire-portal"),
            "tts": services.get("tts", {}).get("session_name", "agentwire-tts"),
            "stt": services.get("stt", {}).get("session_name", "agentwire-stt"),
            "main": "agentwire",  # Main session name is always "agentwire"
        }

    def _is_system_session(self, name: str) -> bool:
        """Check if this is a system session (agentwire services)."""
        # Extract base session name (without @machine suffix)
        base_name = name.split("@")[0]
        session_names = self._get_system_session_names()
        return base_name in session_names.values()

    def _get_session_activity_status(self, session: Session) -> str:
        """Calculate activity status based on last output timestamp.

        Returns:
            "active" if output changed within threshold, "idle" otherwise
        """
        if session.last_output_timestamp == 0.0:
            return "idle"

        time_since_last_output = time.time() - session.last_output_timestamp
        threshold = self.config.server.activity_threshold_seconds

        return "active" if time_since_last_output <= threshold else "idle"

    def _get_global_session_activity(self, session_name: str) -> str:
        """Get session activity from global tracking dict.

        Returns:
            "active" if session has recent output, "idle" otherwise
        """
        activity_info = self.session_activity.get(session_name)
        if not activity_info:
            return "idle"

        last_timestamp = activity_info.get("last_output_timestamp", 0.0)
        if last_timestamp == 0.0:
            return "idle"

        time_since_last_output = time.time() - last_timestamp
        threshold = self.config.server.activity_threshold_seconds

        return "active" if time_since_last_output <= threshold else "idle"

    async def monitor_all_sessions(self):
        """Background task to monitor all session activity for dashboard indicators.

        Polls tmux output for all sessions and broadcasts activity state changes.
        """
        threshold = self.config.server.activity_threshold_seconds
        # Track per-session state: {session_name: {"last_output": str, "last_active": bool}}
        session_states: dict[str, dict] = {}

        logger.info(f"[Monitor] Starting session monitor (threshold: {threshold}s)")

        while True:
            try:
                # Get list of all sessions (local and remote)
                session_names = []

                # Local sessions
                success, result = await self.run_agentwire_cmd(["list", "--local", "--sessions"])
                if success:
                    for s in result.get("sessions", []):
                        if s.get("name"):
                            session_names.append(s["name"])

                # Remote sessions (names already include @machine suffix)
                success, result = await self.run_agentwire_cmd(["list", "--remote", "--sessions"])
                if success:
                    for s in result.get("sessions", []):
                        if s.get("name"):
                            session_names.append(s["name"])

                # Poll each session
                for session_name in session_names:
                    try:
                        # Get current output
                        success, output_result = await self.run_agentwire_cmd(
                            ["output", "-s", session_name, "-n", "50"]
                        )

                        if not success:
                            continue

                        current_output = output_result.get("output", "")

                        # Initialize state for new sessions
                        if session_name not in session_states:
                            session_states[session_name] = {
                                "last_output": "",
                                "last_active": False
                            }

                        state = session_states[session_name]

                        # Check if output changed
                        if current_output != state["last_output"]:
                            state["last_output"] = current_output
                            # Update global activity tracking
                            self.session_activity[session_name] = {
                                "last_output_timestamp": time.time(),
                                "last_output": current_output[-500:] if current_output else "",
                            }

                        # Calculate current activity state
                        activity_info = self.session_activity.get(session_name, {})
                        last_timestamp = activity_info.get("last_output_timestamp", 0.0)
                        time_since = time.time() - last_timestamp if last_timestamp else float('inf')
                        is_active = time_since <= threshold

                        # Broadcast if state changed
                        if is_active != state["last_active"]:
                            state["last_active"] = is_active
                            logger.debug(f"[Monitor] {session_name} activity: {'active' if is_active else 'idle'}")
                            await self.broadcast_dashboard("session_activity", {
                                "session": session_name,
                                "active": is_active
                            })

                    except Exception as e:
                        logger.debug(f"[Monitor] Error polling {session_name}: {e}")

                # Clean up state for sessions that no longer exist
                current_names = set(session_names)
                removed_sessions = []
                for name in list(session_states.keys()):
                    if name not in current_names:
                        del session_states[name]
                        self.session_activity.pop(name, None)
                        removed_sessions.append(name)

                # Notify dashboard about removed sessions
                if removed_sessions:
                    for name in removed_sessions:
                        logger.info(f"[Monitor] Session '{name}' no longer exists, notifying dashboard")
                        await self.broadcast_dashboard("session_closed", {"session": name})
                    # Send updated sessions list
                    sessions_data = await self._get_sessions_data()
                    await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

                await asyncio.sleep(0.5)  # Poll every 500ms

            except asyncio.CancelledError:
                logger.info("[Monitor] Session monitor stopped")
                break
            except Exception as e:
                logger.debug(f"[Monitor] Error in monitor loop: {e}")
                await asyncio.sleep(2)  # Back off on errors

    async def idle_nag_loop(self):
        """Background task: periodically check for idle sessions with open browser windows.

        Gathers idle session data and sends it to the agentwire-notifications session,
        which crafts a natural TTS message and speaks it via say().
        """
        NAG_INTERVAL = 120  # seconds between scans
        NAG_IDLE_THRESHOLD = 120  # seconds idle before including in nag (2 min minimum)
        NAG_SESSION = "agentwire-notifications"
        SERVICE_PREFIX = "agentwire-"
        nag_counts: dict[str, int] = {}  # session -> consecutive nag count

        logger.info("[IdleNag] Starting idle nag loop (interval: %ds, threshold: %ds)",
                     NAG_INTERVAL, NAG_IDLE_THRESHOLD)

        # Let the monitor warm up first
        await asyncio.sleep(10)

        while True:
            try:
                if not self.dashboard_clients:
                    nag_counts.clear()
                    await asyncio.sleep(NAG_INTERVAL)
                    continue

                # Find sessions with open browser windows that are truly idle
                idle_sessions = []
                for name, info in self.session_activity.items():
                    if name.startswith(SERVICE_PREFIX):
                        continue
                    if name == NAG_SESSION:
                        continue
                    if self.session_client_counts.get(name, 0) == 0:
                        nag_counts.pop(name, None)
                        continue
                    last_ts = info.get("last_output_timestamp", 0.0)
                    idle_secs = time.time() - last_ts if last_ts else float('inf')
                    if idle_secs > NAG_IDLE_THRESHOLD:
                        idle_sessions.append((name, idle_secs))
                    else:
                        nag_counts.pop(name, None)

                if not idle_sessions:
                    await asyncio.sleep(NAG_INTERVAL)
                    continue

                # Gather session metadata
                sessions_info = await self._get_sessions_data()
                sessions_by_name = {s.get("name", ""): s for s in sessions_info}

                # Fetch fresh output for each idle session
                session_data = []
                for name, idle_secs in idle_sessions:
                    nag_counts[name] = nag_counts.get(name, 0) + 1
                    idle_min = int(idle_secs / 60)

                    # Session metadata
                    meta = sessions_by_name.get(name, {})

                    # Get a fuller snapshot of the session output
                    output_snippet = ""
                    try:
                        success, output_result = await self.run_agentwire_cmd(
                            ["output", "-s", name, "-n", "30"]
                        )
                        if success:
                            output_snippet = output_result.get("output", "")[-1000:]
                    except Exception:
                        output_snippet = self.session_activity.get(name, {}).get("last_output", "")[-500:]

                    session_data.append({
                        "session": name,
                        "idle_minutes": idle_min,
                        "nag_count": nag_counts[name],
                        "type": meta.get("type", "unknown"),
                        "roles": meta.get("roles", []),
                        "project_path": meta.get("path", ""),
                        "machine": meta.get("machine") or "local",
                        "last_output_snippet": output_snippet,
                    })

                # Send to the notifications session
                prompt = (
                    f"[IDLE NAG] The following {len(session_data)} session(s) have open browser windows "
                    f"but are idle. Review each one's output to decide if it actually needs a nag "
                    f"(waiting on input, hit an error) or should be skipped (task complete, user "
                    f"acknowledged, sitting at a clean prompt). Only say() if something needs attention.\n\n"
                )
                for sd in session_data:
                    roles_str = ", ".join(sd["roles"]) if sd["roles"] else "none"
                    prompt += (
                        f"### {sd['session']}\n"
                        f"- Idle: {sd['idle_minutes']}min | Nagged: {sd['nag_count']}x\n"
                        f"- Type: {sd['type']} | Roles: {roles_str}\n"
                        f"- Project: {sd['project_path']} | Machine: {sd['machine']}\n"
                    )
                    if sd['last_output_snippet']:
                        prompt += f"- Last output:\n```\n{sd['last_output_snippet']}\n```\n"
                    prompt += "\n"

                logger.info("[IdleNag] Sending to %s: %d idle session(s)", NAG_SESSION, len(session_data))
                success, _ = await self.run_agentwire_cmd([
                    "send", "-s", NAG_SESSION, prompt
                ])
                if not success:
                    logger.warning("[IdleNag] Failed to send to %s — is the session running?", NAG_SESSION)

                await asyncio.sleep(NAG_INTERVAL)

            except asyncio.CancelledError:
                logger.info("[IdleNag] Idle nag loop stopped")
                break
            except Exception as e:
                logger.debug("[IdleNag] Error: %s", e)
                await asyncio.sleep(NAG_INTERVAL)

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connections for a session."""
        name = request.match_info["name"]
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        # Get or create session
        if name not in self.active_sessions:
            self.active_sessions[name] = Session(name=name, config=self._get_session_config(name))

        session = self.active_sessions[name]
        client_id = str(id(ws))
        session.clients.add(ws)
        logger.info(f"[{name}] Client connected (total: {len(session.clients)})")

        # Skip tmux polling for special sessions that aren't real tmux sessions
        is_real_session = name != "dashboard"

        # Send current output immediately on connect (if this is a real session)
        if is_real_session:
            try:
                output = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.agent.get_output(name, lines=100)
                )
                if output:
                    session.last_output = output
                    await ws.send_json({"type": "output", "data": output})
            except Exception as e:
                logger.debug(f"Initial output fetch failed for {name}: {e}")

            # Start output polling if not running
            if session.output_task is None or session.output_task.done():
                session.output_task = asyncio.create_task(self._poll_output(session))

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_ws_message(session, ws, client_id, data)
                    except json.JSONDecodeError:
                        pass
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
        finally:
            session.clients.discard(ws)
            if session.locked_by == client_id:
                session.locked_by = None
                await self._broadcast(session, {"type": "session_unlocked"})

        return ws

    async def handle_terminal_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint for interactive terminal via tmux attach.

        Provides bidirectional communication between browser terminal (xterm.js)
        and tmux session. Handles terminal input, output, and resize commands.
        """
        session_name = request.match_info["name"]
        # Browser passes initial terminal size as query params to avoid 80x24 flash
        init_cols = int(request.rel_url.query.get("cols", 80))
        init_rows = int(request.rel_url.query.get("rows", 24))
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        proc = None
        master_fd = None
        tmux_to_ws_task = None
        ws_to_tmux_task = None

        # Track this connection for TTS routing (so audio goes to browser, not local speakers)
        if session_name not in self.active_sessions:
            self.active_sessions[session_name] = Session(name=session_name, config=self._get_session_config(session_name))
        session = self.active_sessions[session_name]
        session.clients.add(ws)
        logger.info(f"[Terminal] Client connected to {session_name} (total: {len(session.clients)})")

        try:
            # Parse session name for local vs remote
            project, branch, machine_id = parse_session_name(session_name)
            session_name = f"{project}/{branch}" if branch else project

            # Build tmux attach command
            # Check if this is a remote machine (needs SSH)
            is_remote = False
            if machine_id:
                machine_config = self._get_machine_config(machine_id)
                if not machine_config:
                    logger.error(f"[Terminal] Machine not found: {machine_id}")
                    await ws.close()
                    return ws
                # Only use SSH if machine is not marked as local
                is_remote = not machine_config.get("local", False)

            if is_remote:
                ssh_host = machine_config.get("host", machine_id)
                ssh_user = machine_config.get("user")
                ssh_target = f"{ssh_user}@{ssh_host}" if ssh_user else ssh_host

                # Remote session via SSH with PTY allocation
                # Use accept-new to accept new host keys but reject changed ones (MITM protection)
                cmd = ["ssh", "-t", "-o", "StrictHostKeyChecking=accept-new", ssh_target, "tmux", "attach", "-t", session_name]
                logger.info(f"[Terminal] Attaching to {session_name}: {' '.join(cmd)}")

                # Create PTY for SSH too (ssh -t needs local PTY)
                master_fd, slave_fd = pty.openpty()

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    preexec_fn=os.setsid,
                )

                os.close(slave_fd)
                os.set_blocking(master_fd, False)

                # Send initial window size to trigger tmux redraw
                winsize = struct.pack("HHHH", 24, 80, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                logger.info(f"[Terminal] Set initial PTY size for SSH to 80x24 (fd={master_fd})")
            else:
                # Local session - use PTY
                cmd = ["tmux", "attach", "-t", session_name]
                logger.info(f"[Terminal] Attaching to {session_name}: {' '.join(cmd)}")

                # Create PTY for local tmux attach
                master_fd, slave_fd = pty.openpty()

                # Setup function to make PTY the controlling terminal
                def setup_pty_session():
                    os.setsid()  # Create new session (required first)
                    # Make the PTY the controlling terminal
                    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

                # Spawn process with slave PTY as stdin/stdout/stderr
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    preexec_fn=setup_pty_session,
                )

                # Close slave fd in parent - child keeps it open
                os.close(slave_fd)

                # Make master fd non-blocking for async reads
                os.set_blocking(master_fd, False)

                # Set initial PTY size from browser's reported dimensions
                winsize = struct.pack("HHHH", init_rows, init_cols, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                logger.info(f"[Terminal] Set initial PTY size to {init_cols}x{init_rows} (fd={master_fd})")

            # Task: Forward tmux stdout → WebSocket
            async def forward_tmux_to_ws():
                """Read from tmux and send to WebSocket."""
                loop = asyncio.get_event_loop()
                data_queue = asyncio.Queue()
                reader_registered = False

                def on_readable():
                    """Called when PTY master FD has data to read."""
                    try:
                        data = os.read(master_fd, 8192)
                        logger.debug(f"[Terminal] on_readable callback: read {len(data) if data else 0} bytes")
                        if data:
                            # Schedule putting data in queue from event loop
                            asyncio.create_task(data_queue.put(data))
                        else:
                            # Empty read = EOF (process exited)
                            logger.info(f"[Terminal] PTY EOF (empty read) for {session_name}")
                            asyncio.create_task(data_queue.put(None))
                    except OSError as e:
                        logger.info(f"[Terminal] PTY read error: {e}")
                        # Signal EOF
                        asyncio.create_task(data_queue.put(None))

                try:
                    if master_fd is not None:
                        # Local: register reader once for PTY master
                        loop.add_reader(master_fd, on_readable)
                        reader_registered = True
                        logger.info(f"[Terminal] Registered PTY reader for {session_name} (fd={master_fd})")

                    while True:
                        if master_fd is not None:
                            # Local: read from queue populated by on_readable
                            data = await data_queue.get()
                            if data is None:  # EOF signal
                                logger.info(f"[Terminal] Received EOF from PTY for {session_name}")
                                # For remote sessions, check exit code to determine message type
                                if is_remote and not ws.closed:
                                    try:
                                        # Wait for SSH process to exit and get return code
                                        exit_code = await proc.wait() if proc else None
                                        logger.info(f"[Terminal] SSH exit code for {session_name}: {exit_code}")

                                        if exit_code == 0:
                                            # Clean exit - tmux session ended normally, close window
                                            await ws.send_json({"type": "remote_session_ended", "session": session_name})
                                            logger.info(f"[Terminal] Sent remote_session_ended to browser for {session_name}")
                                        else:
                                            # Non-zero exit - connection issue, show reconnect overlay
                                            await ws.send_json({"type": "remote_disconnected", "session": session_name})
                                            logger.info(f"[Terminal] Sent remote_disconnected to browser for {session_name}")
                                    except Exception as e:
                                        logger.warning(f"[Terminal] Failed to send disconnect message: {e}")
                                break
                            logger.debug(f"[Terminal] Read {len(data)} bytes from PTY for {session_name}")
                            if not ws.closed:
                                await ws.send_bytes(data)
                                logger.debug(f"[Terminal] Sent {len(data)} bytes to WebSocket for {session_name}")
                        else:
                            # Remote: read from subprocess stdout
                            data = await proc.stdout.read(8192)
                            if not data:
                                break
                            if not ws.closed:
                                await ws.send_bytes(data)
                except asyncio.CancelledError:
                    logger.debug(f"[Terminal] tmux→ws task cancelled for {session_name}")
                except Exception as e:
                    logger.error(f"[Terminal] Error forwarding tmux→ws for {session_name}: {e}")
                finally:
                    if master_fd is not None and reader_registered:
                        try:
                            loop.remove_reader(master_fd)
                            logger.info(f"[Terminal] Unregistered PTY reader for {session_name}")
                        except Exception:
                            pass

            # Task: Forward WebSocket → tmux stdin
            async def forward_ws_to_tmux():
                """Read from WebSocket and write to tmux stdin."""
                try:
                    async for msg in ws:
                        if msg.type == web.WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                                msg_type = payload.get("type")

                                if msg_type == "input":
                                    # Terminal input from browser
                                    input_data = payload.get("data", "")
                                    if input_data:
                                        # Filter out terminal capability responses that xterm sends
                                        # These look like: ESC[?1;2c (Primary DA) or ESC[>0;276;0c (Secondary DA)
                                        # They get typed as input to Claude Code which is annoying
                                        filtered_data = re.sub(r'\x1b\[\?[0-9;]*c', '', input_data)  # Primary DA
                                        filtered_data = re.sub(r'\x1b\[>[0-9;]*c', '', filtered_data)  # Secondary DA
                                        filtered_data = re.sub(r'\x1b\[[0-9;]*c', '', filtered_data)  # Generic DA

                                        if filtered_data:
                                            data_bytes = filtered_data.encode()
                                            if len(data_bytes) <= PASTE_THRESHOLD:
                                                # Normal keystroke — write immediately
                                                if master_fd is not None:
                                                    os.write(master_fd, data_bytes)
                                                elif proc.stdin:
                                                    proc.stdin.write(data_bytes)
                                                    await proc.stdin.drain()
                                            else:
                                                # Paste detected — chunk writes to avoid flooding PTY
                                                for i in range(0, len(data_bytes), PASTE_CHUNK_SIZE):
                                                    chunk = data_bytes[i:i + PASTE_CHUNK_SIZE]
                                                    if master_fd is not None:
                                                        os.write(master_fd, chunk)
                                                    elif proc.stdin:
                                                        proc.stdin.write(chunk)
                                                        await proc.stdin.drain()
                                                    if i + PASTE_CHUNK_SIZE < len(data_bytes):
                                                        await asyncio.sleep(PASTE_CHUNK_DELAY)

                                elif msg_type == "resize":
                                    # Terminal resize
                                    cols = payload.get("cols", 80)
                                    rows = payload.get("rows", 24)
                                    logger.info(f"[Terminal] Resize {session_name} to {cols}x{rows}")

                                    if master_fd is not None:
                                        # Local: use TIOCSWINSZ ioctl to resize PTY
                                        winsize = struct.pack("HHHH", rows, cols, 0, 0)
                                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                                        # Send SIGWINCH to notify tmux of size change
                                        if proc and proc.pid:
                                            try:
                                                os.kill(proc.pid, signal.SIGWINCH)
                                            except (OSError, ProcessLookupError):
                                                pass  # Process may have exited
                                        # Explicitly resize the tmux window to match the browser.
                                        # Using -x/-y instead of -A avoids the race condition where
                                        # tmux hasn't yet processed SIGWINCH when resize-window runs.
                                        resize_proc = await asyncio.create_subprocess_exec(
                                            "tmux", "resize-window", "-t", session_name,
                                            "-x", str(cols), "-y", str(rows),
                                            stdout=asyncio.subprocess.DEVNULL,
                                            stderr=asyncio.subprocess.DEVNULL,
                                        )
                                        await resize_proc.wait()
                                    else:
                                        # Remote: send tmux resize-window command
                                        resize_cmd = f"tmux resize-window -t {session_name} -x {cols} -y {rows}\n"
                                        resize_proc = await asyncio.create_subprocess_exec(
                                            "ssh", ssh_target, "sh", "-c", resize_cmd,
                                            stdout=asyncio.subprocess.DEVNULL,
                                            stderr=asyncio.subprocess.DEVNULL,
                                        )
                                        await resize_proc.wait()

                            except json.JSONDecodeError:
                                logger.warning(f"[Terminal] Invalid JSON from WebSocket: {msg.data}")
                            except Exception as e:
                                logger.error(f"[Terminal] Error handling message: {e}")

                        elif msg.type == web.WSMsgType.ERROR:
                            logger.error(f"[Terminal] WebSocket error: {ws.exception()}")
                            break

                except asyncio.CancelledError:
                    logger.debug(f"[Terminal] ws→tmux task cancelled for {session_name}")
                except Exception as e:
                    logger.error(f"[Terminal] Error forwarding ws→tmux for {session_name}: {e}")

            # Start both forwarding tasks
            tmux_to_ws_task = asyncio.create_task(forward_tmux_to_ws())
            ws_to_tmux_task = asyncio.create_task(forward_ws_to_tmux())

            # Wait for either task to complete (disconnect or error)
            done, pending = await asyncio.wait(
                [tmux_to_ws_task, ws_to_tmux_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            logger.info(f"[Terminal] Disconnected from {session_name}")

        except FileNotFoundError:
            logger.error("[Terminal] tmux command not found")
            if not ws.closed:
                await ws.send_json({
                    "type": "error",
                    "message": "tmux not found on system"
                })

        except Exception as e:
            logger.error(f"[Terminal] Error attaching to {session_name}: {e}")
            if not ws.closed:
                await ws.send_json({
                    "type": "error",
                    "message": f"Failed to attach: {str(e)}"
                })

        finally:
            # Clean up subprocess
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                except Exception as e:
                    logger.debug(f"[Terminal] Error terminating process: {e}")

            # Ensure tasks are cancelled
            for task in [tmux_to_ws_task, ws_to_tmux_task]:
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            # Close PTY master fd if used
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except Exception as e:
                    logger.debug(f"[Terminal] Error closing master fd: {e}")

            # Remove client from session tracking
            session.clients.discard(ws)
            logger.info(f"[Terminal] Client disconnected from {session.name} (remaining: {len(session.clients)})")

        return ws

    async def _handle_ws_message(
        self, session: Session, ws: web.WebSocketResponse, client_id: str, data: dict
    ):
        """Handle incoming WebSocket messages."""
        msg_type = data.get("type")

        if msg_type == "recording_started":
            # Try to lock the session
            if session.locked_by is None:
                session.locked_by = client_id
                # Notify others
                for client in session.clients:
                    if client != ws:
                        try:
                            await client.send_json({"type": "session_locked"})
                        except Exception:
                            pass

        elif msg_type == "recording_stopped":
            # Unlock will happen after TTS completes or on disconnect
            pass

        elif msg_type == "resize":
            # Resize tmux pane for monitor mode (so captured output fits the viewer)
            cols = data.get("cols", 80)
            rows = data.get("rows", 24)
            logger.info(f"[{session.name}] Resize request: {cols}x{rows}")
            # Note: Resizing won't reformat existing scrollback content.
            # For proper display, use terminal mode (Connect) instead of monitor mode.

    # Patterns for say command detection
    # Matches: say "text", agentwire say "text", agentwire say -s session "text"
    SAY_PATTERN = re.compile(r'(?:agentwire\s+)?say\s+(?:-s\s+\S+\s+)?(?:"([^"]+)"|\'([^\']+)\')', re.IGNORECASE)
    ANSI_PATTERN = re.compile(r'\x1b\[[0-9;]*m|\x1b\].*?\x07')

    # Pattern to detect AskUserQuestion UI blocks
    # Format: ☐ Header\n\nQuestion?\n\n❯ 1. Label\n     Description\n  2. Label...
    # Multi-tab format: ←  ☐ Tab1  ☐ Tab2  ✔ Submit  →\n\nQuestion?...
    ASK_PATTERN = re.compile(
        r'☐\s+(\S+)'              # ☐ followed by first word only (active tab name)
        r'.*?\n\s*\n'             # Rest of header line + blank line
        r'((?:.+\n)+?)'           # Question text (one or more lines, non-greedy)
        r'\s*\n'                  # Blank line before options
        r'((?:[❯\s]+\d+\.\s+.+\n(?:\s{3,}.+\n)?)+)',  # Options block
        re.MULTILINE | re.DOTALL
    )

    # Simple format without ☐ header (e.g., "Ready to submit?\n\n❯ 1. Submit\n  2. Cancel")
    ASK_PATTERN_SIMPLE = re.compile(
        r'\n([^\n☐❯]+\?)\s*\n'    # Question ending with ? (not containing ☐ or ❯)
        r'\s*\n'                   # Blank line
        r'((?:[❯\s]+\d+\.\s+.+\n(?:\s{3,}.+\n)?)+)',  # Options block
        re.MULTILINE
    )

    def _parse_ask_options(self, options_block: str) -> list[dict]:
        """Parse numbered options from AskUserQuestion block."""
        options = []
        current_option = None

        for line in options_block.split('\n'):
            line = self.ANSI_PATTERN.sub('', line)
            option_match = re.match(r'[❯\s]*(\d+)\.\s+(.+)', line)
            if option_match:
                if current_option:
                    options.append(current_option)
                current_option = {
                    'number': int(option_match.group(1)),
                    'label': option_match.group(2).strip(),
                    'description': '',
                }
            elif current_option and line.strip():
                current_option['description'] = line.strip()

        if current_option:
            options.append(current_option)

        return options

    async def _poll_output(self, session: Session):
        """Poll agent output and broadcast to session clients."""
        while session.clients:
            try:
                # Run sync get_output in thread pool to avoid blocking
                output = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.agent.get_output(session.name, lines=100)
                )
                if output != session.last_output:
                    old_output = session.last_output
                    session.last_output = output
                    timestamp = time.time()
                    session.last_output_timestamp = timestamp  # Update activity timestamp

                    # Also update global activity tracking (persists across session create/destroy)
                    self.session_activity[session.name] = {
                        "last_output_timestamp": timestamp,
                        "last_output": output,
                    }

                    await self._broadcast(session, {"type": "output", "data": output})

                    # Notify clients that agent is actively working
                    if old_output:  # Skip first poll
                        await self._broadcast(session, {"type": "activity"})
                        # Also notify dashboard clients
                        await self.broadcast_dashboard("session_activity", {
                            "session": session.name,
                            "active": True
                        })

                    # Note: TTS detection removed - agentwire say CLI calls /api/say directly

                # Detect AskUserQuestion blocks (check full output - questions persist)
                clean_output = self.ANSI_PATTERN.sub('', output)
                ask_match = self.ASK_PATTERN.search(clean_output)

                # Try simple pattern if main pattern doesn't match
                # (e.g., "Ready to submit your answers?\n\n❯ 1. Submit")
                header = None
                question = None
                options_block = None

                if ask_match:
                    header = ask_match.group(1)
                    question = ask_match.group(2).strip()
                    options_block = ask_match.group(3)
                else:
                    simple_match = self.ASK_PATTERN_SIMPLE.search(clean_output)
                    if simple_match:
                        question = simple_match.group(1).strip()
                        options_block = simple_match.group(2)
                        # Generate header from question (first word or "Confirm")
                        header = question.split()[0].rstrip('?') if question else "Confirm"

                if question and options_block:
                    options = self._parse_ask_options(options_block)
                    question_key = f"{header}:{question}"

                    if question_key != session.last_question and options:
                        session.last_question = question_key
                        logger.info(f"[{session.name}] Question: {question[:50]}...")

                        await self._broadcast(session, {
                            "type": "question",
                            "header": header,
                            "question": question,
                            "options": options,
                        })

                elif session.last_question and not ask_match:
                    # Question was answered (UI disappeared)
                    session.last_question = None
                    await self._broadcast(session, {"type": "question_answered"})

                # Check for activity status transitions
                current_status = self._get_session_activity_status(session)
                new_is_active = current_status == "active"

                # Broadcast transition event if state changed
                if new_is_active != session.is_active:
                    session.is_active = new_is_active
                    await self._broadcast(session, {
                        "type": "session_activity",
                        "session": session.name,
                        "active": new_is_active
                    })
                    logger.info(f"[{session.name}] Activity transition: {'active' if new_is_active else 'idle'}")

            except Exception as e:
                logger.debug(f"Output poll error for {session.name}: {e}")

            await asyncio.sleep(0.5)

    async def _broadcast(self, session: Session, message: dict):
        """Broadcast message to all session clients."""
        dead_clients = set()
        for client in session.clients:
            try:
                await client.send_json(message)
            except Exception:
                dead_clients.add(client)
        session.clients -= dead_clients


    # API Handlers

    async def api_sessions(self, request: web.Request) -> web.Response:
        """List all active sessions grouped by machine via CLI."""
        try:
            # Get local sessions via CLI
            local_success, local_result = await self.run_agentwire_cmd(["list", "--local", "--sessions"])
            local_sessions = local_result.get("sessions", []) if local_success else []

            # Get remote sessions via CLI (includes SSH checks)
            remote_success, remote_result = await self.run_agentwire_cmd(["list", "--remote", "--sessions"])
            remote_sessions = remote_result.get("sessions", []) if remote_success else []

            # Combine and add activity status
            all_sessions = local_sessions + remote_sessions
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

    async def api_sdk_sessions(self, request: web.Request) -> web.Response:
        """List saved SDK REPL sessions (under ~/.agentwire/sessions/repl/).

        These are the transcripts watch-mode tails. Newest first, capped at 50
        — extend if a real workload needs more.
        """
        try:
            from agentwire.repl import persistence
            sessions = persistence.list_sessions(limit=50)
            return web.json_response({"sessions": sessions})
        except Exception as e:
            logger.error(f"Failed to list sdk sessions: {e}")
            return web.json_response({"sessions": []})

    async def handle_sdk_watch_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Watch one SDK REPL session live by tailing its transcript JSONL.

        Read-only: events flow server → client only. Each event lands as one
        JSON message. Disconnect ends the tail; reconnect resumes from the
        top (the client can dedupe via event ts if needed).
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        name = request.match_info.get("name", "")
        if not name:
            await ws.send_json({"type": "error", "error": "missing session name"})
            await ws.close()
            return ws

        from agentwire.repl import persistence

        # Watcher pulls from disk; if the writer dies the watch ends gracefully.
        # Cap concurrent watchers per session at 8 so a runaway tab-explosion
        # doesn't pin file handles.
        async def stream() -> None:
            try:
                async for event in persistence.tail_transcript(name):
                    if ws.closed:
                        return
                    try:
                        await ws.send_json(event)
                    except (ConnectionResetError, RuntimeError):
                        return
            except FileNotFoundError:
                if not ws.closed:
                    await ws.send_json({"type": "error", "error": "session not found"})
            except Exception as exc:
                logger.warning("sdk-watch stream error %s: %s", name, exc)
                if not ws.closed:
                    try:
                        await ws.send_json({"type": "error", "error": str(exc)})
                    except Exception:
                        pass

        stream_task = asyncio.create_task(stream())
        try:
            async for msg in ws:
                # Watcher is read-only; ignore inbound messages.
                if msg.type == aiohttp.WSMsgType.CLOSE:
                    break
        finally:
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
        return ws

    async def api_sessions_local(self, request: web.Request) -> web.Response:
        """Fast endpoint for local sessions only (no SSH checks)."""
        try:
            success, result = await self.run_agentwire_cmd(["list", "--local", "--sessions"])
            if not success:
                return web.json_response({"sessions": []})

            sessions = result.get("sessions", [])
            # Add activity status
            for s in sessions:
                s["activity"] = self._get_global_session_activity(s.get("name", ""))

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

    async def _fetch_remote_machine_sessions(self, machine: dict) -> dict:
        """Fetch sessions for a specific remote machine. Used by CachedStatusChecker."""
        try:
            # Try to get sessions from this specific machine
            machine_id = machine.get("id")
            success, result = await self.run_agentwire_cmd(
                ["list", "--remote", "--sessions", "--machine", machine_id]
            )

            if not success:
                return {"status": "offline", "sessions": []}

            sessions = result.get("sessions", [])
            # Add activity status to each session
            for s in sessions:
                s["activity"] = self._get_global_session_activity(s.get("name", ""))

            return {
                "status": "online" if sessions else "online",  # Online but might have no sessions
                "sessions": sessions
            }
        except Exception:
            return {"status": "offline", "sessions": []}

    async def api_projects(self, request: web.Request) -> web.Response:
        """List discovered projects (progressive loading).

        Query params:
            machine: Optional machine ID to filter by (e.g., 'local', 'mac-studio')

        Response:
            {"projects": [{name, path, type, roles, machine, status}, ...]}
        """
        try:
            # Get list of machines to scan
            machine_filter = request.query.get("machine")

            if machine_filter:
                # Single machine requested - use checker
                machines = [{"id": machine_filter}]
                scanned_machines = await self.projects_checker.get_with_status(
                    machines,
                    check_fn=self._scan_machine_projects,
                    id_field='id'
                )
                all_projects = []
                for machine_data in scanned_machines:
                    projects = machine_data.get("projects", [])
                    logger.debug(f"[api_projects] Machine {machine_data.get('id')} returned {len(projects)} projects (filtered request)")
                    all_projects.extend(projects)
            else:
                # All machines - get local first (fast), then remote (progressive)
                all_projects = []

                # Local projects (always fast, no caching needed)
                local_result = await self._scan_machine_projects({"id": "local"})
                local_projects = local_result.get("projects", [])
                logger.debug(f"[api_projects] Local scan returned {len(local_projects)} projects")
                all_projects.extend(local_projects)

                # Remote projects (progressive with caching)
                machines_file = self.config.machines.file
                if machines_file.exists():
                    with open(machines_file) as f:
                        data = json.load(f)
                        remote_machines = [
                            {"id": m.get("id")}
                            for m in data.get("machines", [])
                        ]
                        logger.debug(f"[api_projects] Found {len(remote_machines)} remote machines: {[m['id'] for m in remote_machines]}")

                        if remote_machines:
                            scanned_machines = await self.projects_checker.get_with_status(
                                remote_machines,
                                check_fn=self._scan_machine_projects,
                                id_field='id'
                            )
                            logger.debug(f"[api_projects] Checker returned {len(scanned_machines)} machine results")

                            # Track if any machines are still checking
                            has_checking = False

                            for machine_data in scanned_machines:
                                machine_id = machine_data.get("id", "unknown")
                                machine_status = machine_data.get("status", "unknown")
                                projects = machine_data.get("projects", [])
                                logger.debug(f"[api_projects] Machine {machine_id} (status: {machine_status}) has {len(projects)} projects: {[p.get('name', 'unnamed') for p in projects]}")

                                if machine_status == "checking":
                                    has_checking = True

                                # Add machine status to projects for frontend progressive loading
                                for project in projects:
                                    project["_machineStatus"] = machine_status

                                all_projects.extend(projects)

                logger.debug(f"[api_projects] Total projects before dedup: {len(all_projects)}")

                # Deduplicate by normalized path
                # Normalize paths to handle ~/projects vs /Users/user/projects
                def normalize_path(path: str) -> str:
                    """Normalize path for comparison (expand ~, resolve relative paths)."""
                    if not path:
                        return ""
                    # Expand ~ to home directory
                    if path.startswith("~/"):
                        # Use a consistent home path for comparison
                        import os
                        home = os.path.expanduser("~")
                        return path.replace("~", home, 1)
                    return path

                seen_normalized = set()
                deduped_projects = []
                duplicates = []
                for project in all_projects:
                    path = project.get("path")
                    if not path:
                        continue

                    machine = project.get("machine", "local")
                    dedup_key = f"{machine}:{normalize_path(path)}"
                    if dedup_key not in seen_normalized:
                        seen_normalized.add(dedup_key)
                        deduped_projects.append(project)
                    else:
                        # Prefer local version over remote for same project
                        duplicates.append(f"{project.get('name')} ({project.get('machine')})")

                if duplicates:
                    logger.debug(f"[api_projects] Removed {len(duplicates)} duplicates: {', '.join(duplicates)}")

                logger.debug(f"[api_projects] Total projects after dedup: {len(deduped_projects)}")
                all_projects = deduped_projects

            # Return projects with scanning status for auto-refresh
            response = {"projects": all_projects}
            if 'has_checking' in locals():
                response["_scanning"] = has_checking

            return web.json_response(response)
        except Exception as e:
            logger.error(f"Failed to list projects: {e}")
            return web.json_response({"projects": []})

    async def _scan_machine_projects(self, machine: dict) -> dict:
        """Scan projects on a specific machine. Used by CachedStatusChecker."""
        machine_id = machine.get("id")
        try:
            args = ["projects", "list", "--machine", machine_id]

            success, result = await self.run_agentwire_cmd(args)
            if not success:
                logger.warning(f"Failed to scan projects on {machine_id}: {result.get('error', 'unknown error')}")
                return {"status": "offline", "projects": []}

            projects = result.get("projects", [])
            logger.debug(f"Found {len(projects)} projects on {machine_id}")
            return {
                "status": "online",
                "projects": projects
            }
        except Exception as e:
            logger.error(f"Exception scanning projects on {machine_id}: {e}")
            return {"status": "offline", "projects": []}

    async def api_projects_delete(self, request: web.Request) -> web.Response:
        """Delete a project (remove .agentwire.yml or entire folder).

        Body:
            {
                "path": "/path/to/project",
                "machine": "machine-id" or null for local,
                "deleteType": "config" | "folder"
            }

        Response:
            {"success": true} or {"success": false, "error": "message"}
        """
        try:
            data = await request.json()
            path = data.get("path")
            machine = data.get("machine")
            delete_type = data.get("deleteType")

            if not path:
                return web.json_response({"success": False, "error": "Missing path"})
            if delete_type not in ("config", "folder"):
                return web.json_response({"success": False, "error": "Invalid deleteType"})

            # Build the delete command
            if delete_type == "config":
                cmd = f"rm -f '{path}/.agentwire.yml'"
            else:
                # Safety check: don't allow deleting root or home
                if path in ("/", "/root", "/home") or path.rstrip("/") in ("~", "$HOME"):
                    return web.json_response({"success": False, "error": "Cannot delete protected paths"})
                cmd = f"rm -rf '{path}'"

            # Execute locally or remotely
            if machine and machine != "local":
                # Remote machine
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["ssh", machine, cmd],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
            else:
                # Local
                result = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

            if result.returncode != 0:
                return web.json_response({
                    "success": False,
                    "error": result.stderr or "Delete command failed"
                })

            return web.json_response({"success": True})

        except asyncio.TimeoutError:
            return web.json_response({"success": False, "error": "Operation timed out"})
        except Exception as e:
            logger.error(f"Failed to delete project: {e}")
            return web.json_response({"success": False, "error": str(e)})

    async def api_roles(self, request: web.Request) -> web.Response:
        """List available roles.

        Response:
            {"roles": [{name, description}, ...]}
        """
        try:
            success, result = await self.run_agentwire_cmd(["roles", "list"])
            if not success:
                return web.json_response({"roles": []})

            return web.json_response({"roles": result.get("roles", [])})
        except Exception as e:
            logger.error(f"Failed to list roles: {e}")
            return web.json_response({"roles": []})

    async def api_machine_status(self, request: web.Request) -> web.Response:
        """Get status for a specific machine.

        Returns online/offline status and session count for a machine.

        URL params:
            machine_id: The machine ID to check

        Response:
            {
                "status": "online" | "offline",
                "session_count": <int>
            }
        """
        machine_id = request.match_info["machine_id"]

        try:
            # Load machines config
            machines_dict = {}
            if hasattr(self.agent, 'machines'):
                for m in self.agent.machines:
                    machines_dict[m.get('id')] = m

            machine_config = machines_dict.get(machine_id)
            if not machine_config:
                return web.json_response(
                    {"status": "offline", "session_count": 0},
                    status=404
                )

            # Check machine status
            status = await self._check_machine_status(machine_config)

            # Count sessions for this machine
            sessions = self.agent.list_sessions()
            session_count = 0
            for name in sessions:
                _, _, session_machine = parse_session_name(name)
                if session_machine == machine_id:
                    session_count += 1

            return web.json_response({
                "status": status,
                "session_count": session_count,
            })
        except Exception as e:
            logger.error(f"Failed to get machine status for {machine_id}: {e}")
            return web.json_response(
                {"status": "offline", "session_count": 0},
                status=500
            )

    async def api_check_path(self, request: web.Request) -> web.Response:
        """Check if a path exists and is a git repo.

        Query params:
            path: The path to check
            machine: Machine ID ('local' or remote machine ID)

        Returns:
            {exists: bool, is_git: bool, current_branch: str|null}
        """
        path = request.query.get("path", "")
        machine = request.query.get("machine", "local")

        if not path:
            return web.json_response({
                "exists": False,
                "is_git": False,
                "current_branch": None
            })

        if machine and machine != "local":
            # Remote path check via SSH
            result = await self._run_ssh_command(
                machine,
                f"test -d {shlex.quote(path)} && echo exists"
            )
            exists = "exists" in result
            is_git = False
            current_branch = None

            if exists:
                result = await self._run_ssh_command(
                    machine,
                    f"test -d {shlex.quote(path)}/.git && echo git"
                )
                is_git = "git" in result

                if is_git:
                    result = await self._run_ssh_command(
                        machine,
                        f"cd {shlex.quote(path)} && git rev-parse --abbrev-ref HEAD"
                    )
                    current_branch = result.strip() if result else None
        else:
            # Local path check
            expanded = Path(path).expanduser().resolve()
            exists = expanded.exists() and expanded.is_dir()
            is_git = exists and (expanded / ".git").exists()
            current_branch = None

            if is_git:
                result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=expanded,
                    capture_output=True,
                    text=True
                )
                current_branch = result.stdout.strip() if result.returncode == 0 else None

        return web.json_response({
            "exists": exists,
            "is_git": is_git,
            "current_branch": current_branch
        })

    async def api_check_branches(self, request: web.Request) -> web.Response:
        """Get existing branch names matching a prefix.

        Query params:
            path: The git repo path
            machine: Machine ID ('local' or remote machine ID)
            prefix: Branch name prefix to filter by

        Returns:
            {existing: [branch names]}
        """
        path = request.query.get("path", "")
        machine = request.query.get("machine", "local")
        prefix = request.query.get("prefix", "")

        if not path:
            return web.json_response({"existing": []})

        if machine and machine != "local":
            # Remote branch check via SSH
            cmd = f"cd {shlex.quote(path)} && git branch --list {shlex.quote(prefix + '*')} --format='%(refname:short)'"
            result = await self._run_ssh_command(machine, cmd)
            branches = result.strip().split('\n') if result else []
        else:
            # Local branch check
            expanded = Path(path).expanduser().resolve()
            if not expanded.exists():
                return web.json_response({"existing": []})

            result = subprocess.run(
                ["git", "branch", "--list", f"{prefix}*", "--format=%(refname:short)"],
                cwd=expanded,
                capture_output=True,
                text=True
            )
            branches = result.stdout.strip().split('\n') if result.returncode == 0 else []

        # Filter out empty strings
        branches = [b for b in branches if b]

        return web.json_response({"existing": branches})

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
            session_type = data.get("type", "claude-bypass")
            roles = data.get("roles")
            machine = data.get("machine", "local")
            worktree = data.get("worktree", False)
            branch = data.get("branch", "").strip()

            if not name:
                return web.json_response({"error": "Session name is required"})

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
            # Set session type via --type flag
            args.extend(["--type", session_type])
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

                # Filter to only valid roles
                valid_roles = [r for r in roles_list if r in available_roles]

                # Fall back to default role if none are valid
                if not valid_roles and roles_list:
                    logger.warning(f"No valid roles found in {roles_list}, using default role")
                    default_role = self.config.session.default_role
                    if default_role:
                        valid_roles = [default_role]

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
                yaml_config = self._read_agentwire_yaml(session_path, machine_id) or {}
                yaml_config["voice"] = voice
                self._write_agentwire_yaml(session_path, yaml_config, machine_id)

            # Broadcast session created to dashboard clients
            await self.broadcast_dashboard("session_created", {"session": session_name})
            sessions_data = await self._get_sessions_data()
            await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

            return web.json_response({"success": True, "name": session_name})

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
            cwd = self._get_session_cwd(base_name, machine_id)
            if not cwd:
                return web.json_response({"error": "Session working directory not found"}, status=404)

            # Read existing .agentwire.yml (or create new)
            yaml_config = self._read_agentwire_yaml(cwd, machine_id) or {}

            # Update voice
            yaml_config["voice"] = voice

            # Write back
            if not self._write_agentwire_yaml(cwd, yaml_config, machine_id):
                return web.json_response({"error": "Failed to write .agentwire.yml"}, status=500)

            # Update live session if exists
            if name in self.active_sessions:
                self.active_sessions[name].config.voice = voice

            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_voices(self, request: web.Request) -> web.Response:
        """Get available TTS voices."""
        voices = await self._get_voices()
        return web.json_response(voices)

    async def api_icons(self, request: web.Request) -> web.Response:
        """Get list of icon files for a category (sessions, machines, projects).

        Returns { custom: [...], default: [...] } where:
        - custom: icons in /custom/ subfolder (used for name matching)
        - default: icons in main folder (used for random assignment)
        """
        category = request.match_info["category"]
        if category not in ("sessions", "machines", "projects"):
            return web.json_response({"error": "Invalid category"}, status=400)

        icons_dir = Path(__file__).parent / "static" / "icons" / category
        if not icons_dir.exists():
            return web.json_response({"custom": [], "default": []})

        def list_images(directory: Path) -> list[str]:
            if not directory.exists():
                return []
            return sorted([
                f.name for f in directory.iterdir()
                if f.is_file() and f.suffix.lower() in (".png", ".jpeg", ".jpg")
            ])

        # Custom icons for name matching
        custom_icons = list_images(icons_dir / "custom")

        # Default icons for random assignment (main folder only)
        default_icons = list_images(icons_dir)

        return web.json_response({"custom": custom_icons, "default": default_icons})

    async def api_machines(self, request: web.Request) -> web.Response:
        """Get list of all machines (local + configured remotes).

        Uses progressive loading pattern - returns immediately with status='checking',
        background checks populate cache for subsequent requests.
        """
        machines = []

        # Always include local machine first
        local_hostname = socket.gethostname()
        local_ip = await self._resolve_hostname(local_hostname)
        machines.append({
            "id": "local",
            "host": local_hostname,
            "ip": local_ip,
            "local": True,
            "status": "online",
        })

        # Add configured remote machines using progressive loading pattern
        machines_file = self.config.machines.file
        if machines_file.exists():
            try:
                with open(machines_file) as f:
                    data = json.load(f)
                    remote_machines = [
                        {**m, "local": False}
                        for m in data.get("machines", [])
                    ]

                    # Progressive loading: returns cached or "checking" status
                    checked_machines = await self.machine_status_checker.get_with_status(
                        remote_machines,
                        check_fn=self._check_machine_with_ip,
                        id_field='id'
                    )

                    machines.extend(checked_machines)

            except (json.JSONDecodeError, IOError):
                pass

        return web.json_response(machines)

    async def _check_machine_with_ip(self, machine: dict) -> dict:
        """Check machine status and resolve IP. Used by CachedStatusChecker."""
        status = await self._check_machine_status(machine, quick=True)
        ip = None
        if status == "online":
            ip = await self._resolve_hostname(machine.get("host", ""))
        return {"status": status, "ip": ip}

    async def _resolve_hostname(self, hostname: str) -> str | None:
        """Resolve hostname to IP address.

        Tries DNS first, then falls back to SSH config resolution,
        and finally queries the remote machine for its IP.
        """
        if not hostname:
            return None

        # Try DNS lookup first
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, socket.gethostbyname, hostname)
            return result
        except (socket.gaierror, socket.herror):
            pass

        # DNS failed, try SSH config to get the actual hostname/IP
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-G", hostname,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            # Parse output for "hostname <value>"
            ssh_hostname = None
            for line in stdout.decode().splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and parts[0].lower() == "hostname":
                    ssh_hostname = parts[1]
                    break

            if ssh_hostname:
                # Check if it's already an IP address
                if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ssh_hostname):
                    return ssh_hostname

                # Try DNS on the resolved hostname
                try:
                    result = await loop.run_in_executor(None, socket.gethostbyname, ssh_hostname)
                    return result
                except (socket.gaierror, socket.herror):
                    pass

                # DNS failed, try connecting via SSH to get the remote IP
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
                        hostname, "hostname -I 2>/dev/null || ip addr show | grep 'inet ' | grep -v '127.0.0.1' | head -1 | awk '{print $2}' | cut -d/ -f1",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
                    remote_ip = stdout.decode().strip().split()[0] if stdout else None
                    if remote_ip and re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', remote_ip):
                        return remote_ip
                except (asyncio.TimeoutError, OSError, IndexError):
                    pass

        except (asyncio.TimeoutError, OSError):
            pass

        return None

    async def _check_machine_status(self, machine: dict, quick: bool = False) -> str:
        """Check if a remote machine is reachable via SSH.

        Args:
            machine: Machine dict with host/user info
            quick: If True, use very short timeout for fast initial check
        """
        host = machine.get("host", "")
        user = machine.get("user", "")
        ssh_target = f"{user}@{host}" if user else host

        # Use shorter timeout for quick checks
        connect_timeout = "1" if quick else "2"
        wait_timeout = 1.5 if quick else 3.0

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "ssh", "-o", f"ConnectTimeout={connect_timeout}", "-o", "BatchMode=yes",
                    ssh_target, "echo ok",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                ),
                timeout=wait_timeout
            )
            await proc.wait()
            return "online" if proc.returncode == 0 else "offline"
        except (asyncio.TimeoutError, Exception):
            return "offline"

    async def api_add_machine(self, request: web.Request) -> web.Response:
        """Add a new machine to the registry."""
        try:
            data = await request.json()
            machine_id = data.get("id", "").strip()
            host = data.get("host", "").strip()
            user = data.get("user", "").strip()
            projects_dir = data.get("projects_dir", "").strip()

            if not machine_id or not host:
                return web.json_response({"error": "ID and host are required"})

            machines_file = self.config.machines.file
            machines_file.parent.mkdir(parents=True, exist_ok=True)

            # Load existing machines
            machines = []
            if machines_file.exists():
                try:
                    with open(machines_file) as f:
                        machines = json.load(f).get("machines", [])
                except (json.JSONDecodeError, IOError):
                    pass

            # Check for duplicate ID
            if any(m.get("id") == machine_id for m in machines):
                return web.json_response({"error": f"Machine '{machine_id}' already exists"})

            # Add new machine
            new_machine = {"id": machine_id, "host": host}
            if user:
                new_machine["user"] = user
            if projects_dir:
                new_machine["projects_dir"] = projects_dir

            machines.append(new_machine)

            # Save
            with open(machines_file, "w") as f:
                json.dump({"machines": machines}, f, indent=2)

            # Reload agent backend to pick up new machines
            if self.agent and hasattr(self.agent, '_load_machines'):
                self.agent._load_machines()

            return web.json_response({"success": True, "machine": new_machine})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_remove_machine(self, request: web.Request) -> web.Response:
        """Remove a machine from the registry."""
        machine_id = request.match_info["machine_id"]

        try:
            # Can't remove local machine
            if machine_id == "local":
                return web.json_response({"error": "Cannot remove local machine"})

            machines_file = self.config.machines.file
            if not machines_file.exists():
                return web.json_response({"error": "No machines configured"})

            # Load machines
            try:
                with open(machines_file) as f:
                    data = json.load(f)
                    machines = data.get("machines", [])
            except (json.JSONDecodeError, IOError) as e:
                return web.json_response({"error": f"Failed to read machines file: {e}"})

            # Check if machine exists
            machine = next((m for m in machines if m.get("id") == machine_id), None)
            if not machine:
                return web.json_response({"error": f"Machine '{machine_id}' not found"})

            # Remove from machines list
            machines = [m for m in machines if m.get("id") != machine_id]

            # Save updated machines file
            with open(machines_file, "w") as f:
                json.dump({"machines": machines}, f, indent=2)
                f.write("\n")

            # No sessions.json to clean up - config is now in .agentwire.yml per project

            # Reload agent backend to pick up changes
            if self.agent and hasattr(self.agent, '_load_machines'):
                self.agent._load_machines()

            return web.json_response({
                "success": True,
                "machine_id": machine_id,
            })

        except Exception as e:
            logger.error(f"Failed to remove machine: {e}")
            return web.json_response({"error": str(e)})

    async def api_get_config(self, request: web.Request) -> web.Response:
        """Get config file contents or display format.

        Query params:
            format=display - Return key/value pairs for UI display
        """
        # Check if display format requested
        if request.query.get("format") == "display":
            # Return flattened key/value pairs from current config
            items = [
                {"key": "TTS Backend", "value": self.config.tts.backend},
                {"key": "TTS URL", "value": self.config.tts.url},
                {"key": "TTS Default Voice", "value": self.config.tts.default_voice},
                {"key": "STT URL", "value": self.config.stt.url},
                {"key": "Server Host", "value": self.config.server.host},
                {"key": "Server Port", "value": self.config.server.port},
                {"key": "SSL Enabled", "value": self.config.server.ssl.enabled},
                {"key": "Projects Directory", "value": str(self.config.projects.dir)},
                {"key": "Worktrees Enabled", "value": self.config.projects.worktrees.enabled},
                {"key": "Worktrees Suffix", "value": self.config.projects.worktrees.suffix},
                {"key": "Agent Command", "value": self.config.agent.command},
                {"key": "Machines File", "value": str(self.config.machines.file)},
            ]
            return web.json_response({"items": items})

        # Default: return raw config file contents
        config_path = Path.home() / ".agentwire" / "config.yaml"
        content = ""
        if config_path.exists():
            try:
                content = config_path.read_text()
                # SECURITY: Redact sensitive fields before returning
                # Matches patterns like: runpod_api_key: "secret" or runpod_api_key: secret
                content = re.sub(
                    r'(runpod_api_key\s*:\s*)["\']?[^"\'\n]+["\']?',
                    r'\1"[REDACTED]"',
                    content
                )
            except IOError as e:
                return web.json_response({"error": str(e)})
        else:
            # Return default config template
            content = """# AgentWire Configuration
server:
  host: "0.0.0.0"
  port: 8765

tts:
  backend: "chatterbox"
  url: "http://localhost:8100"
  default_voice: "default"

projects:
  dir: "~/projects"
  worktrees:
    enabled: true
    suffix: "-worktrees"
"""
        return web.json_response({
            "path": str(config_path),
            "content": content,
            "exists": config_path.exists(),
        })

    async def api_save_config(self, request: web.Request) -> web.Response:
        """Save config file contents."""
        try:
            data = await request.json()
            content = data.get("content", "")

            # Validate YAML syntax
            import yaml
            try:
                yaml.safe_load(content)
            except yaml.YAMLError as e:
                return web.json_response({"error": f"Invalid YAML: {e}"})

            config_path = Path.home() / ".agentwire" / "config.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(content)

            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_reload_config(self, request: web.Request) -> web.Response:
        """Reload configuration from disk."""
        try:
            from .config import reload_config
            self.config = reload_config()

            # Reinitialize backends with new config
            await self.init_backends()

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

    async def handle_transcribe(self, request: web.Request) -> web.Response:
        """Transcribe audio to text."""
        try:
            reader = await request.multipart()
            audio_field = await reader.next()

            if audio_field is None:
                return web.json_response({"error": "No audio data"})

            # Read audio data
            audio_data = await audio_field.read()

            if not audio_data:
                return web.json_response({"error": "Empty audio data"})

            # Save webm to temp file
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
                f.write(audio_data)
                webm_path = f.name

            # Convert webm to wav (16kHz mono for Whisper)
            wav_path = webm_path.replace(".webm", ".wav")
            try:
                logger.info("Converting webm to wav: %s -> %s", webm_path, wav_path)
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-i", webm_path,
                    "-ar", "16000", "-ac", "1", "-y", wav_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()

                if proc.returncode != 0 or not Path(wav_path).exists():
                    logger.error("Failed to convert webm to wav (ffmpeg returned %d)", proc.returncode)
                    return web.json_response({"error": "Audio conversion failed"})

                # Transcribe the wav file
                logger.info("Transcribing %s via %s backend", wav_path, type(self.stt).__name__)
                text = await self.stt.transcribe(Path(wav_path))
                logger.info("Transcription result: %s", text)
                return web.json_response({"text": text})
            finally:
                Path(webm_path).unlink(missing_ok=True)
                Path(wav_path).unlink(missing_ok=True)

        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return web.json_response({"error": str(e)})

    async def handle_upload(self, request: web.Request) -> web.Response:
        """Upload an image file for attachment to messages."""
        try:
            reader = await request.multipart()
            image_field = await reader.next()

            if image_field is None:
                return web.json_response({"error": "No image data"})

            # Check content type (try property, header, and filename extension)
            content_type = getattr(image_field, 'content_type', None) or image_field.headers.get("Content-Type", "")
            filename = image_field.filename or ""
            logger.debug(f"Upload content_type: {content_type}, filename: {filename}")

            # Fallback: detect from filename extension
            if not content_type or not content_type.startswith("image/"):
                ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
                ext_to_mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
                if ext in ext_to_mime:
                    content_type = ext_to_mime[ext]
                    logger.debug(f"Detected content_type from extension: {content_type}")

            if not content_type.startswith("image/"):
                return web.json_response({"error": f"File must be an image (got {content_type or 'unknown'})"})

            # Read image data
            image_data = await image_field.read()

            if not image_data:
                return web.json_response({"error": "Empty image data"})

            # Check file size
            max_bytes = self.config.uploads.max_size_mb * 1024 * 1024
            if len(image_data) > max_bytes:
                return web.json_response({
                    "error": f"File too large (max {self.config.uploads.max_size_mb}MB)"
                })

            # Ensure uploads directory exists
            uploads_dir = self.config.uploads.dir
            uploads_dir.mkdir(parents=True, exist_ok=True)

            # Generate unique filename
            ext = content_type.split("/")[-1]
            if ext == "jpeg":
                ext = "jpg"
            filename = f"{int(time.time())}-{uuid.uuid4().hex[:8]}.{ext}"
            filepath = uploads_dir / filename

            # Save file
            filepath.write_bytes(image_data)
            logger.info(f"Uploaded image: {filepath}")

            return web.json_response({
                "path": str(filepath),
                "filename": filename
            })

        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return web.json_response({"error": str(e)})

    async def handle_send(self, request: web.Request) -> web.Response:
        """Send text to an agent session via CLI."""
        name = request.match_info["name"]
        try:
            data = await request.json()
            text = data.get("text", "").strip()

            if not text:
                return web.json_response({"error": "No text provided"})

            # Notify dashboard that session is now processing (for agentwire indicator)
            await self.broadcast_dashboard("session_processing", {"session": name, "processing": True})

            # Use CLI: agentwire send -s <session> <text>
            success, result = await self.run_agentwire_cmd(["send", "-s", name, text])

            if not success:
                error_msg = result.get("error", "Failed to send to session")
                return web.json_response({"error": error_msg})

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Send failed: {e}")
            return web.json_response({"error": str(e)})

    # TTS Integration

    def _prepend_silence(self, wav_data: bytes, ms: int = 300) -> bytes:
        """Prepend silence to WAV audio to prevent first syllable cutoff.

        Works with any WAV format (PCM, IEEE Float, etc.) by directly
        manipulating the raw bytes.

        Args:
            wav_data: Original WAV file bytes
            ms: Milliseconds of silence to prepend

        Returns:
            New WAV bytes with silence prepended
        """
        try:
            # Parse WAV header to get format info
            # RIFF header: 12 bytes, fmt chunk: variable, data chunk: variable
            if len(wav_data) < 44 or wav_data[:4] != b'RIFF' or wav_data[8:12] != b'WAVE':
                return wav_data

            # Find fmt chunk
            pos = 12
            sample_rate = 24000  # default
            bytes_per_sample = 4  # default for float32
            channels = 1

            while pos < len(wav_data) - 8:
                chunk_id = wav_data[pos:pos+4]
                chunk_size = struct.unpack('<I', wav_data[pos+4:pos+8])[0]

                if chunk_id == b'fmt ':
                    # fmt chunk: format(2), channels(2), sample_rate(4), byte_rate(4), block_align(2), bits_per_sample(2)
                    channels = struct.unpack('<H', wav_data[pos+10:pos+12])[0]
                    sample_rate = struct.unpack('<I', wav_data[pos+12:pos+16])[0]
                    bits_per_sample = struct.unpack('<H', wav_data[pos+22:pos+24])[0]
                    bytes_per_sample = bits_per_sample // 8

                elif chunk_id == b'data':
                    # Found data chunk - insert silence here
                    data_start = pos + 8
                    original_data = wav_data[data_start:data_start + chunk_size]

                    # Calculate silence
                    silence_samples = int(sample_rate * ms / 1000)
                    silence_bytes = b'\x00' * (silence_samples * bytes_per_sample * channels)

                    # New data size
                    new_data_size = len(silence_bytes) + len(original_data)
                    new_file_size = len(wav_data) - chunk_size + new_data_size - 8

                    # Rebuild WAV
                    result = bytearray(wav_data[:4])  # RIFF
                    result += struct.pack('<I', new_file_size)  # New file size
                    result += wav_data[8:pos+4]  # Up to data chunk id
                    result += struct.pack('<I', new_data_size)  # New data size
                    result += silence_bytes  # Prepended silence
                    result += original_data  # Original audio

                    return bytes(result)

                pos += 8 + chunk_size
                if chunk_size % 2:  # Chunks are word-aligned
                    pos += 1

            return wav_data
        except Exception as e:
            logger.warning(f"Failed to prepend silence: {e}")
            return wav_data

    async def _say_to_room(self, session_name: str, text: str):
        """Generate TTS audio and send to session clients (internal)."""
        await self.speak(session_name, text)

    async def api_say(self, request: web.Request) -> web.Response:
        """POST /api/say/{session} - Generate TTS and broadcast to session."""
        name = request.match_info["name"]
        try:
            data = await request.json()
            text = data.get("text", "").strip()

            if not text:
                return web.json_response({"error": "No text provided"}, status=400)

            # Ensure session exists (create if not)
            if name not in self.active_sessions:
                self.active_sessions[name] = Session(name=name, config=self._get_session_config(name))

            session = self.active_sessions[name]

            # Track this text to avoid duplicate TTS from output polling
            session.played_says.add(text)
            if len(session.played_says) > 50:
                session.played_says = set(list(session.played_says)[-25:])

            # Count chunks for the response (speak() does the actual chunking)
            from .utils.chunker import chunk_text
            chunks = chunk_text(text)
            chunk_count = len(chunks)

            logger.info(f"[{name}] API say: {text[:50]}... ({chunk_count} chunk(s))")

            # Generate and broadcast TTS in background (don't block the API response)
            # speak() handles chunking sequentially — guaranteed playback order
            asyncio.create_task(self.speak(name, text))

            return web.json_response({"success": True, "chunks": chunk_count})

        except Exception as e:
            logger.error(f"Say API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

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

    async def api_local_tts(self, request: web.Request) -> web.Response:
        """POST /api/local-tts/{session} - Generate TTS and return audio for local playback."""
        name = request.match_info["name"]
        try:
            data = await request.json()
            text = data.get("text", "").strip()
            voice = data.get("voice")

            if not text:
                return web.json_response({"error": "No text provided"}, status=400)

            # Get session config for defaults
            session_config = self._get_session_config(name)
            if voice is None:
                voice = session_config.voice
            exaggeration = session_config.exaggeration
            cfg_weight = session_config.cfg_weight

            logger.info(f"[{name}] Local TTS: {text[:50]}... (voice={voice})")

            # Generate audio via TTS server HTTP call
            audio_data = await self._tts_generate(
                text=text,
                voice=voice,
                exaggeration=exaggeration,
                cfg_weight=cfg_weight,
            )

            if not audio_data:
                return web.json_response(
                    {"success": False, "error": "TTS generation returned no audio"},
                    status=500
                )

            # Save to temp file
            temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            try:
                temp_file.write(audio_data)
                temp_path = temp_file.name
                temp_file.close()

                # Play audio (platform-specific)
                import sys
                if sys.platform == "darwin":
                    # macOS: use afplay
                    proc = await asyncio.create_subprocess_exec(
                        "afplay", temp_path,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    await proc.wait()
                elif sys.platform == "linux":
                    # Linux: try aplay, paplay, play in order
                    players = ["aplay", "paplay", "play"]
                    played = False
                    for player in players:
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                player, temp_path,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL
                            )
                            await proc.wait()
                            played = True
                            break
                        except FileNotFoundError:
                            continue

                    if not played:
                        logger.warning("No audio player found (tried aplay, paplay, play)")
                        return web.json_response(
                            {"success": False, "error": "No audio player available"},
                            status=500
                        )
                else:
                    logger.warning(f"Local TTS playback not supported on platform: {sys.platform}")
                    return web.json_response(
                        {"success": False, "error": f"Platform not supported: {sys.platform}"},
                        status=500
                    )

                return web.json_response({"success": True})

            finally:
                # Clean up temp file
                Path(temp_path).unlink(missing_ok=True)

        except asyncio.TimeoutError:
            logger.error(f"TTS generation timeout for: {text[:50]}...")
            return web.json_response(
                {"success": False, "error": "TTS generation timeout"},
                status=500
            )
        except Exception as e:
            logger.error(f"Local TTS API failed: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def api_answer(self, request: web.Request) -> web.Response:
        """POST /api/answer/{session} - Answer an AskUserQuestion prompt."""
        name = request.match_info["name"]
        try:
            data = await request.json()
            answer = data.get("answer", "").strip()
            is_custom = data.get("custom", False)
            option_number = data.get("option_number")  # For "type something" flow

            if not answer:
                return web.json_response({"error": "No answer provided"}, status=400)

            # Three modes:
            # 1. Regular option: just send the number key (no Enter)
            # 2. "Type something" option: send number key, wait, send text + Enter
            # 3. Direct custom: just send text + Enter (free-form input without numbered option)
            if option_number:
                # "Type something" flow: select option first (no Enter), then type
                self.agent.send_keys(name, str(option_number))
                await asyncio.sleep(0.5)  # Wait for Claude to show text input
                success = self.agent.send_input(name, answer)  # text + Enter
            elif is_custom:
                # Direct custom answer: type the text and press Enter
                success = self.agent.send_input(name, answer)
            else:
                # Just send the number key - AskUserQuestion responds to single keypress
                success = self.agent.send_keys(name, str(answer))

            if not success:
                return web.json_response({"error": "Failed to send answer"}, status=500)

            # Notify clients the question was answered
            if name in self.active_sessions:
                session = self.active_sessions[name]
                session.last_question = None
                await self._broadcast(session, {"type": "question_answered"})

            logger.info(f"[{name}] Answered: {answer}")
            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Answer API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_permission_request(self, request: web.Request) -> web.Response:
        """POST /api/permission/{session} - Handle permission request from Claude Code hook.

        This endpoint is called by the permission hook script when Claude Code
        needs permission for an action. It broadcasts the request to connected
        clients and waits for a response.

        In restricted mode, only say commands are auto-allowed,
        everything else is auto-denied silently.
        """
        name = request.match_info["name"]
        try:
            data = await request.json()
            tool_name = data.get("tool_name", "unknown")
            tool_input = data.get("tool_input", {})
            message = data.get("message", "")

            logger.info(f"[{name}] Permission request: {tool_name}")

            # Ensure session exists
            if name not in self.active_sessions:
                self.active_sessions[name] = Session(name=name, config=self._get_session_config(name))

            session = self.active_sessions[name]

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
                        try:
                            # Use CLI for consistent behavior (handles local and remote)
                            # Send "1" to select "Yes" option in permission prompt
                            session_target = f"{tmux_session}@{machine}" if machine else tmux_session
                            subprocess.run(
                                ["agentwire", "send-keys", "-s", session_target, "1"],
                                check=True, capture_output=True
                            )
                        except Exception as e:
                            logger.error(f"[{name}] Failed to send allow keystroke: {e}")
                    return web.json_response({"decision": "allow_always"})
                else:
                    # Auto-deny: send "Escape" keystroke (deny silently)
                    logger.info(f"[{name}] Restricted mode: auto-denying {tool_name}")
                    try:
                        # Use CLI for consistent behavior (handles local and remote)
                        session_target = f"{tmux_session}@{machine}" if machine else tmux_session
                        subprocess.run(
                            ["agentwire", "send-keys", "-s", session_target, "Escape"],
                            check=True, capture_output=True
                        )
                    except Exception as e:
                        logger.error(f"[{name}] Failed to send deny keystroke: {e}")
                    return web.json_response({
                        "decision": "deny",
                        "message": "Restricted mode: only say commands are allowed"
                    })

            # Create pending permission request (normal/prompted mode)
            session.pending_permission = PendingPermission(request=data)

            # Broadcast permission request to all clients (Task 3.1)
            await self._broadcast(session, {
                "type": "permission_request",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "message": message,
            })

            # Generate TTS announcement (Task 3.6)
            await self._announce_permission_request(name, tool_name, tool_input)

            # Wait for user decision with 5 minute timeout
            try:
                await asyncio.wait_for(session.pending_permission.event.wait(), timeout=300)
            except asyncio.TimeoutError:
                logger.warning(f"[{name}] Permission request timed out")
                session.pending_permission = None
                await self._broadcast(session, {"type": "permission_timeout"})
                return web.json_response({
                    "decision": "deny",
                    "message": "Permission request timed out (5 minutes)"
                })

            # Return the decision to the hook script
            decision = session.pending_permission.decision
            session.pending_permission = None

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

            # Store decision and signal the waiting request
            session.pending_permission.decision = {"decision": decision}
            if decision == "deny":
                session.pending_permission.decision["message"] = data.get("message", "User denied permission")
            session.pending_permission.event.set()

            # Send keystroke to session to respond to Claude's interactive prompt
            # Use CLI for consistent behavior (handles local and remote via session@machine format)
            try:
                import subprocess

                if decision == "custom":
                    # Custom feedback: send "3", then message, then Enter
                    custom_message = data.get("message", "")
                    if custom_message:
                        # send-keys handles pauses between key groups
                        subprocess.run(
                            ["agentwire", "send-keys", "-s", name, "3", custom_message, "Enter"],
                            check=True, capture_output=True
                        )
                        logger.info(f"[{name}] Sent custom feedback: {custom_message[:50]}...")
                else:
                    # Map decision to keystroke: allow=1, allow_always=2, deny=Escape
                    keystroke_map = {
                        "allow": "1",
                        "allow_always": "2",
                        "deny": "Escape",
                    }
                    keystroke = keystroke_map.get(decision, "Escape")
                    subprocess.run(
                        ["agentwire", "send-keys", "-s", name, keystroke],
                        check=True, capture_output=True
                    )
                    logger.info(f"[{name}] Sent keystroke '{keystroke}' to session")
            except Exception as e:
                logger.error(f"[{name}] Failed to send keystroke: {e}")

            # Broadcast permission_resolved to all clients (Task 3.7)
            await self._broadcast(session, {
                "type": "permission_resolved",
                "decision": decision,
            })

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Permission respond failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _announce_permission_request(self, session_name: str, tool_name: str, tool_input: dict):
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

        await self._say_to_room(session_name, text)

    async def api_recreate_session(self, request: web.Request) -> web.Response:
        """POST /api/session/{name}/recreate - Destroy session/worktree and create fresh one via CLI.

        Inherits session type from existing session config.
        Supported types: claude-bypass | claude-prompted | claude-restricted | claude-auto | bare
        """
        name = request.match_info["name"]
        try:
            logger.info(f"[{name}] Recreating session...")

            # Get old config for inheriting settings (before CLI deletes it)
            old_config = self._get_session_config(name)

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
                yaml_config = self._read_agentwire_yaml(session_path, machine_id) or {}
                yaml_config["voice"] = old_config.voice
                self._write_agentwire_yaml(session_path, yaml_config, machine_id)

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
            old_config = self._get_session_config(name)

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
                yaml_config = self._read_agentwire_yaml(session_path, machine_id) or {}
                yaml_config["voice"] = old_config.voice
                self._write_agentwire_yaml(session_path, yaml_config, machine_id)

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
            session_config = self._get_session_config(name)

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
                yaml_config = self._read_agentwire_yaml(session_path, machine_id) or {}
                yaml_config["voice"] = session_config.voice
                self._write_agentwire_yaml(session_path, yaml_config, machine_id)

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
        session = self.active_sessions.get(name)
        if not session:
            session = Session(name=name, config=self._get_session_config(name))
            self.active_sessions[name] = session

        await self._broadcast(session, data)
        return web.json_response({"success": True})

    async def api_restart_service(self, request: web.Request) -> web.Response:
        """POST /api/session/{name}/restart-service - Restart a system service.

        For system sessions (portal, tts, main), this properly restarts the service.
        Session names are configurable via services.*.session_name in config.
        """
        import subprocess

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
                    subprocess.run(
                        ["tmux", "kill-session", "-t", portal_session],
                        capture_output=True
                    )
                    await asyncio.sleep(0.5)
                    # Create new tmux session with portal serve command
                    subprocess.run(
                        ["tmux", "new-session", "-d", "-s", portal_session],
                        capture_output=True
                    )
                    subprocess.run(
                        ["tmux", "send-keys", "-t", portal_session,
                         "agentwire portal serve", "Enter"],
                        capture_output=True
                    )

                asyncio.create_task(delayed_restart())
                return web.json_response({
                    "success": True,
                    "message": "Portal restarting in 1 second..."
                })

            elif base_name == tts_session:
                # Restart TTS server
                subprocess.run(
                    ["agentwire", "tts", "stop"],
                    capture_output=True, text=True
                )
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

    async def api_history_list(self, request: web.Request) -> web.Response:
        """GET /api/history - List session history.

        Query params:
            project: Project path (required)
            machine: Machine ID (default "local")
            limit: Max number of entries (default 20)

        Response:
            {history: [{sessionId, firstMessage, lastSummary, timestamp, messageCount}, ...]}
        """
        try:
            project = request.query.get("project")
            if not project:
                return web.json_response(
                    {"error": "project parameter is required"},
                    status=400
                )

            machine = request.query.get("machine", "local")
            limit = request.query.get("limit", "20")

            args = [
                "history", "list",
                "--project", project,
                "--machine", machine,
                "--limit", str(limit)
            ]

            success, result = await self.run_agentwire_cmd(args)
            if not success:
                error_msg = result.get("error", "Failed to list history") if isinstance(result, dict) else "Failed to list history"
                return web.json_response({"error": error_msg}, status=500)

            # CLI returns list directly, wrap it
            history = result if isinstance(result, list) else result.get("history", [])
            return web.json_response({"history": history})

        except Exception as e:
            logger.error(f"History list API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_history_detail(self, request: web.Request) -> web.Response:
        """GET /api/history/{session_id} - Get session history details.

        URL params:
            session_id: The session ID to get details for

        Query params:
            machine: Machine ID (default "local")

        Response:
            {sessionId, summaries: [], firstMessage, timestamps: {start, end}, gitBranch, messageCount}
        """
        try:
            session_id = request.match_info["session_id"]
            machine = request.query.get("machine", "local")

            args = [
                "history", "show",
                session_id,
                "--machine", machine
            ]

            success, result = await self.run_agentwire_cmd(args)
            if not success:
                error_msg = result.get("error", "Failed to get history detail") if isinstance(result, dict) else "Failed to get history detail"
                return web.json_response({"error": error_msg}, status=500)

            return web.json_response(result)

        except Exception as e:
            logger.error(f"History detail API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_history_resume(self, request: web.Request) -> web.Response:
        """POST /api/history/{session_id}/resume - Resume a session from history.

        URL params:
            session_id: The session ID to resume

        Request body:
            name: Optional custom session name
            projectPath: Project path (required)
            machine: Machine ID (required)

        Response:
            {session: "<new-tmux-session-name>"}
        """
        try:
            session_id = request.match_info["session_id"]
            data = await request.json()

            project_path = data.get("projectPath")
            if not project_path:
                return web.json_response(
                    {"error": "projectPath is required"},
                    status=400
                )

            machine = data.get("machine", "local")
            name = data.get("name")

            args = [
                "history", "resume",
                session_id,
                "--project", project_path,
                "--machine", machine
            ]
            if name:
                args.extend(["--name", name])

            success, result = await self.run_agentwire_cmd(args)
            if not success:
                error_msg = result.get("error", "Failed to resume session") if isinstance(result, dict) else "Failed to resume session"
                return web.json_response({"error": error_msg}, status=500)

            session_name = result.get("session") if isinstance(result, dict) else None
            return web.json_response({"session": session_name})

        except Exception as e:
            logger.error(f"History resume API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_live(self, request: web.Request) -> web.Response:
        """GET /api/scheduler/live - Live scheduler state.

        Checks if the scheduler tmux session is actually running.
        Returns 404 with running=false if the daemon isn't active,
        even if a stale state file exists.
        """
        try:
            # Check if scheduler tmux session is alive
            is_running = await self._is_scheduler_running()
            if not is_running:
                return web.json_response({"running": False}, status=404)

            from .scheduler import read_live_state
            state = read_live_state()
            if state is None:
                return web.json_response({"running": False}, status=404)
            state["running"] = True
            return web.json_response(state)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _is_scheduler_running(self) -> bool:
        """Check if the agentwire-scheduler tmux session exists."""
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", "=agentwire-scheduler",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    async def api_scheduler_events(self, request: web.Request) -> web.Response:
        """GET /api/scheduler/events - Recent scheduler events."""
        try:
            from .scheduler import read_events
            tail = int(request.query.get("tail", "20"))
            task_filter = request.query.get("task") or None
            events = read_events(tail=tail, task_filter=task_filter)
            return web.json_response({"events": events})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_board(self, request: web.Request) -> web.Response:
        """GET /api/scheduler/board - Scheduler board data."""
        try:
            from .scheduler import get_board_display, load_board
            board = load_board()
            rows = get_board_display(board)
            return web.json_response({"tasks": rows})
        except (FileNotFoundError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=404)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_task_enable(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/tasks/{name}/enable - Enable a task."""
        name = request.match_info["name"]
        try:
            success, result = await self.run_agentwire_cmd(["scheduler", "enable", name])
            if success:
                return web.json_response({"success": True, "task": name})
            return web.json_response({"error": result.get("error", "Enable failed")}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_task_disable(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/tasks/{name}/disable - Disable a task."""
        name = request.match_info["name"]
        try:
            success, result = await self.run_agentwire_cmd(["scheduler", "disable", name])
            if success:
                return web.json_response({"success": True, "task": name})
            return web.json_response({"error": result.get("error", "Disable failed")}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_task_run(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/tasks/{name}/run - Force-run a task (fire-and-forget)."""
        name = request.match_info["name"]
        try:
            # Fire-and-forget: start the task in background, completion comes via WebSocket
            asyncio.create_task(self.run_agentwire_cmd(["scheduler", "run", name]))
            return web.json_response({"success": True, "task": name, "status": "started"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_start(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/start - Start the scheduler daemon in tmux."""
        try:
            if await self._is_scheduler_running():
                return web.json_response({"success": True, "status": "already_running"})
            # Create tmux session and launch scheduler serve (same as CLI but detached)
            proc = await asyncio.create_subprocess_exec(
                "tmux", "new-session", "-d", "-s", "agentwire-scheduler",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            proc2 = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", "agentwire-scheduler",
                "agentwire scheduler serve", "Enter",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc2.wait()
            return web.json_response({"success": True, "status": "started"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_stop(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/stop - Stop the scheduler daemon."""
        try:
            if not await self._is_scheduler_running():
                return web.json_response({"success": True, "status": "already_stopped"})
            success, result = await self.run_agentwire_cmd(["scheduler", "stop"], json_output=False)
            if success:
                return web.json_response({"success": True, "status": "stopped"})
            return web.json_response({"error": result.get("error", "Unknown error")}, status=500)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_task_events(self, request: web.Request) -> web.Response:
        """GET /api/scheduler/tasks/{name}/events - Events for a specific task."""
        name = request.match_info["name"]
        try:
            from .scheduler import read_events
            tail = int(request.query.get("tail", "100"))
            events = read_events(tail=tail, task_filter=name)
            return web.json_response({"events": events})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_workflows_runs_list(self, request: web.Request) -> web.Response:
        """GET /api/workflows/runs?workflow=X&limit=N - Recent workflow runs.

        Reads ~/.agentwire/workflows/runs/*/metadata.json. Sorted newest first.
        Optional `workflow` filters to a specific workflow name.
        """
        try:
            from .workflows.cli import RUNS_DIR
            from .workflows.storage import list_runs
            workflow_name = request.query.get("workflow")
            try:
                limit = int(request.query.get("limit", "50"))
            except ValueError:
                limit = 50
            loop = asyncio.get_event_loop()
            runs = await loop.run_in_executor(
                None, lambda: list_runs(RUNS_DIR, workflow=workflow_name, limit=limit)
            )
            # Slim each run to only the fields the sidebar needs — full metadata
            # lives in the detail endpoint. Keeps the list response small.
            # Totals aren't stored at run level; aggregate from per-node tokens.
            slim = []
            for r in runs:
                nodes = r.get("nodes", []) or []
                tokens_in = sum((n.get("tokens") or {}).get("input", 0) for n in nodes)
                tokens_out = sum((n.get("tokens") or {}).get("output", 0) for n in nodes)
                cost = sum((n.get("tokens") or {}).get("cost", 0.0) for n in nodes)
                slim.append({
                    "run_id": r.get("run_id"),
                    "workflow": r.get("workflow"),
                    "status": r.get("status"),
                    "runner": r.get("runner", ""),
                    "started_at": r.get("started_at"),
                    "duration_ms": r.get("duration_ms"),
                    "total_cost": cost,
                    "total_tokens_in": tokens_in,
                    "total_tokens_out": tokens_out,
                    "node_count": len(nodes),
                })
            return web.json_response({"runs": slim})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_workflows_run_detail(self, request: web.Request) -> web.Response:
        """GET /api/workflows/runs/{run_id} - Full run detail with normalized node schema.

        Merges metadata.json's per-node records with event-stream-derived tool
        call summaries, returning a single flat `nodes: [...]` list. The raw
        `metadata` dict is also returned (minus the nodes array) for run-level
        fields. Full event streams aren't included — fetch those separately
        from the JSONL files on disk if needed.
        """
        run_id = request.match_info["run_id"]
        try:
            from .workflows.cli import RUNS_DIR
            from .workflows.storage import load_run, load_context, load_events

            loop = asyncio.get_event_loop()
            meta = await loop.run_in_executor(None, lambda: load_run(RUNS_DIR, run_id))
            if meta is None:
                return web.json_response({"error": f"run '{run_id}' not found"}, status=404)

            context = await loop.run_in_executor(None, lambda: load_context(RUNS_DIR, run_id))

            # Walk event streams once to build per-node summaries: tool calls and
            # final assistant text (last non-empty text block).
            def _event_summaries() -> dict[str, dict]:
                all_events = load_events(RUNS_DIR, run_id)
                per_node: dict[str, dict] = {}
                for node_id, ev in all_events:
                    bucket = per_node.setdefault(
                        node_id, {"event_count": 0, "tool_calls": [], "final_text": ""}
                    )
                    bucket["event_count"] += 1
                    msg = ev.get("message") if isinstance(ev, dict) else None
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("role") != "assistant":
                        continue
                    for block in msg.get("content", []) or []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            bucket["tool_calls"].append({
                                "name": block.get("name", ""),
                                "input_preview": str(block.get("input", ""))[:200],
                            })
                        elif block.get("type") == "text":
                            text = (block.get("text") or "").strip()
                            if text:
                                bucket["final_text"] = text  # last non-empty wins
                return per_node

            summaries = await loop.run_in_executor(None, _event_summaries)

            # Merge metadata node records with event summaries into a single flat
            # shape for the frontend. Metadata uses `id` + nested `tokens.{input,output,cost}`;
            # we flatten to `node_id` + `tokens_in` / `tokens_out` / `cost` for a cleaner API.
            nodes_merged: list[dict] = []
            for n in meta.get("nodes", []) or []:
                node_id = n.get("id", "")
                tokens = n.get("tokens") or {}
                summary = summaries.get(node_id, {})
                nodes_merged.append({
                    "node_id": node_id,
                    "status": n.get("status"),
                    "runner": n.get("runner", ""),
                    "duration_ms": n.get("duration_ms", 0),
                    "attempts": n.get("attempts", 1),
                    "tokens_in": tokens.get("input", 0),
                    "tokens_out": tokens.get("output", 0),
                    "cost": tokens.get("cost", 0.0),
                    "error": n.get("error"),
                    "event_count": summary.get("event_count", 0),
                    "tool_calls": summary.get("tool_calls", []),
                    "final_text": summary.get("final_text", ""),
                })

            # Run-level metadata without the nodes array (it's in `nodes` above)
            meta_summary = {k: v for k, v in meta.items() if k != "nodes"}

            return web.json_response({
                "metadata": meta_summary,
                "context": context or {},
                "nodes": nodes_merged,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_scheduler_session_output(self, request: web.Request) -> web.Response:
        """GET /api/scheduler/output?session=X&lines=30 - Get recent session output."""
        session = request.query.get("session")
        if not session:
            return web.json_response({"error": "session parameter required"}, status=400)
        lines = min(int(request.query.get("lines", "30")), 100)
        try:
            loop = asyncio.get_event_loop()
            output = await loop.run_in_executor(
                None, lambda: self.agent.get_output(session, lines=lines)
            )
            return web.json_response({"session": session, "output": output})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

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

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Notify API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def speak(self, session_name: str, text: str) -> bool:
        """Generate TTS audio and send to session clients.

        Audio is broadcast only to clients connected to this specific session
        (terminal, monitor, or chat windows viewing that session).

        Returns:
            True if audio was sent to clients, False if no clients connected.
        """
        # Get or create session
        if session_name not in self.active_sessions:
            self.active_sessions[session_name] = Session(
                name=session_name, config=self._get_session_config(session_name)
            )

        session = self.active_sessions[session_name]

        # Check if any clients are connected to this session
        if not session.clients:
            logger.warning(f"[{session_name}] speak: no session clients connected")
            return False

        logger.info(f"[{session_name}] speak: {len(session.clients)} session client(s)")

        # Get voice settings (resolve "random" once per session)
        voice = session.config.voice or self.config.tts.default_voice
        if voice.lower() == "random":
            voice = await self._resolve_voice(voice)
            session.config.voice = voice  # Cache for this session
            logger.info(f"[{session_name}] Resolved random voice to: {voice}")
        exaggeration = session.config.exaggeration
        cfg_weight = session.config.cfg_weight

        # Notify clients TTS is starting (session clients + dashboard)
        tts_start_msg = {"type": "tts_start", "session": session_name, "text": text}
        await self._broadcast(session, tts_start_msg)
        await self.broadcast_dashboard("tts_start", {"session": session_name, "text": text})

        try:
            # Split long text into sentence-sized chunks for better TTS quality
            from .utils.chunker import chunk_text
            chunks = chunk_text(text)

            any_sent = False
            for chunk in chunks:
                logger.info(f"[{session_name}] TTS voice: {voice}")
                audio_data = await self._tts_generate(
                    text=chunk,
                    voice=voice,
                    exaggeration=exaggeration,
                    cfg_weight=cfg_weight,
                )

                if audio_data:
                    audio_data = self._prepend_silence(audio_data, ms=300)
                    audio_b64 = base64.b64encode(audio_data).decode()
                    logger.info(f"[{session_name}] Broadcasting audio chunk ({len(audio_b64)} bytes b64)")

                    await self._broadcast(session, {"type": "audio", "session": session_name, "data": audio_b64})
                    await self.broadcast_dashboard("audio_playing", {"session": session_name})

                    # Estimate audio duration and schedule audio_done notification
                    # WAV header: 44 bytes, then 16-bit stereo 24kHz = 96000 bytes/sec
                    audio_bytes = len(audio_data)
                    duration_sec = max(0.5, (audio_bytes - 44) / 96000)
                    asyncio.create_task(self._send_audio_done_delayed(session_name, duration_sec))
                    any_sent = True
                else:
                    logger.warning(f"[{session_name}] TTS returned no audio data for chunk")

            return any_sent

        except Exception as e:
            logger.error(f"TTS failed for {session_name}: {e}")
            return False

    async def _send_audio_done_delayed(self, session_name: str, delay_sec: float) -> None:
        """Send audio_done to dashboard after estimated playback duration."""
        await asyncio.sleep(delay_sec)
        await self.broadcast_dashboard("audio_done", {"session": session_name})


async def run_server(config: Config):
    """Run the AgentWire server."""
    server = AgentWireServer(config)
    await server.init_backends()

    # Cleanup old uploads on startup
    await server.cleanup_old_uploads()

    # Start session monitor for all-sessions dashboard indicators
    monitor_task = asyncio.create_task(server.monitor_all_sessions())

    # Start idle nag loop (TTS reminders for idle sessions with open windows)
    idle_nag_task = asyncio.create_task(server.idle_nag_loop())

    # Sessions are now fetched dynamically from tmux + .agentwire.yml
    # No cache to rebuild or periodically refresh

    # Setup SSL if configured
    ssl_context = None
    if config.server.ssl.enabled:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(
            config.server.ssl.cert, config.server.ssl.key
        )

    runner = web.AppRunner(server.app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        config.server.host,
        config.server.port,
        ssl_context=ssl_context,
    )

    protocol = "https" if ssl_context else "http"
    logger.info(f"Starting AgentWire server at {protocol}://{config.server.host}:{config.server.port}")

    try:
        await site.start()
        # Keep running
        while True:
            await asyncio.sleep(3600)
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        await server.close_backends()
        await runner.cleanup()


def main(config_path: str | None = None, **overrides) -> None:
    """Entry point for running the server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(config_path)

    # Apply CLI overrides
    if overrides.get("port"):
        config.server.port = overrides["port"]
    if overrides.get("host"):
        config.server.host = overrides["host"]
    if overrides.get("no_tts"):
        config.tts.backend = "none"
    if overrides.get("no_stt"):
        config.stt.url = None

    try:
        asyncio.run(run_server(config))
    except KeyboardInterrupt:
        logger.info("Server stopped")


if __name__ == "__main__":
    main()
