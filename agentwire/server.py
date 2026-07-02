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
import tempfile
import termios
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import aiohttp_jinja2
import jinja2
import yaml
from aiohttp import web

from . import prompt_router, security
from .cached_status import CachedStatusChecker
from .config import Config, load_config
from .routes.artifacts import ArtifactsRoutesMixin, register_artifacts_routes
from .routes.config import ConfigRoutesMixin, register_config_routes
from .routes.council import CouncilRoutesMixin, register_council_routes
from .routes.desktop import DesktopRoutesMixin, register_desktop_routes
from .routes.history import HistoryRoutesMixin, register_history_routes
from .routes.machines import MachinesRoutesMixin, register_machines_routes
from .routes.notify import NotifyRoutesMixin, register_notify_routes
from .routes.permission import PermissionRoutesMixin, register_permission_routes
from .routes.projects import ProjectsRoutesMixin, register_projects_routes
from .routes.push import PushRoutesMixin, register_push_routes
from .routes.safety import SafetyRoutesMixin, register_safety_routes
from .routes.scheduler import SchedulerRoutesMixin, register_scheduler_routes
from .routes.scratchpad import ScratchpadRoutesMixin, register_scratchpad_routes
from .routes.sessions import SessionsRoutesMixin, register_sessions_routes
from .routes.sessions_admin import SessionsAdminRoutesMixin, register_sessions_admin_routes
from .routes.voice import VoiceRoutesMixin, register_voice_routes
from .security import (
    WS_PROTOCOL_PREFIX,
    create_security_headers_middleware,
    create_security_middleware,
    ensure_auth_token,
    is_loopback_host,
    read_multipart_field_limited,
    resolve_auth_token,
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
    created_at: float = field(default_factory=time.monotonic)  # Grace window for #629 eviction
    hidden_clients: set = field(default_factory=set)  # Clients whose tab is hidden (poll backoff)


class AgentWireServer(
    ArtifactsRoutesMixin,
    ConfigRoutesMixin,
    CouncilRoutesMixin,
    DesktopRoutesMixin,
    HistoryRoutesMixin,
    MachinesRoutesMixin,
    NotifyRoutesMixin,
    PermissionRoutesMixin,
    ProjectsRoutesMixin,
    PushRoutesMixin,
    SafetyRoutesMixin,
    SchedulerRoutesMixin,
    ScratchpadRoutesMixin,
    SessionsRoutesMixin,
    SessionsAdminRoutesMixin,
    VoiceRoutesMixin,
):
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
        # Session-listing caches (#627): one in-process listing shared between
        # the monitor loop and the HTTP routes instead of a CLI subprocess (or
        # an SSH round-trip) per tick/request. {scope: (monotonic_ts, data)}
        self._local_list_cache: tuple[float, list] | None = None
        self._remote_list_cache: tuple[float, dict] | None = None
        self._local_list_lock = asyncio.Lock()
        self._remote_list_lock = asyncio.Lock()
        # Shared per-session capture (#628): monitor tick and window streams
        # read the same tmux capture instead of each shelling out independently.
        self._capture_cache: dict[str, tuple[float, str]] = {}
        # Last time an HTTP client polled the session list (mobile page) —
        # keeps the monitor alive for HTTP-only clients when no WS is open.
        self._last_sessions_poll: float = 0.0
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
        # client_max_size is defense-in-depth for non-multipart bodies (JSON
        # config saves, artifact uploads); the streaming multipart paths
        # (/upload, /transcribe) enforce their own per-field limits because
        # aiohttp does not apply client_max_size to request.multipart().
        max_body = (max(config.uploads.max_size_mb, config.artifacts.max_size_mb) + 2) * 1024 * 1024
        self.app = web.Application(
            client_max_size=max_body,
            middlewares=[
                # Outermost so headers land on auth-rejected responses too.
                create_security_headers_middleware(hsts_enabled=config.server.hsts),
                create_security_middleware(
                    config.server.auth_token,
                    config.server.allowed_origins,
                    on_lockout=self._on_auth_lockout,
                ),
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
        register_push_routes(self, self.app)
        self.app.router.add_get("/mobile", self.handle_mobile)
        self.app.router.add_get("/pair", self.handle_pair_page)
        self.app.router.add_post("/api/pair", self.api_pair)
        self.app.router.add_get("/ws", self.handle_dashboard_ws)
        self.app.router.add_get("/ws/{name:.+}", self.handle_websocket)
        self.app.router.add_get("/ws/terminal/{name:.+}", self.handle_terminal_ws)
        register_sessions_routes(self, self.app)
        # Sessions domain: mutate handlers (create/close/config/recreate/fork/…)
        register_sessions_admin_routes(self, self.app)
        self.app.router.add_post("/upload", self.handle_upload)
        # Voice domain: TTS/STT HTTP endpoints (transcribe, send, say, local-tts,
        # answer, voices, voice-status)
        register_voice_routes(self, self.app)
        # Mobile Review window + permission dialogs (from Claude Code hook)
        register_permission_routes(self, self.app)
        register_config_routes(self, self.app)
        register_safety_routes(self, self.app)
        # Icon listing for dynamic icon picker
        self.app.router.add_get("/api/icons/{category}", self.api_icons)
        # History endpoints
        register_history_routes(self, self.app)
        # Tmux hook notifications
        register_notify_routes(self, self.app)
        register_desktop_routes(self, self.app)
        # Services registry (custom service sessions from config)
        self.app.router.add_get("/api/services/custom", self.api_services_custom)

        # Machines (local + configured remotes)
        register_machines_routes(self, self.app)
        # Scratch pad (shared notes drawer)
        register_scratchpad_routes(self, self.app)
        register_projects_routes(self, self.app)
        # Scheduler monitoring endpoints
        register_scheduler_routes(self, self.app)
        # Council seating board: live sittings + per-prompt snapshot
        register_council_routes(self, self.app)
        # Artifact windows: upload and serve agent-generated HTML
        register_artifacts_routes(self, self.app)
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

    async def _run_subprocess(
        self, argv: list[str], timeout: float | None = None
    ) -> tuple[int, str, str] | None:
        """Run a subprocess without blocking the event loop.

        Returns (returncode, stdout, stderr), or None on timeout/spawn failure.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return None
        except Exception:
            return None

    # Session-listing cache TTLs (#627). Local listing is one cheap tmux
    # subprocess; remote is an SSH round-trip per machine, so it gets a much
    # longer window shared by the monitor loop and every HTTP client.
    LOCAL_LIST_TTL = 1.0
    REMOTE_LIST_TTL = 10.0
    # One tmux capture serves both the monitor tick and the window stream
    # within this window (#628) — just under both loops' 500ms cadence.
    CAPTURE_TTL = 0.4

    async def _list_local_sessions(self, max_age: float | None = None) -> list[dict]:
        """In-process local session listing with a short shared TTL cache.

        Returns fresh dict copies — callers annotate entries in place.
        """
        max_age = self.LOCAL_LIST_TTL if max_age is None else max_age
        async with self._local_list_lock:
            cached = self._local_list_cache
            if cached and time.monotonic() - cached[0] < max_age:
                return [dict(s) for s in cached[1]]
            from .pane_cli import list_local_sessions
            try:
                sessions = await asyncio.to_thread(list_local_sessions)
            except Exception as e:
                logger.debug(f"[Sessions] Local listing failed: {e}")
                return []
            self._local_list_cache = (time.monotonic(), sessions)
            return [dict(s) for s in sessions]

    async def _list_remote_sessions(self, max_age: float | None = None) -> dict[str, list[dict]]:
        """In-process remote session listing, grouped by machine, TTL-cached.

        Only machines that answered over SSH appear as keys (reachable but
        session-less machines map to an empty list) — the #629 eviction guard
        relies on this to never evict sessions on an unreachable machine.
        """
        max_age = self.REMOTE_LIST_TTL if max_age is None else max_age
        async with self._remote_list_lock:
            cached = self._remote_list_cache
            if cached and time.monotonic() - cached[0] < max_age:
                return {m: [dict(s) for s in ss] for m, ss in cached[1].items()}
            from .pane_cli import list_remote_sessions
            try:
                by_machine = await asyncio.to_thread(list_remote_sessions)
            except Exception as e:
                logger.debug(f"[Sessions] Remote listing failed: {e}")
                return {}
            self._remote_list_cache = (time.monotonic(), by_machine)
            return {m: [dict(s) for s in ss] for m, ss in by_machine.items()}

    async def _capture_session_output(self, name: str, max_age: float | None = None) -> str:
        """Capture a session's pane output, deduped across consumers (#628).

        The monitor tick and each open window's poll loop run on the same
        500ms cadence; within CAPTURE_TTL they share one tmux capture instead
        of shelling out independently.
        """
        max_age = self.CAPTURE_TTL if max_age is None else max_age
        cached = self._capture_cache.get(name)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        output = await asyncio.to_thread(self.agent.get_output, name, lines=100)
        self._capture_cache[name] = (time.monotonic(), output)
        return output

    async def _get_session_config(self, name: str) -> SessionConfig:
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
        cwd = await self._get_session_cwd(base_name, machine_id)
        if not cwd:
            return SessionConfig(voice=self.config.tts.default_voice)

        # Read .agentwire.yml from that path
        yaml_config = await self._read_agentwire_yaml(cwd, machine_id)
        if not yaml_config:
            return SessionConfig(voice=self.config.tts.default_voice)

        return SessionConfig(
            type=yaml_config.get("type", "claude-bypass"),
            roles=yaml_config.get("roles", []),
            voice=yaml_config.get("voice", self.config.tts.default_voice),
        )

    async def _get_or_create_session(self, name: str) -> "Session":
        """Get-or-create an active_sessions entry without a TOCTOU race.

        The config fetch awaits (possibly a 5s SSH probe), so a naive
        check-then-assign around it lets a concurrent handler register the
        same session first — clobbering its clients and pending_permission.
        Re-check after the await and keep whichever Session landed first.
        """
        session = self.active_sessions.get(name)
        if session is not None:
            return session
        config = await self._get_session_config(name)
        session = self.active_sessions.get(name)
        if session is None:
            session = Session(name=name, config=config)
            self.active_sessions[name] = session
        return session

    async def _get_session_cwd(self, session_name: str, machine_id: str | None = None) -> str | None:
        """Get working directory of a tmux session.

        Args:
            session_name: Base session name (without @machine suffix)
            machine_id: Machine ID if remote, None for local

        Returns:
            Working directory path, or None if session not found
        """
        local_hostname = socket.gethostname().split('.')[0]

        # Check if this is a local session
        is_local = machine_id is None or machine_id == "local" or machine_id == local_hostname

        if is_local:
            # Local tmux lookup
            result = await self._run_subprocess(
                ["tmux", "display-message", "-t", session_name, "-p", "#{pane_current_path}"],
            )
            if result and result[0] == 0 and result[1].strip():
                return result[1].strip()
            return None
        else:
            # Remote tmux lookup via SSH
            machine = self._get_machine_config(machine_id)
            if not machine:
                return None

            host = machine.get("host", "")
            user = machine.get("user", "")
            ssh_target = f"{user}@{host}" if user else host

            cmd = f"tmux display-message -t {shlex.quote(session_name)} -p '#{{pane_current_path}}'"
            result = await self._run_subprocess(
                ["ssh", *ssh_base_opts(), "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", ssh_target, cmd],
                timeout=5,
            )
            if result and result[0] == 0 and result[1].strip():
                return result[1].strip()
            return None

    def _get_machine_config(self, machine_id: str) -> dict | None:
        """Get machine config by ID from machines.json."""
        if hasattr(self.agent, 'machines'):
            for m in self.agent.machines:
                if m.get('id') == machine_id:
                    return m
        return None

    async def _read_agentwire_yaml(self, cwd: str, machine_id: str | None = None) -> dict | None:
        """Read .agentwire.yml from a directory.

        Args:
            cwd: Working directory path
            machine_id: Machine ID if remote, None for local

        Returns:
            Parsed YAML dict, or None if not found/invalid
        """

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

            yaml_path = f"{cwd}/.agentwire.yml"
            result = await self._run_subprocess(
                ["ssh", *ssh_base_opts(), "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", ssh_target, f"cat {shlex.quote(yaml_path)}"],
                timeout=5,
            )
            if result and result[0] == 0 and result[1].strip():
                try:
                    return yaml.safe_load(result[1]) or {}
                except Exception:
                    pass
            return None

    async def _write_agentwire_yaml(self, cwd: str, data: dict, machine_id: str | None = None) -> bool:
        """Write .agentwire.yml to a directory.

        Args:
            cwd: Working directory path
            data: YAML data to write
            machine_id: Machine ID if remote, None for local

        Returns:
            True if written successfully, False otherwise
        """

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

            yaml_content = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
            yaml_path = f"{cwd}/.agentwire.yml"
            # Use base64 encoding for safe content transmission (avoids heredoc injection)
            encoded = base64.b64encode(yaml_content.encode()).decode()
            cmd = f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(yaml_path)}"
            result = await self._run_subprocess(
                ["ssh", *ssh_base_opts(), "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", ssh_target, cmd],
                timeout=5,
            )
            if result is None:
                logger.warning("Failed to write remote yaml: ssh timed out or failed to spawn")
                return False
            return result[0] == 0

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
            sessions = await self._list_local_sessions()
            for machine_sessions in (await self._list_remote_sessions()).values():
                sessions.extend(machine_sessions)

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
        # Skip the whole tick when nobody is watching (#627): no dashboard WS,
        # no session-window WS, and no recent HTTP session-list poll (mobile).
        has_clients = (
            bool(self.dashboard_clients)
            or any(s.clients for s in self.active_sessions.values())
            or time.monotonic() - self._last_sessions_poll < 15
        )
        if not has_clients:
            return

        # Get list of all sessions (local and remote), in-process + TTL-cached
        # (shared with the HTTP routes — no CLI subprocess per tick, #627).
        session_names = []
        for s in await self._list_local_sessions():
            if s.get("name"):
                session_names.append(s["name"])

        # Remote sessions (names already include @machine suffix). Machines
        # absent from the dict were unreachable this round.
        remote_by_machine = await self._list_remote_sessions()
        for machine_sessions in remote_by_machine.values():
            for s in machine_sessions:
                if s.get("name"):
                    session_names.append(s["name"])

        # Poll each session
        for session_name in session_names:
            try:
                # Capture output in-process, shared with window streams (#628).
                current_output = await self._capture_session_output(session_name)

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
                self._capture_cache.pop(name, None)
                removed_sessions.append(name)

        # Reconcile active_sessions against tmux ground truth (#629): evict
        # Session objects whose tmux session vanished outside the portal
        # (tmux kill, crash, worker auto-reap), closing their websockets.
        await self._reconcile_active_sessions(current_names, remote_by_machine)

        # Notify dashboard about removed sessions
        if removed_sessions:
            for name in removed_sessions:
                logger.info(f"[Monitor] Session '{name}' no longer exists, notifying dashboard")
                await self.broadcast_dashboard("session_closed", {"session": name})
            # Send updated sessions list
            sessions_data = await self._get_sessions_data()
            await self.broadcast_dashboard("sessions_update", {"sessions": sessions_data})

    # Newly-created Session objects get this long before eviction applies —
    # a session the portal just spawned may not be in tmux's list yet.
    RECONCILE_GRACE_SECONDS = 10.0

    async def _reconcile_active_sessions(
        self, live_names: set[str], remote_by_machine: dict[str, list]
    ) -> None:
        """Evict active_sessions entries whose tmux session is gone (#629).

        Guards:
        - "dashboard" is a pseudo-session (never a tmux session) — skip.
        - Remote names (``name@machine``) are only evicted when their machine
          answered the SSH listing this round; an unreachable machine must
          not read as "all its sessions died".
        - A grace window covers just-created sessions racing the listing.
        """
        now = time.monotonic()
        for name in list(self.active_sessions.keys()):
            if name == "dashboard" or name in live_names:
                continue
            session = self.active_sessions.get(name)
            if session is None or now - session.created_at < self.RECONCILE_GRACE_SECONDS:
                continue
            if "@" in name:
                machine_id = name.rsplit("@", 1)[1]
                if machine_id not in remote_by_machine:
                    continue  # machine unreachable — no verdict on the session
            logger.info(f"[Monitor] Evicting stale session object '{name}' (tmux session gone)")
            self.active_sessions.pop(name, None)
            self._capture_cache.pop(name, None)
            if session.output_task and not session.output_task.done():
                session.output_task.cancel()
            for ws in list(session.clients):
                try:
                    await ws.close()
                except Exception:
                    pass
            session.clients.clear()

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
        session = await self._get_or_create_session(name)
        client_id = str(id(ws))
        session.clients.add(ws)
        logger.info(f"[{name}] Client connected (total: {len(session.clients)})")

        # Skip tmux polling for special sessions that aren't real tmux sessions
        is_real_session = name != "dashboard"

        # Send current output immediately on connect (if this is a real session)
        if is_real_session:
            try:
                # Fresh capture on connect (max_age=0) — a cached frame could
                # miss output produced while no client was attached.
                output = await self._capture_session_output(name, max_age=0)
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
            session.hidden_clients.discard(ws)
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
        session = await self._get_or_create_session(session_name)
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

        elif msg_type == "visibility":
            # Client tab visibility — when every client of a session is hidden,
            # _poll_output backs its cadence off (#628).
            if data.get("visible"):
                session.hidden_clients.discard(ws)
            else:
                session.hidden_clients.add(ws)

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
                # Shared capture — dedupes with the monitor tick (#628).
                output = await self._capture_session_output(session.name)
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

            # Back off when every connected client reports a hidden tab (#628).
            all_hidden = bool(session.clients) and all(
                c in session.hidden_clients for c in session.clients
            )
            await asyncio.sleep(2.0 if all_hidden else 0.5)

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

    async def _fetch_remote_machine_sessions(self, machine: dict) -> dict:
        """Fetch sessions for a specific remote machine. Used by CachedStatusChecker."""
        try:
            # Shared TTL-cached remote listing (#627) — one SSH sweep serves
            # the monitor loop and every machine's status check.
            machine_id = machine.get("id")
            remote_by_machine = await self._list_remote_sessions()
            if machine_id not in remote_by_machine:
                return {"status": "offline", "sessions": []}

            sessions = remote_by_machine[machine_id]
            # Add activity status to each session
            for s in sessions:
                s["activity"] = self._get_global_session_activity(s.get("name", ""))

            return {
                "status": "online" if sessions else "online",  # Online but might have no sessions
                "sessions": sessions
            }
        except Exception:
            return {"status": "offline", "sessions": []}

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

            # Read image data, aborting with 413 before buffering an
            # over-limit body into RAM.
            max_bytes = self.config.uploads.max_size_mb * 1024 * 1024
            image_data = await read_multipart_field_limited(image_field, max_bytes)

            if not image_data:
                return web.json_response({"error": "Empty image data"})

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

        except web.HTTPException:
            raise
        except Exception as e:
            logger.error(f"Upload failed: {e}")
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
        session = await self._get_or_create_session(session_name)

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


def apply_cli_overrides(config, overrides: dict) -> None:
    """Apply portal CLI flag overrides onto the loaded config.

    ``--no-tts`` / ``--no-stt`` both flip the backend to ``"none"`` — the
    backend field is what gates the default-tier shim autostarts in
    ``run_server``, so nulling only ``stt.url`` would leave the Moonshine
    shim spawning anyway.
    """
    if overrides.get("port"):
        config.server.port = overrides["port"]
    if overrides.get("host"):
        config.server.host = overrides["host"]
    if overrides.get("no_tts"):
        config.tts.backend = "none"
    if overrides.get("no_stt"):
        config.stt.backend = "none"
        config.stt.url = None


def main(config_path: str | None = None, **overrides) -> None:
    """Entry point for running the server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(config_path)
    apply_cli_overrides(config, overrides)

    try:
        asyncio.run(run_server(config))
    except KeyboardInterrupt:
        logger.info("Server stopped")


if __name__ == "__main__":
    main()
