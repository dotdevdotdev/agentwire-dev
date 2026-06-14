"""Sitting state for the council subsystem.

A *sitting* is one ``council start`` → ``council stop`` span: the orchestrator
session, the roster of lens souls, and a monotonic prompt counter. State is
small JSON at ``~/.agentwire/council/sitting.json``, written atomically
(tempfile + ``os.replace``).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

COUNCIL_DIR = Path.home() / ".agentwire" / "council"
SITTING_PATH = COUNCIL_DIR / "sitting.json"
WORKSPACE_DIR = COUNCIL_DIR / "workspace"
PROMPTS_DIR = COUNCIL_DIR / "prompts"

ORCHESTRATOR_SESSION = "agentwire-council"
DEFAULT_ROSTER = [
    "brain",
    "conscience",
    "gut",
    "critic",
    "historian",
    "devils-advocate",
]

# Lens names become session names, role names, and reply filenames.
_LENS_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def valid_lens(name: str) -> bool:
    """True iff a lens name is safe for sessions, roles, and filenames."""
    return bool(_LENS_RE.match(name))


def session_for(lens: str) -> str:
    """tmux session name for a lens."""
    return f"council-{lens}"


@dataclass
class Sitting:
    orchestrator: str
    roster: list[str]
    sessions: dict[str, str]  # lens -> tmux session name
    started_at: str
    next_prompt_id: int = 1
    session_type: str = "claude-bypass"

    def to_dict(self) -> dict:
        return {
            "orchestrator": self.orchestrator,
            "roster": list(self.roster),
            "sessions": dict(self.sessions),
            "started_at": self.started_at,
            "next_prompt_id": self.next_prompt_id,
            "session_type": self.session_type,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Sitting":
        return cls(
            orchestrator=d.get("orchestrator", ORCHESTRATOR_SESSION),
            roster=list(d.get("roster", [])),
            sessions=dict(d.get("sessions", {})),
            started_at=d.get("started_at", ""),
            next_prompt_id=int(d.get("next_prompt_id", 1)),
            session_type=d.get("session_type", "claude-bypass"),
        )


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically: temp file in same dir, then ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_sitting() -> Sitting | None:
    """Return the current sitting, or None if no sitting (or corrupt state)."""
    if not SITTING_PATH.exists():
        return None
    try:
        with open(SITTING_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return Sitting.from_dict(data)


def write_sitting(sitting: Sitting) -> None:
    _atomic_write(SITTING_PATH, sitting.to_dict())


def clear_sitting() -> None:
    """End the sitting. Prompt history under ``prompts/`` is kept."""
    try:
        SITTING_PATH.unlink()
    except FileNotFoundError:
        pass


def allocate_prompt_id() -> int:
    """Bump and persist the sitting's prompt counter; return the new id.

    Raises ``RuntimeError`` if no sitting is active.
    """
    sitting = read_sitting()
    if sitting is None:
        raise RuntimeError("no active council sitting — run 'agentwire council start'")
    prompt_id = sitting.next_prompt_id
    sitting.next_prompt_id = prompt_id + 1
    write_sitting(sitting)
    return prompt_id


def latest_prompt_id() -> int | None:
    """The most recently allocated prompt id, or None if none yet."""
    sitting = read_sitting()
    if sitting is None or sitting.next_prompt_id <= 1:
        return None
    return sitting.next_prompt_id - 1
