"""
Project-level configuration (.agentwire.yml).

This file lives in project directories and is the source of truth for session config.
"""

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# POSTURE is the single session axis: the Claude Code permission mode the agent
# runs under (#729). Claude Code is the only agent backend (#730), so there is
# nothing left to fuse a permission mode WITH — posture is all there is. The
# `bare` sentinel is orthogonal: no agent, so no permission mode at all.
# Tool-locking postures (restricted/readonly) were dropped — every agent runs
# with damage-control hooks as the guard, not tool allowlists.
POSTURES = ("bypass", "prompted", "auto")
DEFAULT_POSTURE = "bypass"
BARE = "bare"


def resolve_posture(value: str) -> str:
    """Validate an axis value as a posture (or the ``bare`` sentinel).

    Accepts the three postures (bypass/prompted/auto) and the ``bare`` no-agent
    sentinel. Raises ``ValueError`` on an unknown value so a typo fails loudly
    instead of silently picking a tier.
    """
    v = (value or DEFAULT_POSTURE).strip().lower()
    if v == BARE:
        return BARE
    if v not in POSTURES:
        raise ValueError(
            f"Unknown posture '{v}' (expected one of: {', '.join(POSTURES)}, or bare)"
        )
    return v


@dataclass
class WorktreeOverrides:
    """Per-project overrides for `agentwire worktree` (the `worktree:` block).

    Sits between per-invocation flags and the global config in the
    precedence chain (#705): CLI/MCP flag → this block → global `worktree:`
    in config.yaml → built-ins. ``dir`` only moves the root — the nesting
    shape is unchanged: ``<dir>/<project>/<name>/``.
    """
    dir: Optional[Path] = None   # overrides worktree.worktree_dir for this project
    base: Optional[str] = None   # overrides worktree.default_base for this project


_WORKTREE_OVERRIDE_KEYS = {"dir", "base"}


def _parse_worktree_overrides(data: Any) -> WorktreeOverrides:
    """Parse the optional ``worktree:`` block. Unknown keys warn, never fail."""
    if not isinstance(data, dict):
        if data is not None:
            print(
                f"Warning: .agentwire.yml `worktree:` should be a mapping "
                f"with dir/base keys, got {type(data).__name__} — ignoring",
                file=sys.stderr,
            )
        return WorktreeOverrides()
    unknown = sorted(set(data) - _WORKTREE_OVERRIDE_KEYS)
    if unknown:
        print(
            f"Warning: unknown key(s) in .agentwire.yml `worktree:` block: "
            f"{', '.join(unknown)} (expected: dir, base)",
            file=sys.stderr,
        )
    dir_val = data.get("dir")
    base_val = data.get("base")
    return WorktreeOverrides(
        dir=Path(str(dir_val)).expanduser() if dir_val else None,
        base=str(base_val) if base_val else None,
    )


