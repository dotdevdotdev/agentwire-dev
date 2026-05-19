"""
AgentWire configuration management.

Loads config from YAML file with sensible defaults and env var overrides.
"""

import os
from dataclasses import dataclass, field
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

    host: str = "0.0.0.0"
    port: int = 8765
    ssl: SSLConfig = field(default_factory=SSLConfig)
    activity_threshold_seconds: float = 3.0  # Time in seconds before session is considered idle


@dataclass
class WorktreesConfig:
    """Git worktrees configuration for parallel sessions."""

    enabled: bool = True
    suffix: str = "-worktrees"
    auto_create_branch: bool = True


@dataclass
class ProjectsConfig:
    """Projects directory configuration."""

    dir: Path = field(default_factory=lambda: Path.home() / "projects")
    worktrees: WorktreesConfig = field(default_factory=WorktreesConfig)
    extra: list = field(default_factory=list)

    def __post_init__(self):
        self.dir = _expand_path(self.dir) or Path.home() / "projects"


@dataclass
class TTSConfig:
    """Text-to-speech configuration."""

    backend: str = "chatterbox"  # chatterbox | runpod | none
    url: str | None = None  # TTS server URL (required for chatterbox backend)
    default_voice: str = "default"
    voices_dir: Path = field(default_factory=lambda: Path.home() / ".agentwire" / "voices")
    # Voice parameters (applies to all backends)
    exaggeration: float = 0.5
    cfg_weight: float = 0.5
    # RunPod serverless configuration
    runpod_endpoint_id: str = ""
    runpod_api_key: str = ""
    runpod_timeout: int = 120

    def __post_init__(self):
        self.voices_dir = _expand_path(self.voices_dir) or Path.home() / ".agentwire" / "voices"


@dataclass
class STTConfig:
    """Speech-to-text configuration.

    STT uses a remote server. Configure url to enable.
    """

    url: str | None = None  # STT server URL (e.g., http://localhost:8100)
    timeout: int = 30
    # Milliseconds of silence to prepend to the decoded audio before sending
    # to the STT backend. Default 0 — moonshine doesn't need it. Set to ~300
    # if your backend (e.g. older faster-whisper builds) clips the first syllable.
    silence_prepend_ms: int = 0


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

    url: str = "https://localhost:8765"  # URL to reach the portal


@dataclass
class ServiceConfig:
    """Configuration for a single service location."""

    machine: Optional[str] = None  # None = local, or machine ID from machines.json
    port: int = 8765
    health_endpoint: str = "/health"
    scheme: str = "http"  # http or https


@dataclass
class ServicesConfig:
    """Where each service runs in the network."""

    portal: ServiceConfig = field(default_factory=lambda: ServiceConfig(port=8765, scheme="https"))
    tts: ServiceConfig = field(default_factory=lambda: ServiceConfig(port=8100, scheme="http"))


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
    """Default session configuration."""

    default_role: str = "agentwire"  # Default role for new sessions


@dataclass
class SchedulerConfig:
    """Scheduler daemon configuration."""

    board_file: Path = field(default_factory=lambda: Path.home() / ".agentwire" / "scheduler.yaml")
    events_file: Path = field(default_factory=lambda: Path.home() / ".agentwire" / "scheduler-events.jsonl")
    live_state_file: Path = field(default_factory=lambda: Path.home() / ".agentwire" / "scheduler-live.json")
    git_timeout: int = 10          # git rev-parse, diff, status
    git_op_timeout: int = 15       # git commit, kill-session
    gate_timeout: int = 10         # custom gate command
    portal_notify_timeout: int = 5
    session_create_timeout: int = 30
    max_loop_sleep: int = 60
    dispatch_cooldown: int = 60

    def __post_init__(self):
        self.board_file = _expand_path(self.board_file) or self.board_file
        self.events_file = _expand_path(self.events_file) or self.events_file
        self.live_state_file = _expand_path(self.live_state_file) or self.live_state_file


