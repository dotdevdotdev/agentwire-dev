"""
AgentWire WebSocket server.

Multi-session voice web interface for AI coding agents.
"""

import asyncio
import base64
import fcntl
import gzip
import json
import logging
import mimetypes
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
from datetime import datetime
from pathlib import Path
from typing import Dict

import aiohttp
import aiohttp_jinja2
import jinja2
import yaml
from aiohttp import web

from . import prompt_router, security
from .cached_status import CachedStatusChecker
from .config import Config, load_config
from .routes.scratchpad import ScratchpadRoutesMixin, register_scratchpad_routes
from .security import (
    WS_PROTOCOL_PREFIX,
    create_security_middleware,
    ensure_auth_token,
    frozen_config_violations,
    is_loopback_host,
    resolve_auth_token,
    restore_redactions,
    validate_startup_security,
)
from .ssh import ssh_base_opts
from .worktree import parse_session_name

__version__ = "1.3.0"

logger = logging.getLogger(__name__)

# Static asset serving (#488): gzip text on the fly + Cache-Control headers.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")
STATIC_ROOT = (Path(__file__).parent / "static").resolve()
# Extensions worth gzipping (text compresses ~3-5x); images/fonts don't.
COMPRESSIBLE_SUFFIXES = {
    ".js", ".mjs", ".css", ".json", ".svg", ".map", ".txt",
    ".html", ".xml", ".webmanifest", ".ico",
}
# Long cache for content-stable binaries (icons/images), short for code/text
# since filenames aren't content-hashed yet (a follow-up could append_version).
IMAGE_SUFFIXES = {".webp", ".png", ".jpeg", ".jpg", ".gif", ".woff", ".woff2"}
STATIC_CACHE_IMAGE = "public, max-age=604800"  # 7 days
STATIC_CACHE_CODE = "public, max-age=3600"     # 1 hour

# Paste chunking: large inputs (pastes) are written in chunks with delays
# to avoid flooding the PTY buffer and freezing the agent session.
PASTE_THRESHOLD = 64    # bytes — above this, chunk the write
PASTE_CHUNK_SIZE = 128  # bytes per write
PASTE_CHUNK_DELAY = 0.01  # seconds between chunks


