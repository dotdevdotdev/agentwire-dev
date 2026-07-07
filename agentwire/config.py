"""
AgentWire configuration management.

Loads config from YAML file with sensible defaults and env var overrides.
"""

import os
import sys
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Optional

import yaml


def _get_default_agent_command() -> str:
    """Get the default agent command.

    Returns:
        Default command string for Claude Code.
    """
    return "claude --dangerously-skip-permissions"


def _expand_path(path: str | Path | None) -> Path | None:
    """Expand ~ and resolve path."""
    if path is None:
        return None
    return Path(path).expanduser().resolve()


@dataclass
class SSLConfig:
    """SSL certificate configuration."""

    cert: Path | None = None
    key: Path | None = None

    def __post_init__(self):
        self.cert = _expand_path(self.cert)
        self.key = _expand_path(self.key)

    @property
    def enabled(self) -> bool:
        """SSL is enabled if both cert and key exist."""
        return (
            self.cert is not None
            and self.key is not None
            and self.cert.exists()
            and self.key.exists()
        )


@dataclass
class ServerConfig:
    """WebSocket server configuration."""

    # Local-only by default. Opt into LAN exposure explicitly via
    # `server.host: 0.0.0.0` in config — non-loopback binds require an auth
    # token (auto-generated at ~/.agentwire/portal.token; see SECURITY.md).
    host: str = "127.0.0.1"
    port: int = 8765
    ssl: SSLConfig = field(default_factory=SSLConfig)
    activity_threshold_seconds: float = 3.0  # Time in seconds before session is considered idle
    # Exact origins (scheme://host[:port]) allowed on cross-origin browser
    # requests, e.g. a Cloudflare Tunnel domain. The portal's own origin and
    # localhost equivalents are always allowed.
    allowed_origins: list = field(default_factory=list)
    # None = use ~/.agentwire/portal.token (auto-generated). "" = auth
    # disabled (loopback binds only). Any other string = explicit override.
    auth_token: Optional[str] = None
    # Acknowledge a plaintext (non-TLS) bind on a network-reachable host: the
    # bearer token then transits in cleartext. Required to start a non-loopback
    # bind without TLS — the BYO-tunnel case where the tunnel terminates TLS.
    allow_insecure: bool = False
    # Emit HTTP Strict-Transport-Security. Leave OFF for self-signed /
    # localhost / LAN — HSTS + a self-signed cert locks the browser out of the
    # portal if the cert ever rotates. Enable ONLY when serving a real
    # (CA-signed) cert directly. Behind Cloudflare Tunnel / a reverse proxy,
    # set HSTS at the edge instead.
    hsts: bool = False


@dataclass
class WorktreesConfig:
    """Git worktrees configuration for `project/branch` sessions.

    Governs the legacy ``project/branch`` session layout under
    ``~/projects/<project>-worktrees/``. Still ALIVE (#703): the scheduler's
    worktree+PR dispatch creates its sessions via
    ``agentwire new -s <project>/<branch>``, and ``copy_files`` seeds every
    fresh worktree (including scheduler dispatches). Standalone
    ``agentwire worktree`` sessions use :class:`WorktreeConfig` instead
    (``~/worktrees/<project>/<name>/``).
    """

    enabled: bool = True
    suffix: str = "-worktrees"
    auto_create_branch: bool = True
    # Gitignored files that `git worktree add` won't carry over (it only
    # checks out tracked files). These are copied from the main repo into
    # each fresh worktree so agents have the secrets/config they need.
    # .agentwire.yml/.agentwire.tasks.yml are included because worktree-dispatched
    # tasks can't load their session/task config without them when the project
    # gitignores them (the latter is the protected, host-authored task-exec file —
    # #720).
    copy_files: list = field(default_factory=lambda: [".env", ".agentwire.yml", ".agentwire.tasks.yml"])


@dataclass
class ProjectsConfig:
    """Projects directory configuration."""

    dir: Path = field(default_factory=lambda: Path.home() / "projects")
    worktrees: WorktreesConfig = field(default_factory=WorktreesConfig)
    extra: list = field(default_factory=list)

    def __post_init__(self):
        self.dir = _expand_path(self.dir) or Path.home() / "projects"


