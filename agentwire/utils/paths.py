"""
Centralized path management for AgentWire directories and files.

All paths under ~/.agentwire/ should use these helpers to ensure
consistency and automatic directory creation.
"""

from pathlib import Path


def agentwire_dir() -> Path:
    """Return ~/.agentwire/, creating if needed.

    Returns:
        Path to the AgentWire configuration directory.

    Example:
        config_dir = agentwire_dir()
        # /Users/user/.agentwire/
    """
    path = Path.home() / ".agentwire"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    """Return path to config.yaml.

    Returns:
        Path to ~/.agentwire/config.yaml (may not exist).

    Example:
        if config_path().exists():
            config = load_yaml(config_path())
    """
    return agentwire_dir() / "config.yaml"


def machines_path() -> Path:
    """Return path to machines.json.

    Returns:
        Path to ~/.agentwire/machines.json (may not exist).
    """
    return agentwire_dir() / "machines.json"


def logs_dir() -> Path:
    """Return ~/.agentwire/logs/, creating if needed.

    Returns:
        Path to logs directory.
    """
    path = agentwire_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def voices_dir() -> Path:
    """Return ~/.agentwire/voices/, creating if needed.

    Returns:
        Path to voice samples directory.
    """
    path = agentwire_dir() / "voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir() -> Path:
    """Return ~/.agentwire/uploads/, creating if needed.

    Returns:
        Path to uploads directory.
    """
    path = agentwire_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def hooks_dir() -> Path:
    """Return ~/.agentwire/hooks/, creating if needed.

    Returns:
        Path to hooks directory.
    """
    path = agentwire_dir() / "hooks"
    path.mkdir(parents=True, exist_ok=True)
    return path
