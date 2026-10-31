"""The one read-modify-write path for Claude's ``settings.json`` (#1220).

Every agentwire edit of the user's Claude settings goes through
:func:`edit_claude_settings`. The file is the user's, not ours, so:

* an unparseable file is a REFUSAL (:class:`SettingsError`), never a rewrite --
  replacing it with ``{}`` deleted every user setting on the next heal;
* the write is atomic (temp file in the same directory + ``os.replace``), so a
  crash mid-write cannot truncate Claude's own settings;
* the file is re-read just before the replace; if it changed under us the edit
  is recomputed from the new bytes once, and refused if it moves again;
* the original is copied to ``settings.json.agentwire.bak`` before the first
  change, and never overwritten after.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .utils.paths import claude_dir

BACKUP_SUFFIX = ".agentwire.bak"
_ATTEMPTS = 2


class SettingsError(RuntimeError):
    """settings.json could not be safely edited; nothing was written."""


def settings_path() -> Path:
    return claude_dir() / "settings.json"


def _read(path: Path) -> tuple[bytes | None, dict[str, Any]]:
    """(raw bytes or None if absent, parsed settings). Refuses anything unparseable."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError as e:
        raise SettingsError(f"cannot read {path}: {e}") from e
    if not raw.strip():
        return raw, {}
    try:
        parsed = json.loads(raw)
    except ValueError as e:  # JSONDecodeError and UnicodeDecodeError
        raise SettingsError(
            f"{path} is not valid JSON ({e}); left untouched. "
            "Fix the file and re-run."
        ) from e
    if not isinstance(parsed, dict):
        raise SettingsError(f"{path} is not a JSON object; left untouched. Fix the file and re-run.")
    return raw, parsed


def edit_claude_settings(mutate: Callable[[dict[str, Any]], bool]) -> bool:
    """Apply ``mutate`` to the parsed settings and persist it if it reports a change.

    ``mutate`` edits the dict in place and returns True when it changed
    something. Returns True if the file was written. Raises
    :class:`SettingsError` (writing nothing) on unparseable input or when the
    file keeps changing underneath the edit.
    """
    # Edit through a symlink (dotfile managers) instead of replacing it.
    path = settings_path()
    path = path.resolve() if path.is_symlink() else path

    for _ in range(_ATTEMPTS):
        raw, settings = _read(path)
        if not mutate(settings):
            return False

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(settings, indent=2))
                f.flush()
                os.fsync(f.fileno())
            if path.exists():
                shutil.copymode(path, tmp)
            if _current_bytes(path) != raw:
                continue  # changed since we read it: recompute from the new bytes
            backup = path.with_name(path.name + BACKUP_SUFFIX)
            if raw is not None and not backup.exists():
                shutil.copy2(path, backup)
            os.replace(tmp, path)
            return True
        finally:
            tmp.unlink(missing_ok=True)

    raise SettingsError(f"{path} kept changing while it was being edited; nothing written. Re-run.")


def _current_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