DEFAULT_GO_PROMPT = """\
You have been prepared with full context for this task during an interactive \
session. All requirements, clarifications, and decisions are in your \
conversation history above.

Begin autonomous execution now. Commit frequently. Run tests after significant \
changes. When complete, summarize what you accomplished."""


@dataclass
class OvernightConfig:
    """Overnight session queue configuration."""

    window_start: str = "22:00"
    window_end: str = "07:00"
    timezone: str = ""  # Empty = local timezone
    check_interval: int = 60  # Seconds between queue checks
    max_concurrent: int = 1  # Sequential by default
    session_timeout: int = 7200  # 2 hours max per session
    branch_prefix: str = "overnight/"
    pr_draft: bool = True
    session_type: str = "claude-auto"
    session_name: str = "agentwire-overnight"  # Tmux session for daemon
    events_file: Path = field(
        default_factory=lambda: Path.home() / ".agentwire" / "overnight-events.jsonl"
    )
    live_state_file: Path = field(
        default_factory=lambda: Path.home() / ".agentwire" / "overnight-live.json"
    )
    go_prompt: str = DEFAULT_GO_PROMPT

    def __post_init__(self):
        self.events_file = _expand_path(self.events_file) or self.events_file
        self.live_state_file = _expand_path(self.live_state_file) or self.live_state_file


@dataclass
class SafetyConfig:
    """Global damage-control safety knobs."""

    enabled: bool = True
    disabled_rules: list[str] = field(default_factory=list)


