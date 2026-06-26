"""Size-capped append for the append-only ``*-events.jsonl`` debug logs.

Single source of truth for the five event-log writers (inbox, prompt-router,
usage-limit, session-context, scheduler). Each writer used to be a bare
``open(path, "a")`` with no size bound, so a long-lived self-host box would
accumulate unbounded debug history (#499). They all now route through
:func:`append_event`, which rolls the file once it crosses a size threshold,
keeping a bounded number of ``.1``/``.2`` backups.

The on-disk line format is unchanged: one ``json.dumps(record) + "\n"`` per
event. Rotation only renames whole files; it never rewrites a line.

Both knobs are env-configurable (host-side, no rebuild) with sane defaults:

- ``AGENTWIRE_EVENT_LOG_MAX_BYTES`` — roll once the active file would exceed
  this many bytes. Default 5 MiB. ``0`` (or negative) disables rotation.
- ``AGENTWIRE_EVENT_LOG_BACKUPS`` — how many rolled files to retain
  (``.1`` .. ``.N``). Default 3. ``0`` keeps no backups (oldest is dropped).
"""

import json
import os
from pathlib import Path

# Sane defaults — activity-proportional growth (#499) means these rarely roll,
# but they cap a slow-burn paper-cut for unattended boxes.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
DEFAULT_BACKUPS = 3


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int from the environment, falling back on garbage."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _max_bytes() -> int:
    return _env_int("AGENTWIRE_EVENT_LOG_MAX_BYTES", DEFAULT_MAX_BYTES)


def _backups() -> int:
    return max(0, _env_int("AGENTWIRE_EVENT_LOG_BACKUPS", DEFAULT_BACKUPS))


def _rotate(path: Path, backups: int) -> None:
    """Roll ``path`` -> ``path.1`` -> ... -> ``path.N``, dropping the oldest.

    No-op if the file is gone by the time we get here. Mirrors the scheduler's
    ``_rotate_backups`` shift, but for the events file instead of the state
    file: oldest first so nothing is clobbered.
    """
    # Drop the oldest retained backup (or the active file itself if backups==0).
    oldest = path.with_name(path.name + f".{backups}") if backups else path
    try:
        if oldest.exists():
            oldest.unlink()
    except OSError:
        pass
    # Shift .{i} -> .{i+1} from the top down so we never overwrite a kept file.
    for i in range(backups - 1, 0, -1):
        src = path.with_name(path.name + f".{i}")
        if src.exists():
            try:
                os.replace(src, path.with_name(path.name + f".{i + 1}"))
            except OSError:
                pass
    if backups:
        try:
            os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass


def append_event(path: Path, record: dict) -> None:
    """Append one JSONL event to ``path``, rotating first if oversized.

    Best-effort: any OS error is swallowed so logging never breaks a caller.
    The caller owns the record's contents (``ts``/``event``/fields); this helper
    owns serialization (``json.dumps(record) + "\n"``) and the size cap.
    """
    line = json.dumps(record) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = _max_bytes()
        if max_bytes > 0:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            # Roll before the write that would push us past the threshold, so a
            # single fresh file can always hold at least one event.
            if size and size + len(line.encode("utf-8")) > max_bytes:
                _rotate(path, _backups())
        with open(path, "a") as f:
            f.write(line)
    except OSError:
        pass