@dataclass
class WorktreeConfig:
    """Standalone worktree session orchestration (`worktree:` config key).

    Governs `agentwire worktree <name>` — the first-class primitive for
    spawning an isolated branch+worktree+session for one unit of work.

    Distinct from ``projects.worktrees`` (:class:`WorktreesConfig`), which
    governs the legacy ``project/branch`` session layout under
    ``~/projects/<project>-worktrees/``. This one drives the dedicated
    ``agentwire worktree`` command and its branch↔session registry.

    Fields:
        default_base: Base branch new worktrees fork from. ``None`` means
            "ask the repo" — resolved from ``origin/HEAD`` (falling back to
            the current branch). ``--base`` always overrides.
        default_project: Repo path used when ``--project`` is omitted and
            cwd is not inside a git repo. ``None`` → infer from cwd's git
            root, else cwd.
        worktree_dir: Root under which worktrees are created, nested per
            project — ``<worktree_dir>/<project>/<name>/`` — mirroring
            ``~/projects/<project>/``.
        naming: Optional branch-name template applied in default (new-branch)
            mode, e.g. ``"{user}/{slug}"`` or ``"feature-{slug}"``.
            Placeholders: ``{name}`` (raw), ``{slug}`` (slugified),
            ``{user}`` (OS user). ``None`` → branch == name verbatim.
    """

    default_base: str | None = None
    default_project: str | None = None
    worktree_dir: Path = field(default_factory=lambda: Path.home() / "worktrees")
    naming: str | None = None

    def __post_init__(self):
        self.worktree_dir = _expand_path(self.worktree_dir) or Path.home() / "worktrees"
        if self.default_project:
            self.default_project = str(_expand_path(self.default_project))


_VOICE_BACKENDS = ("default", "custom")

_VOICE_BACKEND_MIGRATION_HINT = (
    "Use 'default' (browser speech in the portal + OS voice fallback, zero setup) "
    "or 'custom' with url: pointing at your shim. "
    "See docs/wiki/voice/shim-contract.md."
)


def _validate_voice_backend(
    kind: str, backend: str, url: str | None, allowed: tuple = _VOICE_BACKENDS
) -> None:
    """Shared tier validation for STT/TTS config sections."""
    if backend not in allowed:
        raise ValueError(
            f"{kind}.backend '{backend}' is not valid (allowed: {', '.join(allowed)}). "
            f"{_VOICE_BACKEND_MIGRATION_HINT}"
        )
    if backend == "custom" and not url:
        raise ValueError(
            f"{kind}.backend 'custom' requires {kind}.url pointing at your shim. "
            f"{_VOICE_BACKEND_MIGRATION_HINT}"
        )


@dataclass
class TTSConfig:
    """Text-to-speech configuration.

    Two tiers: ``default`` speaks via Kokoro running in the portal-managed shim
    **subprocess** (tmux ``agentwire-kokoro``, default ``:8102``) — the portal
    ensures it's running on startup and talks to it over HTTP, with process
    isolation keeping the ~200 MB download + ONNX warm-up off the event loop
    (#398, mirroring the STT shim). Browser speechSynthesis (and an OS-voice
    fallback when no browser is connected) covers speech while the model loads
    or when Kokoro can't load (py3.14+). For this tier ``url`` is optional and
    resolves to ``http://localhost:8102``; if an operator overrides the shim
    port (``KOKORO_PORT``) or ``url``, the two must agree. ``custom`` POSTs to
    any HTTP shim implementing the contract (docs/wiki/voice/shim-contract.md).
    """

    backend: str = "default"  # default | custom
    url: str | None = None  # shim URL (required for custom; optional override for default :8102)
    default_voice: str = "default"
    voices_dir: Path = field(default_factory=lambda: Path.home() / ".agentwire" / "voices")
    # Session-default knobs; sent to custom shims inside `options`
    exaggeration: float = 0.5
    cfg_weight: float = 0.5
    # Capability pass-through envelope — opaque to agentwire, verbatim to the shim
    instructions: str = ""
    options: dict = field(default_factory=dict)
    timeout: int = 60

    def __post_init__(self):
        _validate_voice_backend("tts", self.backend, self.url)
        self.voices_dir = _expand_path(self.voices_dir) or Path.home() / ".agentwire" / "voices"


