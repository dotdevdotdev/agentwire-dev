"""
Project-level configuration (.agentwire.yml).

This file lives in project directories and is the source of truth for session config.
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml


class SessionType(str, Enum):
    """Session type determines agent execution mode."""
    BARE = "bare"                    # No agent, just tmux session
    CLAUDE_BYPASS = "claude-bypass"  # Claude with --dangerously-skip-permissions
    CLAUDE_AUTO = "claude-auto"      # Claude with auto mode (classifier safety net)
    CLAUDE_PROMPTED = "claude-prompted"  # Claude with permission hooks
    CLAUDE_RESTRICTED = "claude-restricted"  # Claude with only say allowed
    # Pi coding agent session types (`pi-zai`, `pi-deepseek`, `pi-<provider>[-restricted|-readonly]`)
    # are handled dynamically by `_missing_` below — no explicit members needed.
    SDK_BYPASS = "sdk-bypass"          # Agentwire REPL (claude-agent-sdk), bypass permissions
    SDK_PROMPTED = "sdk-prompted"      # Agentwire REPL, ask before each tool call
    SDK_RESTRICTED = "sdk-restricted"  # Agentwire REPL, plan / read-only
    # Universal types (agent-agnostic, map to agent-specific types)
    STANDARD = "standard"  # Full automation -> claude-bypass
    WORKER = "worker"      # Worker pane -> claude-restricted
    VOICE = "voice"        # Voice with prompts -> claude-prompted

    @classmethod
    def _missing_(cls, value: object) -> "SessionType | None":
        """Handle dynamic pi-<provider> types not enumerated at definition time.

        Only `pi-*` is dynamic — the claude-* and sdk-* families are closed sets,
        so unknown variants there should fail loudly rather than silently round-trip.
        """
        if isinstance(value, str) and value.startswith("pi-"):
            obj = str.__new__(cls, value)
            obj._name_ = value.upper().replace("-", "_")
            obj._value_ = value
            return obj
        return None

    @classmethod
    def from_str(cls, value: str) -> "SessionType":
        """Parse session type from string."""
        value = value.lower().replace("_", "-")
        try:
            return cls(value)
        except ValueError:
            return cls.STANDARD  # Default for unknown types

    def to_cli_flags(self) -> list[str]:
        """Convert to CLI flags for Claude."""
        if self == SessionType.BARE:
            return []  # No Claude
        elif self == SessionType.CLAUDE_BYPASS:
            return ["--dangerously-skip-permissions"]
        elif self == SessionType.CLAUDE_PROMPTED:
            return []  # Uses permission hooks, no bypass
        elif self == SessionType.CLAUDE_AUTO:
            return ["--enable-auto-mode", "--permission-mode", "auto"]
        elif self == SessionType.CLAUDE_RESTRICTED:
            return ["--tools", "Bash"]  # ONLY bash tool (for say command)
        return []


def detect_default_agent_type() -> str:
    """The only supported agent backend today is Claude Code."""
    return "claude"


def normalize_session_type(session_type: str, agent_type: str) -> str:
    """Map universal types (standard/worker/voice) to agent-specific types."""
    if (
        session_type.startswith("claude-")
        or session_type.startswith("pi-")
        or session_type.startswith("sdk-")
        or session_type == "bare"
    ):
        return session_type

    if session_type == "standard":
        return f"{agent_type}-bypass"
    elif session_type == "worker":
        return f"{agent_type}-restricted"
    elif session_type == "voice":
        return f"{agent_type}-prompted"

    return f"{agent_type}-bypass"


def _normalize_allowed_entry(entry: dict) -> dict:
    """Normalize an allowed_paths entry to {path: str, allow: str|list}.

    Entry must be a dict with "path" key and optional "allow" (defaults to "all").
    """
    allow = entry.get("allow", "all")
    if isinstance(allow, str):
        allow = allow.strip().lower()
    elif isinstance(allow, list):
        allow = [a.strip().lower() for a in allow]
    return {"path": entry["path"], "allow": allow}


@dataclass
class SafetyConfig:
    """Per-project safety overrides for damage control hooks."""
    allowed_paths: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {}
        if self.allowed_paths:
            d["allowed_paths"] = self.allowed_paths
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "SafetyConfig":
        raw = data.get("allowed_paths", [])
        if not isinstance(raw, list):
            raw = []
        allowed_paths = [_normalize_allowed_entry(e) for e in raw if isinstance(e, dict)]
        return cls(allowed_paths=allowed_paths)


@dataclass
class ProjectConfig:
    """Project-level configuration for a project directory.

    Lives in .agentwire.yml in the project root.
    Shared by all sessions running in this project folder.
    Session name is NOT stored here - it's runtime context from environment.
    """
    type: SessionType = SessionType.STANDARD
    roles: list[str] = field(default_factory=list)  # Composable roles
    voice: Optional[str] = None  # TTS voice
    parent: Optional[str] = None  # Parent session for hierarchical notifications
    shell: Optional[str] = None  # Default shell for task commands (default: /bin/sh)
    tasks: dict[str, Any] = field(default_factory=dict)  # Task definitions (raw dict, parsed by tasks.py)
    safety: SafetyConfig = field(default_factory=SafetyConfig)

    def to_dict(self) -> dict:
        """Convert to dictionary for YAML serialization."""
        d = {
            "type": self.type.value,
        }
        if self.roles:
            d["roles"] = self.roles
        if self.voice:
            d["voice"] = self.voice
        if self.parent:
            d["parent"] = self.parent
        if self.shell:
            d["shell"] = self.shell
        if self.tasks:
            d["tasks"] = self.tasks
        safety_dict = self.safety.to_dict()
        if safety_dict:
            d["safety"] = safety_dict
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectConfig":
        """Create ProjectConfig from dictionary."""
        type_value = data.get("type", "standard")
        roles = data.get("roles", [])
        voice = data.get("voice")
        parent = data.get("parent")
        shell = data.get("shell")
        tasks = data.get("tasks", {})
        safety_data = data.get("safety", {})
        safety = SafetyConfig.from_dict(safety_data) if isinstance(safety_data, dict) else SafetyConfig()

        return cls(
            type=SessionType.from_str(type_value) if isinstance(type_value, str) else type_value,
            roles=roles if isinstance(roles, list) else [roles] if roles else [],
            voice=voice,
            parent=parent,
            shell=shell,
            tasks=tasks if isinstance(tasks, dict) else {},
            safety=safety,
        )


def find_project_config(start_path: Optional[Path] = None) -> Optional[Path]:
    """Find .agentwire.yml by walking up from start_path.

    Args:
        start_path: Directory to start searching from. Defaults to cwd.

    Returns:
        Path to .agentwire.yml if found, None otherwise.
    """
    if start_path is None:
        start_path = Path.cwd()
    else:
        start_path = Path(start_path).resolve()

    current = start_path
    while current != current.parent:
        config_file = current / ".agentwire.yml"
        if config_file.exists():
            return config_file
        current = current.parent

    # Check root
    config_file = current / ".agentwire.yml"
    if config_file.exists():
        return config_file

    return None


def load_project_config(path: Optional[Path] = None) -> Optional[ProjectConfig]:
    """Load project config from .agentwire.yml.

    Args:
        path: Path to .agentwire.yml or directory containing it.
              If None, searches from cwd upward.

    Returns:
        ProjectConfig if found and valid, None otherwise.
    """
    if path is None:
        config_path = find_project_config()
    elif path.is_dir():
        config_path = path / ".agentwire.yml"
    else:
        config_path = path

    if config_path is None or not config_path.exists():
        return None

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        return ProjectConfig.from_dict(data)
    except Exception:
        return None


def save_project_config(config: ProjectConfig, project_dir: Path) -> bool:
    """Save project config to .agentwire.yml.

    Args:
        config: ProjectConfig to save
        project_dir: Directory to save config in

    Returns:
        True if saved successfully, False otherwise.
    """
    project_dir = Path(project_dir).resolve()
    config_file = project_dir / ".agentwire.yml"

    try:
        with open(config_file, "w") as f:
            yaml.safe_dump(config.to_dict(), f, default_flow_style=False, sort_keys=False)
        return True
    except Exception:
        return False


def get_voice_from_config(project_path: Optional[Path] = None) -> Optional[str]:
    """Get voice from project config.

    Convenience function for say command.

    Args:
        project_path: Path to search from. Defaults to cwd.

    Returns:
        Voice name if config found and has voice, None otherwise.
    """
    config = load_project_config(project_path)
    return config.voice if config else None


def get_parent_from_config(project_path: Optional[Path] = None) -> Optional[str]:
    """Get parent session from project config.

    Used for hierarchical notifications - voice-orch sessions
    notify their parent (typically 'agentwire' main session).

    Args:
        project_path: Path to search from. Defaults to cwd.

    Returns:
        Parent session name if config found and has parent, None otherwise.
    """
    config = load_project_config(project_path)
    return config.parent if config else None