@dataclass
class ProjectConfig:
    """Project-level configuration for a project directory.

    Lives in .agentwire.yml in the project root — PURELY declarative session
    config (type/roles/voice/parent/worktree), no execution vector, agent-writable
    (#720). Holds NO damage-control safety config — the kill switch, rule knobs,
    AND the per-project allowlist all live in the protected, agent-unwritable
    ``.damagecontrol.yml`` instead (#466/#467). Task definitions (pre/post/
    on_task_end/shell — code the scheduler runs via shell=True) live in the
    separate, protected ``.agentwire.tasks.yml`` instead (see ``tasks.py`` /
    ``tasks_cli.py``) — never parsed here.
    Shared by all sessions running in this project folder.
    Session name is NOT stored here - it's runtime context from environment.
    """
    posture: str = DEFAULT_POSTURE  # Permission mode: bypass|prompted|auto, or bare
    roles: list[str] = field(default_factory=list)  # Composable roles
    voice: Optional[str] = None  # TTS voice
    parent: Optional[str] = None  # Parent session for hierarchical notifications
    worktree: WorktreeOverrides = field(default_factory=WorktreeOverrides)  # `agentwire worktree` overrides (#705)

    def to_dict(self) -> dict:
        """Convert to dictionary for YAML serialization."""
        d = {
            "posture": self.posture,
        }
        if self.roles:
            d["roles"] = self.roles
        if self.voice:
            d["voice"] = self.voice
        if self.parent:
            d["parent"] = self.parent
        if self.worktree.dir or self.worktree.base:
            wt: dict[str, Any] = {}
            if self.worktree.dir:
                wt["dir"] = str(self.worktree.dir)
            if self.worktree.base:
                wt["base"] = self.worktree.base
            d["worktree"] = wt
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectConfig":
        """Create ProjectConfig from dictionary."""
        roles = data.get("roles", [])
        voice = data.get("voice")
        parent = data.get("parent")

        try:
            posture = resolve_posture(str(data.get("posture", DEFAULT_POSTURE)))
        except ValueError:
            posture = DEFAULT_POSTURE  # Unknown value → fall back, don't crash config load

        return cls(
            posture=posture,
            roles=roles if isinstance(roles, list) else [roles] if roles else [],
            voice=voice,
            parent=parent,
            worktree=_parse_worktree_overrides(data.get("worktree")),
        )


def find_project_config(start_path: Optional[Path] = None) -> Optional[Path]:
    """Find project config by walking up from start_path.

    At each directory level, a local untracked ``.agentwire.yml`` wins over a
    committed ``.agentwire.yml.example`` template. The ``.example`` fallback lets
    a repo (e.g. agentwire-dev itself) ship a sensible default config that a
    fresh clone uses out of the box, while the owner's personal, gitignored
    ``.agentwire.yml`` overrides it when present. See #620.

    Args:
        start_path: Directory to start searching from. Defaults to cwd.

    Returns:
        Path to the resolved config file if found, None otherwise.
    """
    if start_path is None:
        start_path = Path.cwd()
    else:
        start_path = Path(start_path).resolve()

    current = start_path
    while True:
        live = current / ".agentwire.yml"
        if live.exists():
            return live
        example = current / ".agentwire.yml.example"
        if example.exists():
            return example
        if current == current.parent:
            break
        current = current.parent

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
        # Local .agentwire.yml wins over a committed .agentwire.yml.example (#620).
        live = path / ".agentwire.yml"
        example = path / ".agentwire.yml.example"
        config_path = live if live.exists() else example
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


def ensure_gitignored(
    project_dir: Path,
    filename: str = ".agentwire.yml",
    pattern: Optional[str] = None,
) -> bool:
    """Ensure ``filename`` is gitignored in the project's repo.

    These files are personal/live config (voices, schedules, email recipients,
    task shell commands), and a tracked copy breaks worktree dispatch: worktree
    runs check out HEAD, so uncommitted live edits to a tracked file are
    silently ignored. Worktree runs get the live file via
    projects.worktrees.copy_files instead. A file that is already tracked is
    left alone — that's a deliberate choice to share versioned config.

    Args:
        project_dir: Project root (the directory containing ``filename``)
        filename: The tracked/ignored status of THIS file gates the check.
        pattern: The line appended to .gitignore (defaults to ``filename``;
            pass a glob to also cover sibling files, e.g. a staging draft).

    Returns:
        True if .gitignore was modified, False otherwise.
    """
    pattern = pattern or filename
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
            ["git", "ls-files", "--error-unmatch", filename],
            cwd=project_dir, capture_output=True, timeout=10,
        )
        if tracked.returncode == 0:
            return False

        ignored = subprocess.run(
            ["git", "check-ignore", "-q", filename],
            cwd=project_dir, capture_output=True, timeout=10,
        )
        if ignored.returncode == 0:
            return False

        gitignore = project_dir / ".gitignore"
        existing = gitignore.read_text() if gitignore.exists() else ""
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with open(gitignore, "a") as f:
            f.write(f"{prefix}# AgentWire personal config — keep untracked (worktree dispatch + privacy)\n{pattern}\n")
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