@dataclass
class STTConfig:
    """Speech-to-text configuration.

    Three tiers: ``default`` transcribes on the host via Moonshine running in
    the portal-managed shim **subprocess** (tmux ``agentwire-stt``, default
    ``:8101``) — the portal ensures it's running on startup and talks to it
    over HTTP, with process isolation keeping the ~19s ONNX warm-up off the
    event loop. Browser SpeechRecognition is the fallback while the model
    loads or when Moonshine can't load (py3.14+). For this tier ``url`` is
    optional and resolves to ``http://localhost:8101``; if an operator
    overrides the shim port (``STT_PORT``) or ``url``, the two must agree.
    ``cloud`` uploads audio to the portal, which POSTs it to any
    OpenAI-compatible transcription API (no extra daemon, key from env,
    server-side only); ``custom`` uploads audio to any HTTP shim implementing
    the contract (docs/wiki/voice/shim-contract.md).
    """

    backend: str = "default"  # TIER: default | cloud | custom (portal/host selector)
    # ENGINE: auto | moonshine | whisper — which model the self-hosted STT
    # shim loads (engine.py). Orthogonal to `backend`: the tier picks *where*
    # transcription happens, the engine picks *which model* the custom shim runs.
    engine: str = "auto"
    url: str | None = None  # shim URL (required for custom backend)
    timeout: int = 30
    # cloud tier settings — any provider speaking the OpenAI transcription
    # protocol (OpenAI, Groq, Mistral, self-hosted speaches, ...):
    #   base_url:    API root (default https://api.openai.com/v1)
    #   model:       transcription model (default gpt-4o-mini-transcribe)
    #   api_key_env: NAME of the env var holding the key (default
    #                OPENAI_API_KEY) — the key itself never lives in config
    #   language:    optional ISO-639-1 hint, omitted from the request if empty
    cloud: dict = field(default_factory=dict)
    # Milliseconds of silence to prepend to the decoded audio before sending
    # to the STT shim. Default 0 — moonshine doesn't need it. Set to ~300
    # if your shim (e.g. older faster-whisper builds) clips the first syllable.
    silence_prepend_ms: int = 0
    # User extensions to the portal's jargon-correction map (spoken → written)
    corrections: dict = field(default_factory=dict)
    # Capability pass-through envelope — opaque to agentwire, verbatim to the shim
    instructions: str = ""
    options: dict = field(default_factory=dict)

    def __post_init__(self):
        _validate_voice_backend(
            "stt", self.backend, self.url, allowed=("default", "cloud", "custom")
        )
        allowed_engines = ("auto", "moonshine", "whisper")
        if self.engine not in allowed_engines:
            raise ValueError(
                f"stt.engine must be one of {allowed_engines}, got '{self.engine}'"
            )


@dataclass
class AgentConfig:
    """Agent command configuration."""

    command: str = field(default_factory=_get_default_agent_command)


@dataclass
class MachinesConfig:
    """Remote machines registry configuration."""

    file: Path = field(
        default_factory=lambda: Path.home() / ".agentwire" / "machines.json"
    )

    def __post_init__(self):
        self.file = _expand_path(self.file) or self.file


@dataclass
class UploadsConfig:
    """Uploads directory for images shared across machines."""

    dir: Path = field(
        default_factory=lambda: Path.home() / ".agentwire" / "uploads"
    )
    max_size_mb: int = 10
    cleanup_days: int = 7

    def __post_init__(self):
        self.dir = _expand_path(self.dir) or self.dir


@dataclass
class ArtifactsConfig:
    """Artifacts directory for agent-generated HTML content."""

    dir: Path = field(
        default_factory=lambda: Path.home() / ".agentwire" / "artifacts"
    )
    max_size_mb: int = 10

    def __post_init__(self):
        self.dir = _expand_path(self.dir) or self.dir


@dataclass
class PortalConfig:
    """Portal connection settings (for remote machines)."""

    url: str = "http://localhost:8765"  # URL to reach the portal (https when SSL certs exist)


@dataclass
class ServiceConfig:
    """Configuration for a single service location."""

    machine: Optional[str] = None  # None = local, or machine ID from machines.json
    port: int = 8765
    health_endpoint: str = "/health"
    scheme: str = "http"  # http or https


