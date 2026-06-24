"""
Project-level configuration (.agentwire.yml).

This file lives in project directories and is the source of truth for session config.
"""

import subprocess
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
    # Universal types (agent-agnostic, map to agent-specific types)
    STANDARD = "standard"  # Full automation -> claude-bypass
    WORKER = "worker"      # Worker pane -> claude-restricted
    VOICE = "voice"        # Voice with prompts -> claude-prompted

    @classmethod
    def _missing_(cls, value: object) -> "SessionType | None":
        """Handle dynamic pi-<provider> types not enumerated at definition time.

        Only `pi-*` is dynamic — the claude-* family is a closed set,
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


# The two orthogonal axes a fused session type ("claude-bypass") actually
# encodes. Untangling them is the point: the user picks a POSTURE (how much
# the agent can do unprompted) and a HARNESS (which agent backend), and we
# compose the internal fused string from them. Fused strings still work on
# input (legacy aliases), but posture×harness is the canonical surface.
POSTURES = ("bypass", "prompted", "restricted", "readonly")
DEFAULT_POSTURE = "bypass"
DEFAULT_HARNESS = "claude"


def compose_session_type(harness: str, posture: str) -> str:
    """Compose an internal fused session type from the posture × harness axes.

    - ``bare`` harness ignores posture (there is no agent to gate).
    - ``claude`` maps bypass/prompted/restricted to ``claude-<posture>``;
      ``readonly`` collapses to ``claude-restricted`` (Claude's most-locked
      tier — say-only).
    - ``pi-<provider>`` maps bypass→``pi-<provider>`` (its default tier),
      restricted→``pi-<provider>-restricted``, readonly→``pi-<provider>-readonly``;
      ``prompted`` collapses to the default tier (pi has no hook-prompt mode).

    Raises ValueError on an unknown posture so a typo fails loudly instead of
    silently picking a wrong tier.
    """
    harness = (harness or DEFAULT_HARNESS).strip().lower()
    posture = (posture or DEFAULT_POSTURE).strip().lower()
    if posture not in POSTURES:
        raise ValueError(
            f"Unknown posture '{posture}' (expected one of: {', '.join(POSTURES)})"
        )

    if harness == "bare":
        return "bare"

    if harness == "claude":
        if posture == "readonly":
            return "claude-restricted"
        return f"claude-{posture}"

    # pi-<provider> family
    if harness.startswith("pi-"):
        if posture in ("restricted", "readonly"):
            return f"{harness}-{posture}"
        return harness  # bypass / prompted → provider default tier

    # Unknown harness: treat it as an already-fused/explicit type.
    return harness


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
    """Per-project safety overrides for damage control hooks.

    Holds ONLY the ``allowed_paths`` allowlist — the human opt-in that re-permits
    specific paths (including protected control-plane files). The kill switch and
    rule knobs (``enabled`` / ``disabled_rules`` / ``unattended_allow``) live in
    the agent-unwritable ``.damagecontrol.yml`` instead (#466), never here.
    """
    allowed_paths: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {}
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
        ensure_gitignored(project_dir)
        return True
    except Exception:
        return False


def ensure_gitignored(project_dir: Path) -> bool:
    """Ensure .agentwire.yml is gitignored in the project's repo.

    .agentwire.yml is personal/live config (voices, schedules, email
    recipients), and a tracked copy breaks worktree dispatch: worktree runs
    check out HEAD, so uncommitted live edits to a tracked file are silently
    ignored. Worktree runs get the live file via projects.worktrees.copy_files
    instead. A file that is already tracked is left alone — that's a
    deliberate choice to share versioned config.

    Args:
        project_dir: Project root (the directory containing .agentwire.yml)

    Returns:
        True if .gitignore was modified, False otherwise.
    """
    project_dir = Path(project_dir).resolve()
    try:
        in_repo = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project_dir, capture_output=True, timeout=10,
        )
        if in_repo.returncode != 0:
            return False

        # Already tracked = deliberate team choice; don't fight it
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".agentwire.yml"],
            cwd=project_dir, capture_output=True, timeout=10,
        )
        if tracked.returncode == 0:
            return False

        ignored = subprocess.run(
            ["git", "check-ignore", "-q", ".agentwire.yml"],
            cwd=project_dir, capture_output=True, timeout=10,
        )
        if ignored.returncode == 0:
            return False

        gitignore = project_dir / ".gitignore"
        existing = gitignore.read_text() if gitignore.exists() else ""
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with open(gitignore, "a") as f:
            f.write(f"{prefix}# AgentWire personal config — keep untracked (worktree dispatch + privacy)\n.agentwire.yml\n")
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