async def unpin_tmux_window(session_name: str, ssh_target: str | None = None) -> None:
    """Clear tmux manual size mode so the window-size policy governs again.

    Portal builds before v1.34 pinned windows to manual mode on every
    browser resize (#258); this heals any window still stuck there.
    Unsetting the window-level option restores the configured policy and
    itself triggers a re-fit. (An explicit -A resize would not: it resizes
    once but leaves manual mode set.)
    """
    cmd = ["tmux", "set-option", "-w", "-t", session_name, "-u", "window-size"]
    if ssh_target:
        proc = await asyncio.create_subprocess_exec(
            "ssh", *ssh_base_opts(), ssh_target, shlex.join(cmd),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    await proc.wait()


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


def should_nag_idle_session(
    name: str,
    last_output_timestamp: float,
    nagged_output_ts: dict[str, float],
) -> bool:
    """Edge-trigger decision for the idle-nag loop (pure, unit-testable).

    A session is included in the nag batch only when it has never been
    nagged this episode, or when its ``last_output_timestamp`` has advanced
    since the last nag (a genuinely new question/error/activity). A session
    that stays continuously idle keeps a *fixed* ``last_output_timestamp``,
    so it nags exactly once per idle episode instead of every scan.

    ``nagged_output_ts`` maps session name -> the ``last_output_timestamp``
    captured at its last nag. The caller is responsible for recording the
    timestamp on include and for popping the entry when the session drops
    below the idle threshold (resetting the episode).
    """
    prior = nagged_output_ts.get(name)
    if prior is None:
        return True
    return last_output_timestamp > prior


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
    pane_index: int = 0  # Pane the dialog is on (worker panes > 0)


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


class AgentWireServer(ScratchpadRoutesMixin):
    """Main server managing sessions, WebSockets, and agent backends."""

    def __init__(self, config: Config):
        self.config = config
        self.active_sessions: dict[str, Session] = {}  # Active sessions with connected clients
        self.session_activity: dict[str, dict] = {}  # Global activity tracking for all sessions
        self.dashboard_clients: set = set()  # WebSocket clients for dashboard updates
        self.session_client_counts: dict[str, int] = {}  # Attached tmux client counts per session
        self.active_notifications: dict[str, dict] = {}  # id -> notification for persistence across refresh
        self._background_tasks: set[asyncio.Task] = set()  # strong refs so create_task work isn't GC'd
        self._gzip_cache: dict[Path, tuple[float, bytes]] = {}  # static gzip cache: path -> (mtime, bytes)
        # Rate-limit state for the public, unauthenticated POST /api/pair (#423 S1).
        # Per-IP and global sliding-window attempt logs (monotonic timestamps).
        self._pair_attempts: dict[str, list[float]] = {}
        self._pair_attempts_global: list[float] = []
        self.machine_status_checker = CachedStatusChecker(ttl_seconds=30)  # Progressive loading for machines
        self.remote_sessions_checker = CachedStatusChecker(ttl_seconds=20)  # Progressive loading for remote sessions
        self.projects_checker = CachedStatusChecker(ttl_seconds=30)  # Progressive loading for projects
        self.stt = None
        self.agent = None
        self._http_session: aiohttp.ClientSession | None = None  # For TTS HTTP calls
        # Default-tier Kokoro TTS and Moonshine STT both run in standalone shim
        # subprocesses (process isolation — see ensure_managed_tts /
        # ensure_managed_stt), not in-process: the GIL-holding warm-up would
        # wedge this event loop (#382/#398). No object to construct here;
        # run_server ensures the shims post-bind and the portal talks HTTP.
        # Origin + token enforcement. The middleware is a pure function of
        # config — run_server() resolves the token file into
        # config.server.auth_token before constructing the server.
        self.app = web.Application(
            middlewares=[
                create_security_middleware(
                    config.server.auth_token,
                    config.server.allowed_origins,
                    on_lockout=self._on_auth_lockout,
                )
            ]
        )
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
        # PWA: manifest + root-scoped service worker (#483). Served from root so
        # the SW controls scope "/"; public so the browser can install the app.
        self.app.router.add_get("/manifest.webmanifest", self.handle_manifest)
        self.app.router.add_get("/service-worker.js", self.handle_service_worker)
        self.app.router.add_get("/api/push/config", self.api_push_config)
        self.app.router.add_post("/api/push/subscribe", self.api_push_subscribe)
        self.app.router.add_post("/api/push/unsubscribe", self.api_push_unsubscribe)
        self.app.router.add_get("/mobile", self.handle_mobile)
        self.app.router.add_get("/pair", self.handle_pair_page)
        self.app.router.add_post("/api/pair", self.api_pair)
        self.app.router.add_get("/ws", self.handle_dashboard_ws)
        self.app.router.add_get("/ws/{name:.+}", self.handle_websocket)
        self.app.router.add_get("/ws/terminal/{name:.+}", self.handle_terminal_ws)
        self.app.router.add_get("/api/sessions", self.api_sessions)
        self.app.router.add_get("/api/sessions/local", self.api_sessions_local)
        self.app.router.add_get("/api/sessions/remote", self.api_sessions_remote)
        self.app.router.add_get("/api/worktrees", self.api_worktrees)
        self.app.router.add_get("/api/projects", self.api_projects)
        self.app.router.add_post("/api/projects/create", self.api_projects_create)
        self.app.router.add_post("/api/projects/delete", self.api_projects_delete)
        self.app.router.add_get("/api/roles", self.api_roles)
        self.app.router.add_get("/api/session/defaults", self.api_session_defaults)
        self.app.router.add_get("/api/machine/{machine_id}/status", self.api_machine_status)
        self.app.router.add_get("/api/check-path", self.api_check_path)
        self.app.router.add_get("/api/check-branches", self.api_check_branches)
        self.app.router.add_post("/api/create", self.api_create_session)
        self.app.router.add_post("/api/active-session", self.api_active_session)
        self.app.router.add_post("/api/session/{name:.+}/config", self.api_session_config)
        self.app.router.add_post("/transcribe", self.handle_transcribe)
        self.app.router.add_post("/upload", self.handle_upload)
        self.app.router.add_post("/send/{name:.+}", self.handle_send)
        self.app.router.add_post("/api/say/{name:.+}", self.api_say)
        self.app.router.add_get("/api/sessions/{name:.+}/connections", self.api_session_connections)
        self.app.router.add_post("/api/local-tts/{name:.+}", self.api_local_tts)
        self.app.router.add_post("/api/answer/{name:.+}", self.api_answer)
        # Mobile Review window: structured diff + tap-to-approve/deny
        self.app.router.add_get("/api/review/{name:.+}", self.api_review)
        self.app.router.add_post("/api/review/{name:.+}/answer", self.api_review_answer)
        self.app.router.add_post("/api/session/{name:.+}/recreate", self.api_recreate_session)
        self.app.router.add_post("/api/session/{name:.+}/spawn-sibling", self.api_spawn_sibling)
        self.app.router.add_post("/api/session/{name:.+}/fork", self.api_fork_session)
        self.app.router.add_post("/api/session/{name:.+}/restart-service", self.api_restart_service)
        self.app.router.add_post("/api/session/{name:.+}/broadcast", self.api_session_broadcast)
        self.app.router.add_get("/api/voices", self.api_voices)
        self.app.router.add_get("/api/voice-status", self.api_voice_status)
        self.app.router.add_delete("/api/sessions/{name:.+}", self.api_close_session)
        self.app.router.add_get("/api/machines", self.api_machines)
        self.app.router.add_post("/api/machines", self.api_add_machine)
        self.app.router.add_delete("/api/machines/{machine_id}", self.api_remove_machine)
        self.app.router.add_get("/api/config", self.api_get_config)
        self.app.router.add_post("/api/config", self.api_save_config)
        self.app.router.add_post("/api/config/reload", self.api_reload_config)
        self.app.router.add_get("/api/safety/status", self.api_safety_status)
        self.app.router.add_get("/api/safety/logs", self.api_safety_logs)
        self.app.router.add_get("/api/safety/rules", self.api_safety_rules)
        self.app.router.add_post("/api/safety/config", self.api_safety_config_post)
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
        self.app.router.add_post("/api/desktop/collage", self.api_desktop_collage)
        self.app.router.add_post("/api/desktop/layout", self.api_desktop_layout)
        # Desktop notifications
        self.app.router.add_post("/api/desktop/notification", self.api_desktop_notification)
        self.app.router.add_post("/api/desktop/notification/dismiss", self.api_desktop_notification_dismiss)
        self.app.router.add_get("/api/desktop/notifications", self.api_desktop_notifications_list)
        # Services registry (custom service sessions from config)
        self.app.router.add_get("/api/services/custom", self.api_services_custom)

        # Scratch pad (shared notes drawer)
        register_scratchpad_routes(self, self.app)
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
        # Council seating board: live sittings + per-prompt snapshot
        self.app.router.add_get("/api/council/sittings", self.api_council_sittings)
        self.app.router.add_get("/api/council/archive", self.api_council_archive)
        self.app.router.add_get("/api/council/live", self.api_council_live)
        self.app.router.add_get("/api/council/status", self.api_council_status)
        self.app.router.add_post("/api/council/start", self.api_council_start)
        self.app.router.add_post("/api/council/stop", self.api_council_stop)
        self.app.router.add_post("/api/council/ask", self.api_council_ask)
        # Artifact windows: upload and serve agent-generated HTML
        self.app.router.add_post("/api/artifacts/upload", self.api_artifacts_upload)
        self.app.router.add_get("/api/artifacts", self.api_artifacts_list)
        self.app.router.add_delete("/api/artifacts/{filename:.+}", self.api_artifacts_delete)
        artifacts_dir = self.config.artifacts.dir
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.app.router.add_static("/artifacts", artifacts_dir)
        # Custom static handler: gzip text assets on the fly + Cache-Control so
        # phone first-load and repeat loads aren't crippled by the static
        # payload (aiohttp's add_static does neither). See _handle_static.
        self.app.router.add_get("/static/{path:.+}", self._handle_static)

    async def init_backends(self):
        """Initialize TTS, STT, and agent backends."""
        # Convert config to dict for backend factories
        config_dict = {
            "tts": {
                "backend": self.config.tts.backend,
                "url": self.config.tts.url,
                "exaggeration": self.config.tts.exaggeration,
                "cfg_weight": self.config.tts.cfg_weight,
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

        self.stt = get_stt_backend(self.config)
        self.agent = get_agent_backend(config_dict)

        # Create HTTP session for TTS server calls
        self._http_session = aiohttp.ClientSession()

        logger.info(f"TTS URL: {self.config.tts.url}")
        logger.info(f"STT backend: {type(self.stt).__name__}")

    async def close_backends(self):
        """Clean up backend resources."""
        if self._http_session:
            await self._http_session.close()
        # The default-tier TTS (agentwire-kokoro) and STT (agentwire-stt) shims
        # run in their own tmux sessions — leave them running on portal
        # shutdown; the user may own them. `agentwire kokoro stop` /
        # `agentwire stt stop` are the off switches.

    async def ensure_managed_stt(self) -> None:
        """Ensure the default-tier Moonshine shim subprocess is running.

        Delegates to the CLI (single source of truth): ``agentwire stt start``
        is idempotent — ``cmd_stt_start`` early-returns if the ``agentwire-stt``
        tmux session already exists, so a user-started shim is reused with no
        port clash. The ~19s ONNX warm-up happens in that child process, never
        on the portal's event loop. Mirrors ``autostart_custom_services``."""
        await asyncio.sleep(5)  # let the portal finish binding first
        try:
            success, result = await self.run_agentwire_cmd(["stt", "start"], json_output=False)
            if success:
                await self._post_toast(
                    "Starting host STT (Moonshine) — browser speech until ready",
                    session="moonshine-stt", priority="normal", id_prefix="moonshine")
                logger.info("[STT] Ensured managed Moonshine shim")
            else:
                logger.warning("[STT] Managed shim start failed: %s", result.get("error"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[STT] Managed shim start error: %s", e)
        finally:
            # Flip voice-status off the 30s TTL so the next poll re-probes the
            # shim's /health and surfaces server_transcribe promptly.
            self._voice_status_cache = None

    async def ensure_managed_tts(self) -> None:
        """Ensure the default-tier Kokoro TTS shim subprocess is running.

        Delegates to the CLI (single source of truth): ``agentwire kokoro start``
        is idempotent — ``cmd_kokoro_start`` early-returns if the
        ``agentwire-kokoro`` tmux session already exists, so a user-started shim
        is reused with no port clash. The ~200 MB download + ONNX warm-up
        happens in that child process, never on the portal's event loop. Mirrors
        ``ensure_managed_stt``."""
        await asyncio.sleep(5)  # let the portal finish binding first
        try:
            success, result = await self.run_agentwire_cmd(["kokoro", "start"], json_output=False)
            if success:
                await self._post_toast(
                    "Starting host voice (Kokoro) — browser speech until ready",
                    session="kokoro-voice", priority="normal", id_prefix="kokoro")
                logger.info("[TTS] Ensured managed Kokoro shim")
            else:
                logger.warning("[TTS] Managed shim start failed: %s", result.get("error"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[TTS] Managed shim start error: %s", e)
        finally:
            # Flip voice-status off the 30s TTL so the next poll re-probes the
            # shim's /health and surfaces the ready voice promptly.
            self._voice_status_cache = None

    def _tts_envelope_options(self, exaggeration: float, cfg_weight: float) -> dict:
        """Session knobs + config pass-through, merged into the shim `options`."""
        return {
            "exaggeration": exaggeration,
            "cfg_weight": cfg_weight,
            **self.config.tts.options,
        }

    def _tts_base_url(self) -> str | None:
        """Resolve the shim URL for the active TTS tier.

        ``default`` → the portal-managed Kokoro shim (``tts.url`` override or
        :8102); ``custom`` → the user/remote-managed shim at ``tts.url``. Both
        speak the same HTTP contract, so synthesis goes through one path."""
        from .tts import _default_tts_url

        if self.config.tts.backend == "default":
            return _default_tts_url(self.config.tts)
        return self.config.tts.url

    async def _kokoro_shim_ready(self) -> bool:
        """True if the default-tier Kokoro shim's /health reports ``ok``.

        The default tier probes the managed shim before synthesizing; while it
        downloads/loads (or hasn't spawned) the caller falls back to browser
        speechSynthesis / OS voice — never blocking on a not-ready engine."""
        from .tts import _default_tts_url

        health = await self._probe_shim(_default_tts_url(self.config.tts), "/health")
        return bool(health and health.get("status") == "ok")

    async def _tts_generate(
        self,
        text: str,
        voice: str | None,
        instructions: str | None = None,
        options: dict | None = None,
    ) -> bytes | None:
        """Generate TTS audio via the active-tier shim (contract envelope).

        Core fields: text (+ optional voice). `instructions` and `options`
        pass through verbatim — only the shim interprets them. The base URL
        resolves per tier (default → managed Kokoro shim, custom → `tts.url`).
        """
        if not self._http_session:
            return None

        base_url = self._tts_base_url()
        if not base_url:
            return None

        payload: dict = {"text": text}
        if voice:
            payload["voice"] = voice
        if instructions:
            payload["instructions"] = instructions
        if options:
            payload["options"] = options

        try:
            async with self._http_session.post(
                f"{base_url}/tts",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.config.tts.timeout),
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
                timeout=aiohttp.ClientTimeout(total=2),
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
                    ["ssh", *ssh_base_opts(), "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", ssh_target, cmd],
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
                    ["ssh", *ssh_base_opts(), "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", ssh_target, f"cat {shlex.quote(yaml_path)}"],
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

        local_hostname = socket.gethostname().split('.')[0]

        is_local = machine_id is None or machine_id == "local" or machine_id == local_hostname

        if is_local:
            yaml_path = Path(cwd) / ".agentwire.yml"
            try:
                with open(yaml_path, "w") as f:
                    yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
                from .project_config import ensure_gitignored
                ensure_gitignored(Path(cwd))
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
                    ["ssh", *ssh_base_opts(), "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", ssh_target, cmd],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return result.returncode == 0
            except (subprocess.TimeoutExpired, Exception) as e:
                logger.warning(f"Failed to write remote yaml: {e}")
                return False

    async def _get_voices(self) -> list[str]:
        """Available TTS voices: Kokoro presets on the default tier (once
        the engine is ready), the shim's list on the custom tier.

        Custom-tier results are cached for 30s so an unreachable shim
        can't stall every page load (handle_index awaits this)."""
        if self.config.tts.backend == "custom":
            now = time.time()
            cached = getattr(self, "_voices_cache", None)
            if cached and now - cached[0] < 30:
                return cached[1]
            voices = await self._tts_get_voices()
            self._voices_cache = (now, voices)
            return voices
        if self.config.tts.backend == "default" and await self._kokoro_shim_ready():
            from .tts.engines.kokoro import PRESET_VOICES

            return list(PRESET_VOICES)
        return []

    async def _probe_shim(self, base_url: str, path: str, timeout: float = 1.5):
        """GET a shim endpoint, returning parsed JSON or None (fail-soft)."""
        if not self._http_session or not base_url:
            return None
        try:
            async with self._http_session.get(
                f"{base_url.rstrip('/')}{path}",
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return None

    async def api_voice_status(self, request: web.Request) -> web.Response:
        """GET /api/voice-status — voice tier + availability for the frontend.

        The portal uses this to pick its input/output paths (browser speech
        vs audio upload) and to render the instant-mode banner. Custom-shim
        probes are cached for 30s.
        """
        now = time.time()
        cached = getattr(self, "_voice_status_cache", None)
        if cached and now - cached[0] < 30:
            return web.json_response(cached[1])

        stt_cfg, tts_cfg = self.config.stt, self.config.tts

        stt: dict = {"backend": stt_cfg.backend, "url": stt_cfg.url, "available": True}
        # server_transcribe drives the frontend's browser-vs-upload choice: true
        # → MediaRecorder POST /transcribe, false → browser SpeechRecognition.
        stt["server_transcribe"] = stt_cfg.backend in ("cloud", "custom")
        if stt_cfg.backend == "custom":
            stt["available"] = await self._probe_shim(stt_cfg.url, "/health") is not None
        elif stt_cfg.backend == "default":
            # Portal-managed Moonshine shim subprocess. The client only uploads
            # once the shim's /health is "ok" (model loaded); while it loads or
            # if the spawn failed, server_transcribe stays false and the client
            # keeps using browser speech recognition. available stays true —
            # browser fallback is always there.
            from .stt import _default_stt_url

            health = await self._probe_shim(_default_stt_url(stt_cfg), "/health")
            stt["server_transcribe"] = bool(health and health.get("status") == "ok")

        tts: dict = {"backend": tts_cfg.backend, "url": tts_cfg.url, "available": True}
        if tts_cfg.backend == "default":
            # Portal-managed Kokoro shim subprocess. Probe its /health for the
            # warm-up state (mirrors the STT shim); the browser keeps
            # synthesizing speech until status is "ok", and `available` stays
            # true because that browser fallback is always there.
            from .tts import _default_tts_url

            health = await self._probe_shim(_default_tts_url(tts_cfg), "/health")
            state = health.get("status") if health else "absent"
            percent = health.get("percent", 0) if health else 0
            tts["kokoro"] = {"state": state, "percent": percent}
            if health and health.get("error"):
                tts["kokoro"]["error"] = health["error"]
            if state == "ok":
                from .tts.engines.kokoro import PRESET_VOICES

                tts["voices"] = list(PRESET_VOICES)
        elif tts_cfg.backend == "custom":
            health = await self._probe_shim(tts_cfg.url, "/health")
            tts["available"] = health is not None
            if tts["available"]:
                caps = await self._probe_shim(tts_cfg.url, "/capabilities")
                if caps:
                    if caps.get("tool_prompt"):
                        tts["tool_prompt"] = caps["tool_prompt"]
                    if caps.get("voices") is not None:
                        tts["voices"] = caps["voices"]

        status = {
            "stt": stt,
            "tts": tts,
            "corrections": stt_cfg.corrections,
            # Instant (zero-round-trip browser) mode only holds while STT stays
            # browser-side; once host Moonshine takes over, audio uploads.
            "instant_mode": not stt["server_transcribe"] and tts_cfg.backend == "default",
        }
        self._voice_status_cache = (now, status)
        return web.json_response(status)

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
            # Insert before a `--` separator if present — anything appended
            # after `--` would be swallowed into positional args (e.g. the
            # first-message text on `send`).
            if "--" in cmd:
                cmd.insert(cmd.index("--"), "--json")
            else:
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

    async def handle_manifest(self, request: web.Request) -> web.Response:
        """Serve the PWA web app manifest from root (#483)."""
        manifest_path = Path(__file__).parent / "static" / "manifest.webmanifest"
        return web.FileResponse(
            manifest_path,
            headers={"Content-Type": "application/manifest+json"},
        )

    async def handle_service_worker(self, request: web.Request) -> web.Response:
        """Serve the service worker from root so its scope is "/" (#483).

        ``Service-Worker-Allowed: /`` belt-and-suspenders the root scope, and we
        forbid caching so a redeploy of the SW is picked up promptly.
        """
        sw_path = Path(__file__).parent / "static" / "service-worker.js"
        return web.FileResponse(
            sw_path,
            headers={
                "Content-Type": "application/javascript",
                "Service-Worker-Allowed": "/",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    async def api_push_config(self, request: web.Request) -> web.Response:
        """GET /api/push/config — public-key + enabled flag for the push client (#483)."""
        from .channels.push import _get_push_config, push_ready

        cfg = _get_push_config()
        ready, _reason = push_ready()
        return web.json_response(
            {"enabled": bool(ready), "vapidPublicKey": cfg.vapid_public_key or ""}
        )

    async def api_push_subscribe(self, request: web.Request) -> web.Response:
        """POST /api/push/subscribe — persist a browser's Web Push subscription (#483)."""
        from . import push_store

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
        from . import push_store

        try:
            data = await request.json()
        except Exception:
            data = {}
        endpoint = (data.get("endpoint") or "").strip()
        if not endpoint:
            return web.json_response({"success": False, "error": "endpoint required"}, status=400)
        removed = push_store.remove(endpoint)
        return web.json_response({"success": True, "removed": removed})

    async def _fanout_push(self, text: str, session: str | None = None,
                           priority: str = "normal") -> None:
        """Best-effort Web Push fan-out for a toast (#483).

        Mirrors every portal toast to subscribed devices so a backgrounded/locked
        phone buzzes. A no-op when push is disabled/unconfigured; runs the
        blocking pywebpush calls off the event loop and never raises into the
        toast path.
        """
        try:
            from .channels.push import push_ready, send_web_push

            ready, _reason = push_ready()
            if not ready:
                return
            title = f"AgentWire — {session}" if session else "AgentWire"
            await asyncio.to_thread(send_web_push, title, text, "/", session or "agentwire")
        except Exception:
            logger.debug("push fan-out failed", exc_info=True)

    async def handle_mobile(self, request: web.Request) -> web.Response:
        """Serve the mobile PTT page — minimal phone surface (#279).

        Static shell on the public bootstrap surface (same exposure class as
        `/`); every API/WS call the page makes carries the bearer token. No
        voices fetch here — the page bootstraps via authenticated API calls.
        """
        response = aiohttp_jinja2.render_template("mobile.html", request, {})
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    async def handle_pair_page(self, request: web.Request) -> web.Response:
        """Serve the device-pairing page (#423).

        Public bootstrap surface: an unpaired device has no token yet. The page
        reads the pairing code from `?code=` (or a manual field), POSTs it to
        `/api/pair`, and stores the minted device token in localStorage — the
        same key `apiFetch` reads — then bounces to the portal.
        """
        response = aiohttp_jinja2.render_template("pair.html", request, {})
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    # Pairing rate limit: max 5 attempts / IP / minute, 30 globally / minute.
    _PAIR_WINDOW = 60.0
    _PAIR_PER_IP = 5
    _PAIR_GLOBAL = 30

    def _pair_rate_ok(self, ip: str) -> bool:
        """Record a pairing attempt; False if it exceeds the per-IP or global cap."""
        now = time.monotonic()
        cutoff = now - self._PAIR_WINDOW
        self._pair_attempts_global = [t for t in self._pair_attempts_global if t > cutoff]
        per_ip = [t for t in self._pair_attempts.get(ip, []) if t > cutoff]
        if len(per_ip) >= self._PAIR_PER_IP or len(self._pair_attempts_global) >= self._PAIR_GLOBAL:
            self._pair_attempts[ip] = per_ip  # keep pruned state, don't record the rejected attempt
            return False
        per_ip.append(now)
        self._pair_attempts[ip] = per_ip
        self._pair_attempts_global.append(now)
        # Drop IP buckets that pruned to empty so the dict can't grow unbounded.
        self._pair_attempts = {k: v for k, v in self._pair_attempts.items() if v}
        return True

    def _on_auth_lockout(self, ip: str, failures: int) -> None:
        """Owner-notify when an IP is locked out for auth-token spraying (#498).

        The security middleware calls this synchronously on the lockout-crossing;
        send_email blocks (HTTP to Resend), so offload it to a thread to keep the
        event loop responsive. Best-effort — a failed email never wedges auth.
        """
        import socket as _socket

        def _send() -> None:
            try:
                from .channels.email import send_email

                send_email(
                    subject=f"[agentwire] portal auth lockout: {ip}",
                    body=(
                        f"The portal on `{_socket.gethostname()}` locked out "
                        f"`{ip}` after {failures} failed auth attempts within "
                        f"{int(security.AUTH_FAIL_WINDOW)}s — possible token spray "
                        "against an exposed portal. Further requests from that IP "
                        "are rejected with 429 until the window clears."
                    ),
                )
            except Exception:
                logger.exception("auth-lockout owner email failed")

        try:
            asyncio.get_running_loop().run_in_executor(None, _send)
        except RuntimeError:
            _send()

    async def api_pair(self, request: web.Request) -> web.Response:
        """Redeem a pairing code for a freshly-minted device token (#423).

        Public (no bearer required) but gated by the short-lived pairing code the
        host printed via `agentwire portal pair`. One-shot: the code is consumed.
        """
        from .devices import DeviceRegistry, consume_pairing

        # Rate limit (#423 S1): this is the one unauthenticated token-minting
        # endpoint. Throttle per-IP and globally over a sliding window so a
        # brute-forcer can't grind pairing codes (40-bit, 10-min TTL).
        if not self._pair_rate_ok(request.remote or "?"):
            return web.json_response(
                {"error": "Too many pairing attempts — wait a minute and retry."},
                status=429,
            )

        try:
            data = await request.json()
        except Exception:
            data = {}
        code = str(data.get("code", "")).strip()
        if not code:
            return web.json_response({"error": "Missing pairing code"}, status=400)

        pairing = consume_pairing(code)
        if pairing is None:
            return web.json_response(
                {"error": "Invalid or expired pairing code"}, status=403
            )

        name = str(data.get("name") or pairing.name or "device")
        registry = DeviceRegistry.load()
        device, token = registry.add(name=name)
        logger.info("Paired device %s (%r)", device.id, device.name)
        return web.json_response(
            {
                "token": token,
                "device": device.public(),
            }
        )

    def _ws_response(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocketResponse that echoes the auth bearer subprotocol.

        Browsers pass the token as `Sec-WebSocket-Protocol:
        agentwire.bearer.<token>` (validated by the security middleware before
        the handler runs) and close the socket unless the server echoes the
        protocol back.
        """
        offered = [
            p.strip()
            for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
            if p.strip()
        ]
        bearer = tuple(p for p in offered if p.startswith(WS_PROTOCOL_PREFIX))
        return web.WebSocketResponse(protocols=bearer)

    async def handle_dashboard_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint for dashboard updates (sessions, machines, config)."""
        ws = self._ws_response(request)
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

    async def _wait_for_pane_ready(self, session_name: str, timeout: float = 2.0) -> bool:
        """Poll `tmux capture-pane -p` until non-empty (or timeout).

        After `agentwire new` returns, the tmux session exists but the agent
        process started via `tmux send-keys` may not have rendered its first
        frame. A WS attach in that window can race the startup and show a
        disconnected/reconnect overlay. We use capture-pane as a cheap "is the
        pane producing output yet?" probe — usually <100ms, never longer than
        `timeout` so the UI never feels stuck.

        Returns True if pane became non-empty before timeout, False otherwise.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        # parse_session_name handles "name", "project/branch", and trailing "@machine"
        local_name = session_name.split("@", 1)[0]
        while asyncio.get_event_loop().time() < deadline:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "tmux", "capture-pane", "-p", "-t", local_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await proc.communicate()
                if stdout and stdout.strip():
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.05)
        return False

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
        """
        data = await request.json()
        text = data.get("text", "")
        if not text:
            return web.json_response({"success": False, "error": "text required"}, status=400)

        import uuid
        notification_id = data.get("id") or str(uuid.uuid4())[:8]
        session = data.get("session")
        priority = data.get("priority", "normal")

        if session:
            stale_ids = [
                nid for nid, n in self.active_notifications.items()
                if n.get("session") == session
            ]
            for nid in stale_ids:
                self.active_notifications.pop(nid, None)
                await self.broadcast_dashboard("notification_dismiss", {"id": nid})

        notification = {
            "id": notification_id,
            "text": text,
            "session": session,
            "priority": priority,
            "timestamp": time.time(),
        }

        self.active_notifications[notification_id] = notification

        clients = len(self.dashboard_clients)
        await self.broadcast_dashboard("notification", notification)
        await self._fanout_push(text, session=session, priority=priority)

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

    def _compute_session_states(self, sessions: list[dict]) -> None:
        """Glanceable per-session state for the mobile page (#290).

        Annotates each session dict with `state` (and `state_kind` /
        `state_hint` when blocked on a prompt). Precedence:

          off         pane 0 runs no agent (Claude exited → bare shell);
                      also the fallback for anything unrecognized/errored —
                      when in doubt show off, never a false working/idle
          needs_input prompt-router marker or live pending_permission
          working     recent output (activity_threshold_seconds)
          idle        agent present, nothing pending, no recent output
        """
        from . import prompt_router

        try:
            markers: dict[str, dict] = {}
            for m in prompt_router.list_markers():
                markers.setdefault(str(m.get("session", "")).split("@")[0], m)
        except Exception:
            markers = {}

        for s in sessions:
            name = s.get("name", "")
            state, kind, hint = "off", None, None
            try:
                if name and prompt_router.is_agent_pane(name, 0):
                    marker = markers.get(name.split("@")[0])
                    session_obj = self.active_sessions.get(name)
                    pending = session_obj.pending_permission if session_obj else None
                    if marker:
                        state = "needs_input"
                        kind = marker.get("kind")
                        hint = marker.get("question") or None
                    elif pending:
                        state = "needs_input"
                        kind = "permission"
                        tool = pending.request.get("tool_name", "")
                        hint = f"Claude wants to use {tool}" if tool else "Permission requested"
                    elif self._get_global_session_activity(name) == "active":
                        state = "working"
                    else:
                        state = "idle"
            except Exception:
                state, kind, hint = "off", None, None
            s["state"] = state
            s["state_kind"] = kind
            s["state_hint"] = hint

    async def monitor_all_sessions(self):
        """Background task to monitor all session activity for dashboard indicators.

        Single source of truth for the dashboard's per-session activity dots:
        captures every session's output once per tick **in-process** via
        `self.agent.get_output` (the same `tmux capture-pane` `_poll_output`
        uses) and broadcasts `session_activity` only on state change. This
        replaces the previous per-session `agentwire output` subprocess, which
        re-imported the full CLI on every tick (#489).

        `_poll_output` remains live-streaming only — it does not touch the
        dashboard activity broadcasts.
        """
        threshold = self.config.server.activity_threshold_seconds
        # Track per-session state: {session_name: {"last_output": str, "last_active": bool}}
        session_states: dict[str, dict] = {}

        logger.info(f"[Monitor] Starting session monitor (threshold: {threshold}s)")

        while True:
            try:
                await self._monitor_tick(session_states, threshold)
                await asyncio.sleep(0.5)  # Poll every 500ms

            except asyncio.CancelledError:
                logger.info("[Monitor] Session monitor stopped")
                break
            except Exception as e:
                logger.debug(f"[Monitor] Error in monitor loop: {e}")
                await asyncio.sleep(2)  # Back off on errors

    async def _monitor_tick(self, session_states: dict[str, dict], threshold: float):
        """Run exactly one monitor pass over all sessions.

        Lists sessions (local + remote), captures each one's output in-process
        via ``agent.get_output``, broadcasts ``session_activity`` only on state
        change, and prunes vanished sessions. Mutates ``session_states`` in
        place. Extracted from the poll loop so tests can drive a single,
        fully-deterministic tick — awaiting this coroutine guarantees every
        listed session has been polled, with no wall-clock sampling race.
        """
        loop = asyncio.get_event_loop()

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
                # Capture output in-process (no subprocess / CLI re-import).
                current_output = await loop.run_in_executor(
                    None,
                    lambda n=session_name: self.agent.get_output(n, lines=50),
                )

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

    async def idle_nag_loop(self):
        """Background task: periodically check for idle sessions.

        Gathers idle session data for every non-service tmux session (whether
        or not its terminal is currently open in the dashboard) and sends it
        to the agentwire-notifications session, which crafts a natural TTS
        message and speaks it via say(). The dashboard itself must have at
        least one connected client — no listeners means no nags.
        """
        NAG_INTERVAL = 120  # seconds between scans  # noqa: N806  # function-local constant
        NAG_IDLE_THRESHOLD = 120  # seconds idle before including in nag (2 min minimum)  # noqa: N806  # function-local constant
        NAG_SESSION = "agentwire-notifications"  # noqa: N806  # function-local constant
        SERVICE_PREFIX = "agentwire-"  # noqa: N806  # function-local constant
        nag_counts: dict[str, int] = {}  # session -> nag count this idle episode
        # session -> last_output_timestamp captured at its last nag. Drives the
        # edge-trigger: a continuously-idle session keeps a fixed timestamp, so
        # it nags once; a new question/error advances the timestamp and re-nags.
        nagged_output_ts: dict[str, float] = {}

        logger.info("[IdleNag] Starting idle nag loop (interval: %ds, threshold: %ds)",
                     NAG_INTERVAL, NAG_IDLE_THRESHOLD)

        # Let the monitor warm up first
        await asyncio.sleep(10)

        while True:
            try:
                if not self.dashboard_clients:
                    nag_counts.clear()
                    nagged_output_ts.clear()
                    await asyncio.sleep(NAG_INTERVAL)
                    continue

                # Find every non-service session that has gone idle. Whether
                # its terminal is currently open in the dashboard doesn't
                # matter — the user wants to know it needs attention either
                # way; they may have closed or minimized the window.
                idle_sessions = []
                for name, info in self.session_activity.items():
                    if name.startswith(SERVICE_PREFIX):
                        continue
                    if name == NAG_SESSION:
                        continue
                    last_ts = info.get("last_output_timestamp", 0.0)
                    idle_secs = time.time() - last_ts if last_ts else float('inf')
                    if idle_secs > NAG_IDLE_THRESHOLD:
                        # Edge-trigger: only nag on a never-nagged session or
                        # when its output has genuinely changed since last nag.
                        if should_nag_idle_session(name, last_ts, nagged_output_ts):
                            idle_sessions.append((name, idle_secs, last_ts))
                    else:
                        # Active again — reset the episode so it can nag afresh.
                        nag_counts.pop(name, None)
                        nagged_output_ts.pop(name, None)

                # Empty batch → do NOT wake the notifications agent. This is the
                # core of the edge-trigger: a continuously-idle session is sent
                # exactly once, then the agent stays asleep.
                if not idle_sessions:
                    await asyncio.sleep(NAG_INTERVAL)
                    continue

                # Gather session metadata
                sessions_info = await self._get_sessions_data()
                sessions_by_name = {s.get("name", ""): s for s in sessions_info}

                # Fetch fresh output for each idle session
                session_data = []
                for name, idle_secs, last_ts in idle_sessions:
                    nag_counts[name] = nag_counts.get(name, 0) + 1
                    nagged_output_ts[name] = last_ts
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

    async def autostart_custom_services(self):
        """Boot autostart custom services shortly after portal launch.

        This is the reboot fix for #214: the launchd plist runs
        `agentwire portal start`, not `agentwire up`, so the server itself
        is the convergence point. Delegates to the CLI (single source of
        truth) — `services up --all` is idempotent and skips downed services.
        """
        await asyncio.sleep(5)  # let the portal finish binding first
        try:
            success, result = await self.run_agentwire_cmd(["services", "up", "--all"])
            if success:
                started = [r["name"] for r in result.get("results", []) if r.get("result") == "started"]
                if started:
                    logger.info("[Services] Autostarted: %s", ", ".join(started))
                else:
                    logger.info("[Services] All autostart services already running")
            else:
                logger.warning("[Services] Autostart failed: %s", result.get("error"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[Services] Autostart error: %s", e)

    async def _post_toast(self, text: str, session: str, priority: str = "high",
                          id_prefix: str = "toast") -> None:
        """Post a dashboard toast notification, replacing any stale toast
        for the same session key."""
        notification_id = f"{id_prefix}-{str(uuid.uuid4())[:8]}"
        stale = [nid for nid, n in self.active_notifications.items()
                 if n.get("session") == session]
        for nid in stale:
            self.active_notifications.pop(nid, None)
            await self.broadcast_dashboard("notification_dismiss", {"id": nid})
        notification = {
            "id": notification_id,
            "text": text,
            "session": session,
            "priority": priority,
            "timestamp": time.time(),
        }
        self.active_notifications[notification_id] = notification
        await self.broadcast_dashboard("notification", notification)
        await self._fanout_push(text, session=session, priority=priority)

    async def _notify_service_event(self, name: str, text: str, speak: bool):
        """Toast (+ optional TTS) for a service watchdog event."""
        await self._post_toast(text, session=f"service:{name}", priority="high",
                               id_prefix=f"svc-{name}")
        if speak:
            await self.run_agentwire_cmd(["say", text], json_output=False)

    async def service_watchdog_loop(self):
        """Background task: healthcheck registered custom services.

        Per-service cadence from healthcheck.interval. On failure: toast +
        TTS on the healthy→unhealthy transition, then respawn per the
        service's restart policy with exponential backoff (WatchdogState in
        services.py — pure and unit-tested). `services down` services are
        skipped entirely. Backoff state is in-memory; a portal restart
        resets it, which is fine.
        """
        from .services import WatchdogState

        TICK = 15  # seconds between scheduling passes  # noqa: N806  # function-local constant
        states: dict[str, WatchdogState] = {}
        last_check: dict[str, float] = {}

        logger.info("[Watchdog] Service watchdog started")
        await asyncio.sleep(30)  # let autostart finish before first checks

        while True:
            try:
                now = time.time()
                success, result = await self.run_agentwire_cmd(["services", "status"])
                if not success:
                    logger.debug("[Watchdog] status failed: %s", result.get("error"))
                    await asyncio.sleep(TICK)
                    continue

                for status in result.get("services", []):
                    name = status["name"]
                    interval = status.get("healthcheck", {}).get("interval", 60)
                    if now - last_check.get(name, 0) < interval:
                        continue
                    last_check[name] = now

                    if status.get("disabled") or not status.get("autostart"):
                        states.pop(name, None)  # not managed while opted out
                        continue

                    state = states.setdefault(name, WatchdogState())
                    actions = state.on_check(now, status["healthy"], status.get("restart", "on-failure"))

                    for action in actions:
                        if action == "notify_down":
                            logger.warning("[Watchdog] %s unhealthy: %s", name, status.get("detail"))
                            await self._notify_service_event(
                                name, f"Service {name} is down ({status.get('detail')})", speak=True)
                        elif action == "notify_recovered":
                            logger.info("[Watchdog] %s recovered", name)
                            await self._notify_service_event(
                                name, f"Service {name} recovered", speak=False)
                        elif action == "restart":
                            logger.info("[Watchdog] Restarting %s (attempt %d)",
                                        name, state.restart_count)
                            await self.run_agentwire_cmd(["services", "up", name])

                await asyncio.sleep(TICK)

            except asyncio.CancelledError:
                logger.info("[Watchdog] Service watchdog stopped")
                break
            except Exception as e:
                logger.debug("[Watchdog] Error: %s", e)
                await asyncio.sleep(TICK)

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connections for a session."""
        name = request.match_info["name"]
        ws = self._ws_response(request)
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
        ws = self._ws_response(request)
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
            # "local" is the implicit machine and is never present in machines.json.
            # Treat it the same as no machine — local tmux attach.
            if machine_id == "local":
                machine_id = None

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
                cmd = ["ssh", "-t", *ssh_base_opts(), "-o", "StrictHostKeyChecking=accept-new", ssh_target, "tmux", "attach", "-t", session_name]
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

                # Set initial PTY size from browser's reported dimensions
                winsize = struct.pack("HHHH", init_rows, init_cols, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                logger.info(f"[Terminal] Set initial PTY size for SSH to {init_cols}x{init_rows} (fd={master_fd})")

                # Heal windows pinned to manual size mode by pre-v1.34 portals
                await unpin_tmux_window(session_name, ssh_target)
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

                # Heal windows pinned to manual size mode by pre-v1.34 portals
                await unpin_tmux_window(session_name)

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
                                # Tell the browser *why* the stream ended so it can decide between
                                # closing the window (session truly gone) vs showing a reconnect
                                # overlay (transient — bg process side effect, portal restart, etc).
                                if not ws.closed:
                                    try:
                                        exit_code = await proc.wait() if proc else None
                                        logger.info(f"[Terminal] tmux attach exit code for {session_name}: {exit_code}")

                                        prefix = "remote" if is_remote else "local"
                                        if exit_code == 0:
                                            await ws.send_json({"type": f"{prefix}_session_ended", "session": session_name})
                                            logger.info(f"[Terminal] Sent {prefix}_session_ended to browser for {session_name}")
                                        else:
                                            await ws.send_json({"type": f"{prefix}_disconnected", "session": session_name})
                                            logger.info(f"[Terminal] Sent {prefix}_disconnected to browser for {session_name}")
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
                                    # Terminal resize: update the PTY and notify the
                                    # tmux/ssh client via SIGWINCH. The window itself
                                    # is sized by tmux per the window-size policy —
                                    # never force it with resize-window -x/-y, which
                                    # pins the window into manual mode (#258).
                                    cols = payload.get("cols", 80)
                                    rows = payload.get("rows", 24)
                                    logger.info(f"[Terminal] Resize {session_name} to {cols}x{rows}")

                                    winsize = struct.pack("HHHH", rows, cols, 0, 0)
                                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                                    if proc and proc.pid:
                                        try:
                                            os.kill(proc.pid, signal.SIGWINCH)
                                        except (OSError, ProcessLookupError):
                                            pass  # Process may have exited

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
    # Dialog/ANSI patterns live in prompt_router (single source of truth,
    # shared with the pane-sweep prompt detector).
    ANSI_PATTERN = prompt_router.ANSI_PATTERN
    ASK_PATTERN = prompt_router.ASK_PATTERN
    ASK_PATTERN_SIMPLE = prompt_router.ASK_PATTERN_SIMPLE

    def _parse_ask_options(self, options_block: str) -> list[dict]:
        """Parse numbered options from AskUserQuestion block."""
        return prompt_router.parse_ask_options(options_block)

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

    async def api_worktrees(self, request: web.Request) -> web.Response:
        """List worktree sessions (across all repos) with read-only git status.

        Thin wrapper over `agentwire worktree --list --all --json`; the CLI
        folds in local-only git status (dirty/ahead/behind/pushed) per entry so
        the sidebar can badge worktree sessions without per-session round-trips.
        """
        try:
            success, result = await self.run_agentwire_cmd(["worktree", "--list", "--all"])
            entries = result.get("entries", []) if success else []
            return web.json_response({"entries": entries})
        except Exception as e:
            logger.error(f"Failed to list worktrees: {e}")
            return web.json_response({"entries": []})

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

            # Computed state (off/needs_input/working/idle) — shells out to
            # tmux per session, so keep it off the event loop.
            await asyncio.to_thread(self._compute_session_states, sessions)

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

    async def api_projects_create(self, request: web.Request) -> web.Response:
        """Create a new local project.

        Body:
            {
                "name": "myproject",          # required, alphanumerics + ._-
                "clone_url": "git@..."         # optional, clone from this URL
                "git_init": false              # optional, init empty git repo (ignored with clone_url)
            }

        Response:
            {"success": true, "name": "...", "path": "...", "machine": "local"}
            {"success": false, "error": "..."}
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "Invalid JSON body"}, status=400)

        name = (data.get("name") or "").strip()
        clone_url = (data.get("clone_url") or "").strip() or None
        git_init = bool(data.get("git_init"))

        if not name:
            return web.json_response({"success": False, "error": "name is required"}, status=400)

        args = ["projects", "create", name]
        if clone_url:
            args.extend(["--from", clone_url])
        elif git_init:
            args.append("--git-init")

        success, result = await self.run_agentwire_cmd(args)
        if not success:
            return web.json_response({"success": False, "error": result.get("error", "Unknown error")}, status=400)
        return web.json_response(result)

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

            if not path or not isinstance(path, str):
                return web.json_response({"success": False, "error": "Missing path"})
            if delete_type not in ("config", "folder"):
                return web.json_response({"success": False, "error": "Invalid deleteType"})

            # Path validation: absolute, no traversal, no shell metacharacters.
            # The endpoint has no auth (local-trust model — see SECURITY.md), so
            # treat the input as untrusted regardless and reject anything that
            # could escape argv quoting on either local or remote (SSH) execution.
            if not path.startswith("/"):
                return web.json_response({"success": False, "error": "path must be absolute"})
            if ".." in Path(path).parts:
                return web.json_response({"success": False, "error": "path may not contain '..'"})
            if re.search(r"[\s;&|`$<>(){}\[\]\\\"'*?#]", path):
                return web.json_response({"success": False, "error": "path contains disallowed characters"})
            if path.rstrip("/") in ("", "/root", "/home", "/Users", "/tmp", "/etc") or path.rstrip("/") in ("~", "$HOME"):
                return web.json_response({"success": False, "error": "Cannot delete protected paths"})

            # Build argv. For SSH we still need to cross a remote shell, so
            # quote with shlex; locally we use array form with shell=False.
            if delete_type == "config":
                target = f"{path.rstrip('/')}/.agentwire.yml"
                local_argv = ["rm", "-f", target]
            else:
                local_argv = ["rm", "-rf", path]

            if machine and machine != "local":
                # Remote shell — quote each argv element through shlex.
                remote_cmd = " ".join(shlex.quote(a) for a in local_argv)
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["ssh", *ssh_base_opts(), machine, remote_cmd],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            else:
                result = await asyncio.to_thread(
                    subprocess.run,
                    local_argv,
                    capture_output=True,
                    text=True,
                    timeout=30,
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

    async def api_session_defaults(self, request: web.Request) -> web.Response:
        """Resolve a new session's defaults via the CLI (the single resolver).

        Query params: kind (default orchestrator), posture, harness.
        Response: {kind, posture, harness, session_type, roles, postures}.
        The new-session UI reads this instead of hardcoding posture/harness or
        the intrinsic role chips.
        """
        kind = request.query.get("kind", "orchestrator")
        posture = request.query.get("posture")
        harness = request.query.get("harness")
        args = ["session-defaults", "--kind", kind]
        if posture:
            args += ["--posture", posture]
        if harness:
            args += ["--harness", harness]
        success, result = await self.run_agentwire_cmd(args)
        if not success:
            return web.json_response({"error": result.get("error", "Failed to resolve defaults")}, status=400)
        return web.json_response(result)

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

        # Thin wrapper: the git/SSH logic lives in the CLI (SSOT).
        success, result = await self.run_agentwire_cmd(
            ["repo-info", "--path", path, "--machine", machine]
        )
        if not success:
            logger.error(f"repo-info failed for {path} on {machine}: {result.get('error')}")
            return web.json_response(
                {"exists": False, "is_git": False, "current_branch": None},
                status=500,
            )

        return web.json_response({
            "exists": result.get("exists", False),
            "is_git": result.get("is_git", False),
            "current_branch": result.get("current_branch"),
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

        # Thin wrapper: the git/SSH logic lives in the CLI (SSOT).
        success, result = await self.run_agentwire_cmd(
            ["branches", "--path", path, "--machine", machine, "--prefix", prefix]
        )
        if not success:
            logger.error(f"branches failed for {path} on {machine}: {result.get('error')}")
            return web.json_response({"existing": []}, status=500)

        return web.json_response({"existing": result.get("existing", [])})

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
            # Posture × harness are the canonical axes; a legacy fused `type`
            # is still accepted. When posture/harness are present they win.
            posture = (data.get("posture") or "").strip()
            harness = (data.get("harness") or "").strip()
            session_type = data.get("type") if not (posture or harness) else None
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
            # Session type: posture × harness when given, else legacy --type.
            if posture:
                args.extend(["--posture", posture])
            if harness:
                args.extend(["--harness", harness])
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
                yaml_config = self._read_agentwire_yaml(session_path, machine_id) or {}
                yaml_config["voice"] = voice
                self._write_agentwire_yaml(session_path, yaml_config, machine_id)

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

    async def _deliver_first_message(self, session_name: str, message: str) -> None:
        """Background: wait for the agent to boot, then deliver the first
        message with verification (via `agentwire send --wait-ready`)."""
        success, result = await self.run_agentwire_cmd(
            ["send", "-s", session_name, "--wait-ready", "--timeout", "60", "--", message]
        )
        if not success:
            logger.warning(f"First message delivery failed for {session_name}: {result.get('error')}")
            await self._post_toast(
                f"First message not delivered to {session_name} — paste it manually",
                session=session_name, priority="high", id_prefix="firstmsg")

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

    async def _handle_static(self, request: web.Request) -> web.StreamResponse:
        """Serve /static assets with Cache-Control + on-the-fly gzip.

        aiohttp's ``add_static`` adds neither, so a phone first-load streams
        ~445KB of uncompressed JS and repeat loads re-fetch unconditionally.
        This handler gzips compressible text (cached in-memory by path+mtime)
        and stamps Cache-Control on everything (#488).
        """
        # Resolve safely under the static root (block path traversal).
        target = (STATIC_ROOT / request.match_info["path"]).resolve()
        if not target.is_relative_to(STATIC_ROOT) or not target.is_file():
            raise web.HTTPNotFound()

        suffix = target.suffix.lower()
        cache = STATIC_CACHE_IMAGE if suffix in IMAGE_SUFFIXES else STATIC_CACHE_CODE
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"

        accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "")
        if suffix in COMPRESSIBLE_SUFFIXES and accepts_gzip:
            body = self._gzipped_static(target)
            return web.Response(
                body=body,
                content_type=content_type,
                charset="utf-8",
                headers={
                    "Cache-Control": cache,
                    "Content-Encoding": "gzip",
                    "Vary": "Accept-Encoding",
                },
            )

        # Pass Content-Type explicitly: web.FileResponse otherwise guesses via
        # aiohttp's private mimetypes instance, which (unlike our module-level
        # add_type registrations) lacks .webp/.avif/.woff2 on hermetic CI
        # runners whose system mime DB is bare — serving octet-stream (#525).
        return web.FileResponse(
            target, headers={"Cache-Control": cache, "Content-Type": content_type}
        )

    def _gzipped_static(self, path: Path) -> bytes:
        """Return gzipped bytes for a static file, cached by path + mtime."""
        mtime = path.stat().st_mtime
        cached = self._gzip_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        body = gzip.compress(path.read_bytes(), compresslevel=6)
        self._gzip_cache[path] = (mtime, body)
        return body

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
            # Serve the small pre-generated WebP thumbnails (see
            # scripts/generate_icon_thumbnails.py) rather than the full-res
            # PNG/JPEG sources — the sidebar renders these at ~48px.
            if not directory.exists():
                return []
            return sorted([
                f.name for f in directory.iterdir()
                if f.is_file() and f.suffix.lower() == ".webp"
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
                        "ssh", *ssh_base_opts(), "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
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
                    "ssh", *ssh_base_opts(), "-o", f"ConnectTimeout={connect_timeout}", "-o", "BatchMode=yes",
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
                {"key": "TTS URL", "value": self.config.tts.url or "—"},
                {"key": "TTS Default Voice", "value": self.config.tts.default_voice},
                {"key": "STT Backend", "value": self.config.stt.backend},
                {"key": "STT URL", "value": self.config.stt.url or "—"},
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
                # Matches patterns like: api_key: "secret" or auth_token: secret
                content = re.sub(
                    r'((?:api_key|auth_token)\s*:\s*)["\']?[^"\'\n]+["\']?',
                    r'\1"[REDACTED]"',
                    content
                )
            except IOError as e:
                return web.json_response({"error": str(e)})
        else:
            # Return default config template (instant mode: browser voice, loopback)
            content = """# AgentWire Configuration
server:
  host: "127.0.0.1"
  port: 8765

tts:
  backend: "default"  # browser voice — or "custom" with url: pointing at your shim
  # url: "http://localhost:8100"

stt:
  backend: "default"  # browser speech recognition — or "cloud" (hosted API),
                      # or "custom" with url:
  # url: "http://localhost:8101"
  # cloud:  # any OpenAI-compatible transcription API; key from env, never in config
  #   base_url: "https://api.openai.com/v1"
  #   model: "gpt-4o-mini-transcribe"
  #   api_key_env: "OPENAI_API_KEY"

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
        """Save config file contents.

        Security-critical keys are frozen (#425): even a valid token cannot use
        this endpoint to disable auth, move the bind host, rewrite the
        executables/services that run as RCE, or turn off the damage-control
        rules. Those are host-file-edit-only.
        """
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
            old_content = config_path.read_text() if config_path.exists() else ""

            # Reverse the read-side secret redaction so saving the editor's text
            # back doesn't overwrite real secrets with "[REDACTED]" (and so the
            # frozen-key check below sees the true auth_token, not the marker).
            content = restore_redactions(content, old_content)

            violations = frozen_config_violations(content, old_content)
            if violations:
                return web.json_response(
                    {
                        "error": (
                            "These keys are frozen and can only be changed by "
                            "editing ~/.agentwire/config.yaml on the host: "
                            + ", ".join(violations)
                        ),
                        "frozen_keys": violations,
                    },
                    status=403,
                )

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

    async def api_safety_status(self, request: web.Request) -> web.Response:
        """Damage-control current state: master enable, disabled rules, today's counts."""
        try:
            from .safety_commands import LOGS_DIR, load_patterns
            patterns = load_patterns()
            today = datetime.now().strftime("%Y-%m-%d")
            log_file = LOGS_DIR / f"{today}.jsonl"
            counts: Dict[str, int] = {}
            if log_file.exists():
                try:
                    with open(log_file, "r") as f:
                        for line in f:
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            d = entry.get("decision", "")
                            counts[d] = counts.get(d, 0) + 1
                except Exception:
                    pass
            # Read the kill switch + disabled rules from the agent-unwritable
            # damage-control policy files (#466), not from config.yaml.
            from .safety._core import load_safety_config
            safety_cfg = load_safety_config()
            return web.json_response({
                "enabled": safety_cfg.get("enabled", True),
                "disabled_rules": list(safety_cfg.get("disabled_rules", []) or []),
                "rule_count": len(patterns.get("bashToolPatterns", [])),
                "today_counts": counts,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_safety_logs(self, request: web.Request) -> web.Response:
        """Recent safety audit log entries with optional filters."""
        try:
            from .safety_commands import query_audit_logs
            limit = int(request.query.get("limit", "200"))
            decision = request.query.get("decision")
            entries = query_audit_logs()  # all entries, newest-first by file
            if decision:
                wanted = {d.strip() for d in decision.split(",") if d.strip()}
                entries = [e for e in entries if e.get("decision") in wanted]
            if limit > 0:
                entries = entries[-limit:]
            return web.json_response({"entries": entries})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_safety_rules(self, request: web.Request) -> web.Response:
        """Flat list of bash-rule IDs with their patterns/reasons."""
        try:
            from .safety_commands import load_patterns
            patterns = load_patterns()
            rules = []
            for p in patterns.get("bashToolPatterns", []):
                if not isinstance(p, dict):
                    continue
                rules.append({
                    "id": p.get("id"),
                    "pattern": p.get("pattern"),
                    "reason": p.get("reason"),
                    "ask": bool(p.get("ask", False)),
                    "bypassable": bool(p.get("bypassable", False)),
                    "source": p.get("source"),
                })
            return web.json_response({"rules": rules})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_safety_config_post(self, request: web.Request) -> web.Response:
        """Frozen (#425): the safety block is host-file-edit-only.

        Disabling the damage-control rules with the same token that the rules are
        meant to contain defeats defense-in-depth. The master switch and
        disabled-rules list can now only be changed by editing
        ~/.agentwire/damagecontrol.yml on the host — itself a protected,
        agent-unwritable control-plane file (#466). GET /api/safety/* still works
        for viewing status, rules and logs.
        """
        return web.json_response(
            {
                "error": (
                    "Safety configuration is frozen and can only be changed by "
                    "editing ~/.agentwire/damagecontrol.yml on the host, then "
                    "reloading the portal."
                ),
                "frozen_keys": ["safety"],
            },
            status=403,
        )

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
        """Transcribe audio to text.

        Decodes WebM/Opus uploads in-process via PyAV (no ffmpeg subprocess
        startup) and resamples to 16 kHz mono PCM16 — the canonical input
        shape for Whisper- and Moonshine-class models. Optionally prepends a
        configurable amount of silence (``stt.silence_prepend_ms``, default 0).

        All three tiers transcribe server-side: default and custom via an HTTP
        shim, cloud via a hosted API. If the default-tier shim isn't ready yet
        the backend raises and this endpoint answers 500 (the client is already
        using browser speech recognition until /api/voice-status flips).
        """
        try:
            reader = await request.multipart()
            audio_field = await reader.next()

            if audio_field is None:
                return web.json_response({"error": "No audio data"})

            audio_data = await audio_field.read()
            if not audio_data:
                return web.json_response({"error": "Empty audio data"})

            silence_ms = int(getattr(self.config.stt, "silence_prepend_ms", 0) or 0)

            try:
                wav_data = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._decode_audio_to_wav,
                    audio_data,
                    silence_ms,
                )
            except Exception as e:
                logger.error("Failed to decode audio: %s", e)
                return web.json_response({"error": "Audio conversion failed"})

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_data)
                wav_path = f.name

            try:
                logger.info("Transcribing %s via %s backend", wav_path, type(self.stt).__name__)
                text = await self.stt.transcribe(Path(wav_path))
                logger.info("Transcription result: %s", text)
                return web.json_response({"text": text})
            finally:
                Path(wav_path).unlink(missing_ok=True)

        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return web.json_response({"error": str(e)})

    @staticmethod
    def _decode_audio_to_wav(audio_data: bytes, silence_prepend_ms: int = 0) -> bytes:
        """Decode arbitrary input audio (WebM/Opus, MP3, M4A, …) to 16 kHz mono PCM16 WAV.

        Replaces the previous ``ffmpeg -i in.webm out.wav`` subprocess. Subprocess
        cold-start was 100–300 ms before any actual decoding; PyAV uses libav
        bindings in-process so the only cost is the decoding itself.
        """
        import io
        import wave

        import av  # PyAV — declared in pyproject.toml `dependencies`

        target_rate = 16000

        with av.open(io.BytesIO(audio_data), mode="r") as container:
            if not container.streams.audio:
                raise RuntimeError("Input contains no audio stream")

            resampler = av.AudioResampler(format="s16", layout="mono", rate=target_rate)
            pcm_chunks: list[bytes] = []

            def _frame_bytes(f) -> bytes:
                # AudioFrame.planes[0] may include SIMD alignment padding; slice
                # to the exact PCM length (samples × channels × bytes_per_sample).
                bytes_per_sample = f.format.bytes  # 2 for s16
                channels = len(f.layout.channels)  # 1 for mono
                size = f.samples * channels * bytes_per_sample
                return bytes(f.planes[0])[:size]

            for frame in container.decode(audio=0):
                for resampled in resampler.resample(frame):
                    pcm_chunks.append(_frame_bytes(resampled))
            for resampled in resampler.resample(None):
                pcm_chunks.append(_frame_bytes(resampled))

        if silence_prepend_ms > 0:
            silence_samples = int(target_rate * silence_prepend_ms / 1000)
            pcm_chunks.insert(0, b"\x00\x00" * silence_samples)

        pcm_data = b"".join(pcm_chunks)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # 16-bit
            wav.setframerate(target_rate)
            wav.writeframes(pcm_data)
        return buf.getvalue()

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

            # Default tier: Kokoro shim on local speakers when ready, OS voice
            # while it warms up (process-isolated — see ensure_managed_tts)
            if self.config.tts.backend == "default":
                from .utils.speech import strip_speech_tags

                clean = strip_speech_tags(text)
                if await self._kokoro_shim_ready():
                    try:
                        session_config = self._get_session_config(name)
                        wav = await self._tts_generate(
                            clean, voice or session_config.voice
                        )
                        if wav and await self._play_wav_locally(wav):
                            return web.json_response(
                                {"success": True, "tier": "default", "engine": "kokoro"}
                            )
                    except Exception as e:
                        logger.error(f"Kokoro local TTS failed: {e}")
                ok = await self._os_say(clean)
                if ok:
                    return web.json_response({"success": True, "tier": "default"})
                return web.json_response(
                    {"success": False, "error": "OS voice playback failed"},
                    status=500,
                )

            # Get session config for defaults
            session_config = self._get_session_config(name)
            if voice is None:
                voice = session_config.voice
            exaggeration = session_config.exaggeration
            cfg_weight = session_config.cfg_weight

            logger.info(f"[{name}] Local TTS: {text[:50]}... (voice={voice})")

            # Generate audio via TTS shim HTTP call
            audio_data = await self._tts_generate(
                text=text,
                voice=voice,
                instructions=self.config.tts.instructions or None,
                options=self._tts_envelope_options(exaggeration, cfg_weight),
            )

            if not audio_data:
                return web.json_response(
                    {"success": False, "error": "TTS generation returned no audio"},
                    status=500
                )

            if await self._play_wav_locally(audio_data):
                return web.json_response({"success": True})
            return web.json_response(
                {"success": False, "error": "Local audio playback failed"},
                status=500,
            )

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
                import subprocess

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
                        subprocess.run(
                            ["agentwire", "send-keys", "-s", tmux_name,
                             "--pane", str(pane_index), "3", custom_message, "Enter"],
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
                        ["agentwire", "send-keys", "-s", tmux_name,
                         "--pane", str(pane_index), keystroke],
                        check=True, capture_output=True
                    )
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

    async def api_services_custom(self, request: web.Request) -> web.Response:
        """GET /api/services/custom - Names of config-defined custom services.

        The sidebar merges these into its Services column so user-flagged
        sessions group as services rather than regular sessions.
        """
        try:
            names = [s.name for s in self.config.services.custom]
            return web.json_response({"names": names})
        except Exception as e:
            return web.json_response({"error": str(e), "names": []}, status=500)

    # ─── Scratch pad (shared notes drawer) ──────────────────────────
    # Storage logic lives in agentwire/scratchpad.py (shared with the CLI and
    # MCP tool). Every mutation broadcasts scratchpad_updated so all connected
    # clients (desktop + phone) refresh their drawers.

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
            from .council import state as council_state
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
            from .council import inbox as council_inbox
            from .council import state as council_state
            from .council import view as council_view

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
            from .council import state as council_state
            from .council import view as council_view

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
        from .council import inbox
        from .council import state as council_state
        from .council import view as council_view

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

    async def _start_scheduler_daemon(self) -> bool:
        """Launch the scheduler daemon in a detached tmux session.

        No-op if it's already running. Returns True if it was started.
        """
        if await self._is_scheduler_running():
            return False
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
        return True

    async def api_scheduler_start(self, request: web.Request) -> web.Response:
        """POST /api/scheduler/start - Start the scheduler daemon in tmux."""
        try:
            started = await self._start_scheduler_daemon()
            return web.json_response(
                {"success": True, "status": "started" if started else "already_running"}
            )
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

            # Report how many dashboards received the broadcast. A lifecycle
            # event is ephemeral (not persisted), so 0 clients means nobody saw
            # it — the caller should know that, not get a blind "broadcast" (#444).
            return web.json_response({"success": True, "clients": len(self.dashboard_clients)})

        except Exception as e:
            logger.error(f"Notify API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _os_say(self, text: str) -> bool:
        """Speak via the OS voice (macOS `say` / Linux `espeak`).

        Default-tier fallback when no browser is connected anywhere.
        Absolute path on macOS — users commonly shadow `say` in PATH with an
        `agentwire say` wrapper, which would recurse into a fork bomb.
        """
        import sys as _sys

        binary = "/usr/bin/say" if _sys.platform == "darwin" else "espeak"
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0
        except FileNotFoundError:
            logger.warning(f"OS voice binary not found: {binary}")
            return False

    async def _speak_no_clients_fallback(self, text: str) -> bool:
        """No browser connected: default tier plays on this machine's
        speakers — the Kokoro shim when ready, OS voice while it warms up — so
        notifications stay audible; custom tier returns False (the CLI's
        smart routing handles local playback there)."""
        if self.config.tts.backend != "default":
            return False
        from .utils.speech import strip_speech_tags

        clean = strip_speech_tags(text)
        if await self._kokoro_shim_ready():
            try:
                wav = await self._tts_generate(clean, self.config.tts.default_voice)
                if wav and await self._play_wav_locally(wav):
                    return True
            except Exception as e:
                logger.error(f"Kokoro local playback failed: {e}")
        return await self._os_say(clean)

    async def _play_wav_locally(self, wav_bytes: bytes) -> bool:
        """Play WAV bytes on this machine's speakers (afplay / aplay / paplay / play)."""
        import sys as _sys

        temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            temp_file.write(wav_bytes)
            temp_file.close()
            players = ["afplay"] if _sys.platform == "darwin" else ["aplay", "paplay", "play"]
            for player in players:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        player, temp_file.name,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                    return proc.returncode == 0
                except FileNotFoundError:
                    continue
            logger.warning(f"No local audio player found (tried {', '.join(players)})")
            return False
        finally:
            Path(temp_file.name).unlink(missing_ok=True)

    async def speak(self, session_name: str, text: str) -> bool:
        """Generate TTS audio and send to session clients.

        Audio is broadcast only to clients connected to this specific session
        (terminal, monitor, or chat windows viewing that session).

        Special case: the `agentwire-notifications` session is a meta-session —
        its audio is meant for whoever is watching the dashboard. If it has no
        own clients, fan the audio out across every session that does, so the
        nag is heard wherever the user has a browser window open.

        Returns:
            True if audio was sent to clients, False if no clients connected.
        """
        # Get or create session
        if session_name not in self.active_sessions:
            self.active_sessions[session_name] = Session(
                name=session_name, config=self._get_session_config(session_name)
            )

        session = self.active_sessions[session_name]

        # Notifications session: fan out to whichever session(s) the user
        # currently has open, since this session itself rarely has listeners.
        fanout_targets: list = []
        if session_name == "agentwire-notifications" and not session.clients:
            fanout_targets = [
                s for s in self.active_sessions.values()
                if s.name != session_name and s.clients
            ]
            if not fanout_targets:
                logger.warning(f"[{session_name}] speak: no dashboard listeners anywhere")
                return await self._speak_no_clients_fallback(text)
            logger.info(
                f"[{session_name}] speak: no own clients, fanning out to "
                f"{len(fanout_targets)} session(s): {[s.name for s in fanout_targets]}"
            )
        elif not session.clients:
            logger.warning(f"[{session_name}] speak: no session clients connected")
            return await self._speak_no_clients_fallback(text)
        else:
            logger.info(f"[{session_name}] speak: {len(session.clients)} session client(s)")

        # Notify clients TTS is starting (session clients + dashboard)
        tts_start_msg = {"type": "tts_start", "session": session_name, "text": text}
        broadcast_to = fanout_targets if fanout_targets else [session]
        for target in broadcast_to:
            await self._broadcast(target, tts_start_msg)

        # Default tier: the managed Kokoro shim once its model is warmed up
        # (process-isolated — see ensure_managed_tts). Until then (and if
        # warm-up failed) browsers synthesize the text themselves via
        # speechSynthesis — broadcast clean text instead of audio.
        if self.config.tts.backend == "default":
            from .utils.speech import strip_speech_tags

            clean = strip_speech_tags(text)

            if await self._kokoro_shim_ready():
                voice = session.config.voice or self.config.tts.default_voice

                async def _generate(chunk: str) -> bytes | None:
                    return await self._tts_generate(chunk, voice)

                return await self._speak_chunks(session_name, clean, broadcast_to, _generate)

            speak_msg = {"type": "speak_text", "session": session_name, "text": clean}
            for target in broadcast_to:
                await self._broadcast(target, speak_msg)
            await self.broadcast_dashboard("audio_playing", {"session": session_name})
            return True

        # Custom tier: generate audio via the shim and broadcast WAV chunks.
        # Get voice settings (resolve "random" once per session)
        voice = session.config.voice or self.config.tts.default_voice
        if voice.lower() == "random":
            voice = await self._resolve_voice(voice)
            session.config.voice = voice  # Cache for this session
            logger.info(f"[{session_name}] Resolved random voice to: {voice}")
        exaggeration = session.config.exaggeration
        cfg_weight = session.config.cfg_weight
        logger.info(f"[{session_name}] TTS voice: {voice}")

        async def _generate(chunk: str) -> bytes | None:
            return await self._tts_generate(
                text=chunk,
                voice=voice,
                instructions=self.config.tts.instructions or None,
                options=self._tts_envelope_options(exaggeration, cfg_weight),
            )

        return await self._speak_chunks(session_name, text, broadcast_to, _generate)

    async def _speak_chunks(
        self, session_name: str, text: str, broadcast_to: list, generate
    ) -> bool:
        """Chunk text, render WAV per chunk via `generate(chunk)`, broadcast
        base64 audio messages. Shared by the custom shim tier and the
        in-process Kokoro default tier."""
        await self.broadcast_dashboard("tts_start", {"session": session_name, "text": text})

        try:
            # Split long text into sentence-sized chunks for better TTS quality
            from .utils.chunker import chunk_text
            chunks = chunk_text(text)

            any_sent = False
            for chunk in chunks:
                audio_data = await generate(chunk)

                if audio_data:
                    audio_data = self._prepend_silence(audio_data, ms=300)
                    audio_b64 = base64.b64encode(audio_data).decode()
                    logger.info(f"[{session_name}] Broadcasting audio chunk ({len(audio_b64)} bytes b64)")

                    audio_msg = {"type": "audio", "session": session_name, "data": audio_b64}
                    for target in broadcast_to:
                        await self._broadcast(target, audio_msg)
                    await self.broadcast_dashboard("audio_playing", {"session": session_name})

                    # Schedule audio_done after the actual playback duration
                    # (parsed from the WAV header; fall back to an estimate).
                    duration_sec = self._wav_duration_seconds(audio_data)
                    if duration_sec is None:
                        duration_sec = (len(audio_data) - 44) / 96000
                    asyncio.create_task(
                        self._send_audio_done_delayed(session_name, max(0.5, duration_sec))
                    )
                    any_sent = True
                else:
                    logger.warning(f"[{session_name}] TTS returned no audio data for chunk")

            return any_sent

        except Exception as e:
            logger.error(f"TTS failed for {session_name}: {e}")
            return False

    @staticmethod
    def _wav_duration_seconds(wav_data: bytes) -> float | None:
        """Exact playback duration from the WAV header (data size / byte rate)."""
        try:
            if len(wav_data) < 44 or wav_data[:4] != b'RIFF' or wav_data[8:12] != b'WAVE':
                return None
            pos = 12
            byte_rate = None
            while pos < len(wav_data) - 8:
                chunk_id = wav_data[pos:pos + 4]
                chunk_size = struct.unpack('<I', wav_data[pos + 4:pos + 8])[0]
                if chunk_id == b'fmt ':
                    byte_rate = struct.unpack('<I', wav_data[pos + 16:pos + 20])[0]
                elif chunk_id == b'data' and byte_rate:
                    return chunk_size / byte_rate
                pos += 8 + chunk_size + (chunk_size % 2)
            return None
        except Exception:
            return None

    async def _send_audio_done_delayed(self, session_name: str, delay_sec: float) -> None:
        """Send audio_done to dashboard after estimated playback duration."""
        await asyncio.sleep(delay_sec)
        await self.broadcast_dashboard("audio_done", {"session": session_name})


async def run_server(config: Config):
    """Run the AgentWire server."""
    # Resolve the auth token before the server is built — the security
    # middleware reads it from config. Non-loopback binds auto-generate a
    # token and refuse to start without one; loopback binds only enforce a
    # token if one is already configured (origin checks cover the browser
    # vector locally).
    if is_loopback_host(config.server.host):
        config.server.auth_token = resolve_auth_token(config)
    else:
        config.server.auth_token = ensure_auth_token(config)
    validate_startup_security(config)

    server = AgentWireServer(config)
    await server.init_backends()

    # Default-tier TTS: ensure the Kokoro shim subprocess is running (process
    # isolation — the ~200 MB download + ONNX warm-up happens in that child,
    # never on this event loop). speechSynthesis covers speech until the shim's
    # /health is ok; on py3.14+ (no package) the spawn is gated off and it
    # stays the browser path.
    from .tts import kokoro_importable

    if config.tts.backend == "default" and kokoro_importable():
        asyncio.create_task(server.ensure_managed_tts())

    # Default-tier STT: ensure the Moonshine shim subprocess is running
    # (process isolation — the ~19s ONNX warm-up happens in that child, never
    # on this event loop). Browser SpeechRecognition covers input until the
    # shim's /health is ok; on py3.14+ (no package) the spawn is gated off and
    # it stays the browser path.
    from .stt import moonshine_importable

    if config.stt.backend == "default" and moonshine_importable():
        asyncio.create_task(server.ensure_managed_stt())

    # Cleanup old uploads on startup
    await server.cleanup_old_uploads()

    # Auto-start the scheduler daemon alongside the portal (configurable).
    if config.scheduler.autostart:
        try:
            if await server._start_scheduler_daemon():
                logger.info("Auto-started scheduler daemon")
        except Exception as e:
            logger.warning(f"Scheduler autostart failed: {e}")

    # Start session monitor for all-sessions dashboard indicators
    monitor_task = asyncio.create_task(server.monitor_all_sessions())

    # Start idle nag loop (TTS reminders for idle sessions with open windows)
    idle_nag_task = asyncio.create_task(server.idle_nag_loop())

    # Autostart custom services (incl. the notifications bridge) — the portal
    # is the convergence point, so launchd/`portal start`/`agentwire up` all
    # bring services back without a separate `agentwire up` run.
    autostart_task = asyncio.create_task(server.autostart_custom_services())

    # Watchdog: healthcheck registered services, notify + restart per policy
    watchdog_task = asyncio.create_task(server.service_watchdog_loop())

    # Council board: poll live sittings' replies and push council_update deltas
    council_task = asyncio.create_task(server.council_watch_loop())

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
        for task in (monitor_task, idle_nag_task, autostart_task, watchdog_task, council_task):
            task.cancel()
            try:
                await task
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