@dataclass
class HealthcheckConfig:
    """How a custom service's health is probed.

    kind:
      tmux_session (default) — healthy when the tmux session exists
      http                   — healthy when GET `url` returns 2xx
      command                — healthy when `command` exits 0
    """

    kind: str = "tmux_session"
    url: Optional[str] = None       # for kind: http
    command: Optional[str] = None   # for kind: command
    interval: int = 60              # seconds between watchdog checks


@dataclass
class CustomServiceConfig:
    """A user-defined session that behaves like a service.

    Custom services show up in the portal's Services column, are booted by
    `agentwire up` AND on portal launch, and are watched by the portal's
    service watchdog. A service is just an agentwire session created in a
    project directory; `roles`/`posture` override the project's .agentwire.yml
    when set.

    restart: never | on-failure | always — what the watchdog does when the
    healthcheck fails ("always" behaves like "on-failure" for tmux services;
    manual `agentwire services down` always sticks regardless of policy).
    """

    name: str
    project: Optional[str] = None
    autostart: bool = True
    roles: Optional[str] = None  # comma-separated; overrides project .agentwire.yml
    posture: Optional[str] = None   # posture override (e.g. bypass, auto)
    restart: str = "on-failure"  # never | on-failure | always
    healthcheck: HealthcheckConfig = field(default_factory=HealthcheckConfig)
    # Context auto-management policy (issue #442): clear | compact | none.
    # Default "none" — a service is only auto-managed when it opts in. Stateless
    # bridges (notifications) set "clear"; stateful orchestrators "compact".
    context_policy: str = "none"

    def __post_init__(self):
        if self.project:
            self.project = str(_expand_path(self.project) or self.project)


@dataclass
class ServicesConfig:
    """Where each service runs in the network."""

    portal: ServiceConfig = field(default_factory=lambda: ServiceConfig(port=8765))
    tts: ServiceConfig = field(default_factory=lambda: ServiceConfig(port=8100, scheme="http"))
    custom: list = field(default_factory=list)  # list[CustomServiceConfig]


@dataclass
class ReplConfig:
    """Agentwire REPL (Textual TUI) configuration.

    `theme` is a dict of color overrides keyed by Textual theme attribute
    (primary, secondary, accent, foreground, background, surface, panel,
    success, warning, error). Each value is any color string Textual
    accepts (#RRGGBB, named colors, etc.). Empty/missing keys fall back
    to the brand defaults defined in `agentwire/repl/textual_app.py`.

    Example `~/.agentwire/config.yaml`:

        repl:
          theme:
            primary: "#ff00aa"        # override neon-green primary
            background: "#0d0d2a"     # tweak the flat-black background
    """

    theme: dict[str, str] = field(default_factory=dict)


@dataclass
class SessionConfig:
    """Default session configuration.

    Note: there is intentionally NO global default-role here. A session's
    intrinsic etiquette is derived from its spawn verb (see
    ``roles.resolve_roles``), never from a config default — that scatter is
    the bug this replaced.
    """

    inject_soul: bool = True  # Append the bundled soul personality role to human-facing sessions


@dataclass
class SchedulerConfig:
    """Scheduler daemon configuration."""

    autostart: bool = True         # start the daemon when the portal boots
    board_file: Path = field(default_factory=lambda: Path.home() / ".agentwire" / "scheduler.yaml")
    # Run-state lives in its OWN file — the daemon NEVER rewrites board_file
    # (which holds user-authored task definitions). See #449.
    state_file: Path = field(default_factory=lambda: Path.home() / ".agentwire" / "scheduler-state.yaml")
    state_backups: int = 5         # rotated good copies of the state file kept for recovery
    events_file: Path = field(default_factory=lambda: Path.home() / ".agentwire" / "scheduler-events.jsonl")
    live_state_file: Path = field(default_factory=lambda: Path.home() / ".agentwire" / "scheduler-live.json")
    git_timeout: int = 10          # git rev-parse, diff, status
    git_op_timeout: int = 15       # git commit, kill-session
    gate_timeout: int = 10         # custom gate command
    portal_notify_timeout: int = 5
    session_create_timeout: int = 30
    max_loop_sleep: int = 60
    dispatch_cooldown: int = 60
    # Watchdog ceiling for a single dispatch (seconds). A hung `ensure`
    # (e.g. an unrecognized usage-limit dialog) otherwise holds the single
    # dispatch slot forever and starves the whole board (#677). 0 disables.
    dispatch_max_runtime: int = 14400
    # Crash-loop guard: a board that won't parse / has no tasks backs off
    # exponentially from `error_backoff_base` up to `error_backoff_max`
    # instead of tight-looping (#449).
    error_backoff_base: int = 30
    error_backoff_max: int = 1800

    def __post_init__(self):
        self.board_file = _expand_path(self.board_file) or self.board_file
        self.state_file = _expand_path(self.state_file) or self.state_file
        self.events_file = _expand_path(self.events_file) or self.events_file
        self.live_state_file = _expand_path(self.live_state_file) or self.live_state_file


