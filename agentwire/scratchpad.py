"""Scratch pad — persistent notes shared across portal clients and agents.

Notes live in ``~/.agentwire/scratchpad.json`` so every portal client (desktop,
phone) sees the same pad, and agents can add notes via the MCP tool / CLI.
This module is the single source of truth for note CRUD — the CLI commands,
portal routes, and MCP tools all call into it.

Writes are atomic (tmp file + rename) so concurrent writers can't truncate
the pad. Last-writer-wins per note; the pad is a convenience surface, not a
database.
"""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRATCHPAD_FILE = Path.home() / ".agentwire" / "scratchpad.json"

MAX_NOTE_CHARS = 20_000   # one note (keep pads snappy to render)
MAX_NOTES = 200           # oldest notes beyond this are dropped on add


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_notes() -> list[dict]:
    """All notes, newest first."""
    try:
        data = json.loads(SCRATCHPAD_FILE.read_text())
        notes = data.get("notes", [])
        return notes if isinstance(notes, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_notes(notes: list[dict]) -> None:
    SCRATCHPAD_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=SCRATCHPAD_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"notes": notes}, f, indent=2)
        os.replace(tmp, SCRATCHPAD_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_note(text: str, source: str | None = None) -> dict:
    """Create a note (newest first). Returns the created note.

    Args:
        text: Note body (trimmed; capped at MAX_NOTE_CHARS).
        source: Optional provenance label, e.g. a session name or "selection".
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Note text is empty")
    note = {
        "id": uuid.uuid4().hex[:12],
        "text": text[:MAX_NOTE_CHARS],
        "source": source or None,
        "created": _now(),
        "updated": _now(),
    }
    notes = load_notes()
    notes.insert(0, note)
    _save_notes(notes[:MAX_NOTES])
    return note


def update_note(note_id: str, text: str) -> dict | None:
    """Replace a note's text. Returns the updated note, or None if not found."""
    text = (text or "").strip()
    notes = load_notes()
    for note in notes:
        if note.get("id") == note_id:
            note["text"] = text[:MAX_NOTE_CHARS]
            note["updated"] = _now()
            _save_notes(notes)
            return note
    return None


def remove_note(note_id: str) -> bool:
    """Delete a note. Returns True if it existed."""
    notes = load_notes()
    remaining = [n for n in notes if n.get("id") != note_id]
    if len(remaining) == len(notes):
        return False
    _save_notes(remaining)
    return True


def clear_notes() -> int:
    """Delete all notes. Returns how many were removed."""
    notes = load_notes()
    _save_notes([])
    return len(notes)