@dataclass
class Config:
    """Root configuration for AgentWire."""

    server: ServerConfig = field(default_factory=ServerConfig)
    projects: ProjectsConfig = field(default_factory=ProjectsConfig)
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
    overnight: OvernightConfig = field(default_factory=OvernightConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
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
        host=server_data.get("host", "0.0.0.0"),
        port=server_data.get("port", 8765),
        ssl=ssl,
        activity_threshold_seconds=server_data.get("activity_threshold_seconds", 3.0),
    )

    # Projects
    projects_data = data.get("projects", {})
    worktrees_data = projects_data.get("worktrees", {})
    worktrees = WorktreesConfig(
        enabled=worktrees_data.get("enabled", True),
        suffix=worktrees_data.get("suffix", "-worktrees"),
        auto_create_branch=worktrees_data.get("auto_create_branch", True),
    )
    projects = ProjectsConfig(
        dir=projects_data.get("dir", "~/projects"),
        worktrees=worktrees,
        extra=projects_data.get("extra", []),
    )

    # TTS
    tts_data = data.get("tts", {})
    tts = TTSConfig(
        backend=tts_data.get("backend", "chatterbox"),
        url=tts_data.get("url"),
        default_voice=tts_data.get("default_voice", "default"),
        runpod_endpoint_id=tts_data.get("runpod_endpoint_id", ""),
        runpod_api_key=tts_data.get("runpod_api_key", ""),
        runpod_timeout=tts_data.get("runpod_timeout", 120),
    )

    # STT
    stt_data = data.get("stt", {})
    stt = STTConfig(
        url=stt_data.get("url"),
        timeout=stt_data.get("timeout", 30),
        silence_prepend_ms=int(stt_data.get("silence_prepend_ms", 0)),
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

    # Portal
    portal_data = data.get("portal", {})
    portal = PortalConfig(
        url=portal_data.get("url", "https://localhost:8765"),
    )

    # Services (network service locations)
    services_data = data.get("services", {})
    portal_service_data = services_data.get("portal", {})
    tts_service_data = services_data.get("tts", {})
    portal_service = ServiceConfig(
        machine=portal_service_data.get("machine"),
        port=portal_service_data.get("port", 8765),
        health_endpoint=portal_service_data.get("health_endpoint", "/health"),
        scheme=portal_service_data.get("scheme", "https"),  # Portal defaults to HTTPS
    )
    tts_service = ServiceConfig(
        machine=tts_service_data.get("machine"),
        port=tts_service_data.get("port", 8100),
        health_endpoint=tts_service_data.get("health_endpoint", "/health"),
        scheme=tts_service_data.get("scheme", "http"),  # TTS defaults to HTTP
    )
    services = ServicesConfig(
        portal=portal_service,
        tts=tts_service,
    )

    # Channel configs (registry-driven)
    channel_configs = {}
    try:
        from agentwire.channels import ChannelRegistry

        for name, channel_cls in ChannelRegistry._channels.items():
            if channel_cls.config_class:
                resolved = ChannelRegistry.resolve_config(name, data)
                if resolved:
                    channel_configs[name] = channel_cls.config_class(**resolved)
                else:
                    channel_configs[name] = channel_cls.config_class()
    except ImportError:
        pass

    # Scheduler
    scheduler_data = data.get("scheduler", {})
    scheduler = SchedulerConfig(
        board_file=scheduler_data.get("board_file", "~/.agentwire/scheduler.yaml"),
        events_file=scheduler_data.get("events_file", "~/.agentwire/scheduler-events.jsonl"),
        live_state_file=scheduler_data.get("live_state_file", "~/.agentwire/scheduler-live.json"),
        git_timeout=scheduler_data.get("git_timeout", 10),
        git_op_timeout=scheduler_data.get("git_op_timeout", 15),
        gate_timeout=scheduler_data.get("gate_timeout", 10),
        portal_notify_timeout=scheduler_data.get("portal_notify_timeout", 5),
        session_create_timeout=scheduler_data.get("session_create_timeout", 30),
        max_loop_sleep=scheduler_data.get("max_loop_sleep", 60),
        dispatch_cooldown=scheduler_data.get("dispatch_cooldown", 60),
    )

    # Overnight
    overnight_data = data.get("overnight", {})
    overnight = OvernightConfig(
        window_start=overnight_data.get("window_start", "22:00"),
        window_end=overnight_data.get("window_end", "07:00"),
        timezone=overnight_data.get("timezone", ""),
        check_interval=overnight_data.get("check_interval", 60),
        max_concurrent=overnight_data.get("max_concurrent", 1),
        session_timeout=overnight_data.get("session_timeout", 7200),
        branch_prefix=overnight_data.get("branch_prefix", "overnight/"),
        pr_draft=overnight_data.get("pr_draft", True),
        session_type=overnight_data.get("session_type", "claude-auto"),
        session_name=overnight_data.get("session_name", "agentwire-overnight"),
        events_file=overnight_data.get("events_file", "~/.agentwire/overnight-events.jsonl"),
        live_state_file=overnight_data.get("live_state_file", "~/.agentwire/overnight-live.json"),
        go_prompt=overnight_data.get("go_prompt", DEFAULT_GO_PROMPT),
    )

    # REPL (Textual TUI) — theme overrides
    repl_data = data.get("repl", {}) or {}
    theme_overrides = repl_data.get("theme", {}) or {}
    if not isinstance(theme_overrides, dict):
        theme_overrides = {}
    repl = ReplConfig(theme={str(k): str(v) for k, v in theme_overrides.items()})

    # Safety (damage-control kill switch + disabled rules)
    safety_data = data.get("safety", {}) or {}
    if not isinstance(safety_data, dict):
        safety_data = {}
    disabled_rules_raw = safety_data.get("disabled_rules", []) or []
    if not isinstance(disabled_rules_raw, list):
        disabled_rules_raw = []
    safety = SafetyConfig(
        enabled=bool(safety_data.get("enabled", True)),
        disabled_rules=[str(r) for r in disabled_rules_raw if r],
    )

    return Config(
        server=server,
        projects=projects,
        tts=tts,
        stt=stt,
        agent=agent,
        machines=machines,
        uploads=uploads,
        artifacts=artifacts,
        portal=portal,
        services=services,
        scheduler=scheduler,
        overnight=overnight,
        channels=channel_configs,
        repl=repl,
        safety=safety,
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