@dataclass
class UsageLimitConfig:
    """Usage-limit recovery (watchdog) knobs.

    Gates NEW parks only — already-parked sessions are always resumed,
    regardless of these settings.
    """

    enabled: bool = True  # master switch for dialog detection/parking
    exclude_sessions: list[str] = field(default_factory=list)  # never auto-park these


@dataclass
class PromptRouterConfig:
    """Interactive-prompt routing (worker prompts → parent session) knobs."""

    enabled: bool = True  # master switch for the sweep + hook-path routing
    exclude_sessions: list[str] = field(default_factory=list)  # never route these


@dataclass
class SessionContextConfig:
    """Session context-bloat observability + auto-management (issue #442) knobs.

    Phase 0 surfaces each session's remaining-context % and flags low sessions.
    Phase 1 adds opt-in auto-action: a session carrying a ``context_policy`` of
    ``clear``/``compact`` (default ``none``) gets that command sent when it
    crosses the warn threshold while idle at an empty prompt.
    """

    # Flag a session when its REMAINING context drops to/below this %
    # (the bar shows headroom, not usage — see session_context.py). Default
    # 20% remaining == ~80% of the way toward the limit.
    warn_remaining_pct: int = 20

    # Master switch for the Phase-1 auto-action sweep on the limits watchdog.
    # Observability (CLI/MCP) is unaffected by this — it only gates auto-action.
    auto_enabled: bool = True

    # Per-session policy overrides keyed by session name — ``clear`` | ``compact``
    # | ``none``. For arbitrary sessions (councils, anchors) that aren't service-
    # registry entries. Bundled services are default-on via their own entry, so
    # they need no entry here. Anything not listed (and not a default-on service)
    # is ``none`` — never auto-managed.
    policies: dict = field(default_factory=dict)


