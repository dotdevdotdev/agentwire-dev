"""Tracks claude-in-chrome tab ids opened by a session (#717).

Worktree sessions open claude-in-chrome tabs to e2e-verify their work before
opening a PR. Those tabs are never closed if the session finishes without
tidying up after itself, or dies mid-work — this module is pure bookkeeping
so a crashed session's orphaned tabs can still be identified during teardown.

agentwire has no way to call `tabs_close_mcp` itself: that MCP server runs
inside the CALLING agent's own client, not agentwire's process. So this only
tracks tab ids; the actual close always happens at the LLM/agent layer —
either the session closing its own tabs before finishing (the normal path),
or `worktree_remove` surfacing untracked-but-still-open tab ids to whichever
agent runs the teardown (the crash backstop).

Layout: one JSON file (``~/.agentwire/chrome-tabs.json``), keyed by session
name -> list of ``{tab_id, url, tracked_at}``. Plain JSON, hand-editable.
"""

import contextlib
import datetime
import fcntl
import json
import os
import tempfile
from pathlib import Path

REGISTRY_FILE = Path.home() / ".agentwire" / "chrome-tabs.json"


@contextlib.contextmanager
def _locked():
    """Serialize read-modify-write across processes via an flock sidecar."""
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = REGISTRY_FILE.with_suffix(REGISTRY_FILE.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load() -> dict:
    if not REGISTRY_FILE.exists():
        return {}
    try:
        data = json.loads(REGISTRY_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    """Atomically write the registry (temp file in same dir + os.replace)."""
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(REGISTRY_FILE.parent), prefix=REGISTRY_FILE.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REGISTRY_FILE)  # atomic on POSIX
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def track(session: str, tab_id: str, url: str | None = None) -> dict:
    """Record that ``session`` opened claude-in-chrome tab ``tab_id``.

    Idempotent per (session, tab_id) — re-tracking the same tab just refreshes
    its entry rather than duplicating it.
    """
    entry = {
        "tab_id": tab_id,
        "url": url,
        "tracked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with _locked():
        data = _load()
        tabs = [t for t in data.get(session, []) if t.get("tab_id") != tab_id]
        tabs.append(entry)
        data[session] = tabs
        _save(data)
    return entry


def untrack(session: str, tab_id: str) -> bool:
    """Drop a tracked tab (the session closed it itself). Returns whether it was tracked."""
    with _locked():
        data = _load()
        tabs = data.get(session, [])
        remaining = [t for t in tabs if t.get("tab_id") != tab_id]
        removed = len(remaining) != len(tabs)
        if remaining:
            data[session] = remaining
        else:
            data.pop(session, None)
        if removed:
            _save(data)
    return removed


def tabs_for(session: str) -> list[dict]:
    """All tracked tabs for a session (read-only)."""
    return _load().get(session, [])


def clear(session: str) -> list[dict]:
    """Drop + return every tracked tab for a session — the teardown backstop."""
    with _locked():
        data = _load()
        tabs = data.pop(session, [])
        if tabs:
            _save(data)
    return tabs


def all_tabs() -> dict[str, list[dict]]:
    """Every session -> tracked-tabs mapping (read-only, for listing/debugging)."""
    return _load()