@dataclass
class Config:
    """Root configuration for AgentWire."""

    server: ServerConfig = field(default_factory=ServerConfig)
    projects: ProjectsConfig = field(default_factory=ProjectsConfig)
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    machines: MachinesConfig = field(default_factory=MachinesConfig)
    uploads: UploadsConfig = field(default_factory=UploadsConfig)
    artifacts: ArtifactsConfig = field(default_factory=ArtifactsConfig)
    portal: PortalConfig = field(default_factory=PortalConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    repl: ReplConfig = field(default_factory=ReplConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    usage_limit: UsageLimitConfig = field(default_factory=UsageLimitConfig)
    prompt_router: PromptRouterConfig = field(default_factory=PromptRouterConfig)
    session_context: SessionContextConfig = field(default_factory=SessionContextConfig)
    channels: dict = field(default_factory=dict)


def _merge_dict(base: dict, override: dict) -> dict:
    """Deep merge override into base dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_overrides(data: dict) -> dict:
    """Apply environment variable overrides.

    Env vars use AGENTWIRE_ prefix with double underscore for nesting.
    Example: AGENTWIRE_SERVER__PORT=9000
    """
    prefix = "AGENTWIRE_"

    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue

        # Parse key: AGENTWIRE_SERVER__PORT -> ["server", "port"]
        parts = key[len(prefix) :].lower().split("__")

        # Navigate to the right place in the dict
        current = data
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]

        # Set the value (try to parse as int/bool/float)
        final_key = parts[-1]
        current[final_key] = _parse_env_value(value)

    return data


def _parse_env_value(value: str) -> str | int | float | bool:
    """Parse environment variable value to appropriate type."""
    # Boolean
    if value.lower() in ("true", "yes", "1"):
        return True
    if value.lower() in ("false", "no", "0"):
        return False

    # Integer
    try:
        return int(value)
    except ValueError:
        pass

    # Float
    try:
        return float(value)
    except ValueError:
        pass

    return value


def _dict_to_config(data: dict) -> Config:
    """Convert nested dict to Config dataclass."""
    # Server
    server_data = data.get("server", {})
    ssl_data = server_data.get("ssl", {})
    ssl = SSLConfig(
        cert=ssl_data.get("cert"),
        key=ssl_data.get("key"),
    )
    server = ServerConfig(
        host=server_data.get("host", "127.0.0.1"),
        port=server_data.get("port", 8765),
        ssl=ssl,
        activity_threshold_seconds=server_data.get("activity_threshold_seconds", 3.0),
        allowed_origins=server_data.get("allowed_origins") or [],
        # "" (explicit disable) must survive — don't collapse it to None.
        auth_token=server_data.get("auth_token"),
        allow_insecure=bool(server_data.get("allow_insecure", False)),
        hsts=bool(server_data.get("hsts", False)),
    )

    # Projects
    projects_data = data.get("projects", {})
    worktrees_data = projects_data.get("worktrees", {})
    worktrees = WorktreesConfig(
        enabled=worktrees_data.get("enabled", True),
        suffix=worktrees_data.get("suffix", "-worktrees"),
        auto_create_branch=worktrees_data.get("auto_create_branch", True),
        copy_files=worktrees_data.get("copy_files", [".env", ".agentwire.yml", ".agentwire.tasks.yml"]),
    )
    projects = ProjectsConfig(
        dir=projects_data.get("dir", "~/projects"),
        worktrees=worktrees,
        extra=projects_data.get("extra", []),
    )

    # Worktree-session orchestration (`worktree:` key).
    worktree_data = data.get("worktree", {}) or {}
    worktree = WorktreeConfig(
        default_base=worktree_data.get("default_base"),
        default_project=worktree_data.get("default_project"),
        worktree_dir=worktree_data.get("worktree_dir", "~/worktrees"),
        naming=worktree_data.get("naming"),
    )

    # TTS
    tts_data = data.get("tts", {})
    tts = TTSConfig(
        backend=tts_data.get("backend", "default"),
        url=tts_data.get("url"),
        default_voice=tts_data.get("default_voice", "default"),
        voices_dir=tts_data.get("voices_dir", Path.home() / ".agentwire" / "voices"),
        exaggeration=tts_data.get("exaggeration", 0.5),
        cfg_weight=tts_data.get("cfg_weight", 0.5),
        instructions=tts_data.get("instructions", ""),
        options=tts_data.get("options") or {},
        timeout=tts_data.get("timeout", 60),
    )

    # STT
    stt_data = data.get("stt", {})
    stt = STTConfig(
        backend=stt_data.get("backend", "default"),
        engine=stt_data.get("engine", "auto"),
        url=stt_data.get("url"),
        timeout=stt_data.get("timeout", 30),
        cloud=stt_data.get("cloud") or {},
        silence_prepend_ms=int(stt_data.get("silence_prepend_ms", 0)),
        corrections=stt_data.get("corrections") or {},
        instructions=stt_data.get("instructions", ""),
        options=stt_data.get("options") or {},
    )

    # Agent
    agent_data = data.get("agent", {})
    agent = AgentConfig(
        command=agent_data.get("command", _get_default_agent_command()),
    )

    # Machines
    machines_data = data.get("machines", {})
    machines = MachinesConfig(
        file=machines_data.get("file", "~/.agentwire/machines.json"),
    )

    # Uploads
    uploads_data = data.get("uploads", {})
    uploads = UploadsConfig(
        dir=uploads_data.get("dir", "~/.agentwire/uploads"),
        max_size_mb=uploads_data.get("max_size_mb", 10),
        cleanup_days=uploads_data.get("cleanup_days", 7),
    )

    # Artifacts
    artifacts_data = data.get("artifacts", {})
    artifacts = ArtifactsConfig(
        dir=artifacts_data.get("dir", "~/.agentwire/artifacts"),
        max_size_mb=artifacts_data.get("max_size_mb", 10),
    )

    # Portal — scheme follows SSL state unless explicitly configured, so a
    # fresh no-cert install gets working http URLs out of the box
    portal_scheme = "https" if ssl.enabled else "http"
    portal_data = data.get("portal", {})
    portal = PortalConfig(
        url=portal_data.get("url", f"{portal_scheme}://localhost:8765"),
    )

    # Services (network service locations)
    services_data = data.get("services", {})
    portal_service_data = services_data.get("portal", {})
    tts_service_data = services_data.get("tts", {})
    portal_service = ServiceConfig(
        machine=portal_service_data.get("machine"),
        port=portal_service_data.get("port", 8765),
        health_endpoint=portal_service_data.get("health_endpoint", "/health"),
        scheme=portal_service_data.get("scheme", portal_scheme),
    )
    tts_service = ServiceConfig(
        machine=tts_service_data.get("machine"),
        port=tts_service_data.get("port", 8100),
        health_endpoint=tts_service_data.get("health_endpoint", "/health"),
        scheme=tts_service_data.get("scheme", "http"),  # TTS defaults to HTTP
    )
    custom_services = []
    for entry in services_data.get("custom", []) or []:
        if isinstance(entry, str):
            custom_services.append(CustomServiceConfig(name=entry))
        elif isinstance(entry, dict) and entry.get("name"):
            hc_data = entry.get("healthcheck") or {}
            healthcheck = HealthcheckConfig(
                kind=hc_data.get("kind", "tmux_session"),
                url=hc_data.get("url"),
                command=hc_data.get("command"),
                interval=hc_data.get("interval", 60),
            )
            context_policy = entry.get("context_policy", "none")
            if context_policy not in ("clear", "compact", "none"):
                context_policy = "none"
            custom_services.append(CustomServiceConfig(
                name=entry["name"],
                project=entry.get("project"),
                autostart=entry.get("autostart", True),
                roles=entry.get("roles"),
                posture=entry.get("posture"),
                restart=entry.get("restart", "on-failure"),
                healthcheck=healthcheck,
                context_policy=context_policy,
            ))
    services = ServicesConfig(
        portal=portal_service,
        tts=tts_service,
        custom=custom_services,
    )

    # Channel configs (registry-driven)
    channel_configs = {}
    try:
        from agentwire.channels import ChannelRegistry

        for name, channel_cls in ChannelRegistry._channels.items():
            if channel_cls.config_class:
                resolved = ChannelRegistry.resolve_config(name, data)
                # Only pass fields the dataclass accepts. Secrets (api_key)
                # are env-only — ~/.agentwire/.env — so a stale key in yaml
                # gets a warning, not a config-load crash.
                allowed = {
                    f.name for f in dataclass_fields(channel_cls.config_class) if f.init
                }
                unknown = sorted(set(resolved) - allowed)
                if unknown:
                    print(
                        f"Warning: ignoring channels.{name} field(s) "
                        f"{', '.join(unknown)} — secrets belong in "
                        f"~/.agentwire/.env, never config.yaml "
                        f"(docs/wiki/security/secrets.md)",
                        file=sys.stderr,
                    )
                kwargs = {k: v for k, v in resolved.items() if k in allowed}
                channel_configs[name] = channel_cls.config_class(**kwargs)
    except ImportError:
        pass

    # Scheduler
    scheduler_data = data.get("scheduler", {})
    scheduler = SchedulerConfig(
        autostart=scheduler_data.get("autostart", True),
        board_file=scheduler_data.get("board_file", "~/.agentwire/scheduler.yaml"),
        state_file=scheduler_data.get("state_file", "~/.agentwire/scheduler-state.yaml"),
        state_backups=scheduler_data.get("state_backups", 5),
        events_file=scheduler_data.get("events_file", "~/.agentwire/scheduler-events.jsonl"),
        live_state_file=scheduler_data.get("live_state_file", "~/.agentwire/scheduler-live.json"),
        git_timeout=scheduler_data.get("git_timeout", 10),
        git_op_timeout=scheduler_data.get("git_op_timeout", 15),
        gate_timeout=scheduler_data.get("gate_timeout", 10),
        portal_notify_timeout=scheduler_data.get("portal_notify_timeout", 5),
        session_create_timeout=scheduler_data.get("session_create_timeout", 30),
        max_loop_sleep=scheduler_data.get("max_loop_sleep", 60),
        dispatch_cooldown=scheduler_data.get("dispatch_cooldown", 60),
        error_backoff_base=scheduler_data.get("error_backoff_base", 30),
        error_backoff_max=scheduler_data.get("error_backoff_max", 1800),
    )

    # REPL (Textual TUI) — theme overrides
    repl_data = data.get("repl", {}) or {}
    theme_overrides = repl_data.get("theme", {}) or {}
    if not isinstance(theme_overrides, dict):
        theme_overrides = {}
    repl = ReplConfig(theme={str(k): str(v) for k, v in theme_overrides.items()})

    # Usage-limit recovery (watchdog enable + session exclusions)
    usage_limit_data = data.get("usage_limit", {}) or {}
    if not isinstance(usage_limit_data, dict):
        usage_limit_data = {}
    exclude_sessions_raw = usage_limit_data.get("exclude_sessions", []) or []
    if not isinstance(exclude_sessions_raw, list):
        exclude_sessions_raw = []
    usage_limit = UsageLimitConfig(
        enabled=bool(usage_limit_data.get("enabled", True)),
        exclude_sessions=[str(s) for s in exclude_sessions_raw if s],
    )

    # Prompt routing (worker prompts → parent session)
    prompt_router_data = data.get("prompt_router", {}) or {}
    if not isinstance(prompt_router_data, dict):
        prompt_router_data = {}
    pr_exclude_raw = prompt_router_data.get("exclude_sessions", []) or []
    if not isinstance(pr_exclude_raw, list):
        pr_exclude_raw = []
    prompt_router = PromptRouterConfig(
        enabled=bool(prompt_router_data.get("enabled", True)),
        exclude_sessions=[str(s) for s in pr_exclude_raw if s],
    )

    # Session context observability + auto-management (issue #442)
    session_context_data = data.get("session_context", {}) or {}
    if not isinstance(session_context_data, dict):
        session_context_data = {}
    try:
        warn_remaining_pct = int(session_context_data.get("warn_remaining_pct", 20))
    except (TypeError, ValueError):
        warn_remaining_pct = 20
    policies_raw = session_context_data.get("policies", {}) or {}
    if not isinstance(policies_raw, dict):
        policies_raw = {}
    policies = {
        str(name): str(pol)
        for name, pol in policies_raw.items()
        if str(pol) in ("clear", "compact", "none")
    }
    session_context = SessionContextConfig(
        warn_remaining_pct=max(0, min(100, warn_remaining_pct)),
        auto_enabled=bool(session_context_data.get("auto_enabled", True)),
        policies=policies,
    )

    # Session defaults
    session_data = data.get("session", {}) or {}
    session = SessionConfig(
        inject_soul=bool(session_data.get("inject_soul", True)),
    )

    return Config(
        server=server,
        session=session,
        projects=projects,
        worktree=worktree,
        tts=tts,
        stt=stt,
        agent=agent,
        machines=machines,
        uploads=uploads,
        artifacts=artifacts,
        portal=portal,
        services=services,
        scheduler=scheduler,
        channels=channel_configs,
        repl=repl,
        usage_limit=usage_limit,
        prompt_router=prompt_router,
        session_context=session_context,
    )


def load_config(config_path: Optional[Path] = None) -> Config:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. Defaults to ~/.agentwire/config.yaml

    Returns:
        Config object with all settings.

    Behavior:
        1. Starts with default values
        2. Merges config file if it exists
        3. Applies environment variable overrides
    """
    if config_path is None:
        config_path = Path.home() / ".agentwire" / "config.yaml"
    else:
        config_path = Path(config_path).expanduser().resolve()

    # Start with empty dict (defaults come from dataclasses)
    data: dict = {}

    # Load from file if it exists
    if config_path.exists():
        with open(config_path) as f:
            file_data = yaml.safe_load(f) or {}
            data = _merge_dict(data, file_data)

    # Apply environment variable overrides
    data = _apply_env_overrides(data)

    # Debug logging for STT config
    import logging
    logger = logging.getLogger(__name__)
    if 'stt' in data:
        logger.info(f"STT config after env overrides: {data['stt']}")

    return _dict_to_config(data)


# Module-level config instance (lazy loaded)
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global config instance (lazy loaded)."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config(config_path: Optional[Path] = None) -> Config:
    """Reload configuration from disk."""
    global _config
    _config = load_config(config_path)
    return _config
