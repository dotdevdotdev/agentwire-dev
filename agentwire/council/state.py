"""Sitting state for the council subsystem — namespaced by ``<name>``.

A *sitting* is one ``council start`` → ``council stop`` span: the orchestrator
session, the roster of lens souls, and a monotonic prompt counter. Independent
sittings run concurrently, each isolated under its own name::

    ~/.agentwire/council/<name>/
      sitting.json        # roster, lens→session map, prompt counter
      workspace/          # shared cwd all the sitting's sessions run in
      prompts/NNNN/       # per-prompt inbox (see inbox.py)

The ``<name>`` is identity. Sessions are ``council-<name>-<lens>``; the
orchestrator is ``agentwire-council-<name>``. **Never** ``.split('-')`` a
session string to recover name/lens — ``sitting.json`` is the SSOT for the
lens→session map.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Root holding every namespaced sitting (``<name>/`` subdirs). The only
# filesystem constant — everything else is derived per name so two sittings
# never share state.
COUNCIL_ROOT = Path.home() / ".agentwire" / "council"

DEFAULT_ROSTER = [
    "brain",
    "conscience",
    "gut",
    "critic",
    "historian",
    "devils-advocate",
]

# Lens *and* council names become session names, role names, and filenames.
# Same grammar for both: ``[a-z0-9][a-z0-9-]*`` (tmux-safe — no ``.``/``:``).
_LENS_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# cwd-derived default names are slugified + capped so ``council-<name>-<lens>``
# stays a sane tmux session length.
_NAME_MAX = 24


def valid_lens(name: str) -> bool:
    """True iff a lens name is safe for sessions, roles, and filenames."""
    return bool(_LENS_RE.match(name))


# A council name shares the lens grammar (both flow into session/file names).
valid_name = valid_lens


# --- per-name paths -------------------------------------------------------------


def council_dir(name: str) -> Path:
    return COUNCIL_ROOT / name


def sitting_path(name: str) -> Path:
    return council_dir(name) / "sitting.json"


def workspace_dir(name: str) -> Path:
    return council_dir(name) / "workspace"


def prompts_dir(name: str) -> Path:
    return council_dir(name) / "prompts"


def orchestrator_for(name: str) -> str:
    """tmux session name for a sitting's orchestrator."""
    return f"agentwire-council-{name}"


def session_for(name: str, lens: str) -> str:
    """tmux session name for a lens within a sitting."""
    return f"council-{name}-{lens}"


def default_name(cwd: Path | None = None) -> str:
    """Deterministic sitting name seeded by the current directory.

    Derives from the repo/worktree root (so N worktrees of one repo don't
    collide onto one sitting), slugified with the #307 worktree helper and
    capped at ``_NAME_MAX``. A short path-hash is appended only when the slug
    would be truncated, keeping the common case readable while staying a pure
    function of cwd (re-derivable by every later command from the same dir).
    """
    from agentwire import worktree

    base = cwd or Path.cwd()
    root = worktree.git_root(base) or base
    slug = worktree.slugify(root.name)
    if len(slug) <= _NAME_MAX:
        return slug
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:6]
    return f"{slug[: _NAME_MAX - 7].rstrip('-')}-{digest}"


def list_sittings() -> list[str]:
    """Names of every sitting with state on disk (a ``sitting.json``)."""
    if not COUNCIL_ROOT.is_dir():
        return []
    out = []
    for child in COUNCIL_ROOT.iterdir():
        if child.is_dir() and (child / "sitting.json").exists():
            out.append(child.name)
    return out


# --- sitting record -------------------------------------------------------------


@dataclass
class Sitting:
    orchestrator: str
    roster: list[str]
    sessions: dict[str, str]  # lens -> tmux session name
    started_at: str
    cwd: str = ""  # dir the sitting was started from (for `council list`)
    next_prompt_id: int = 1
    session_type: str = "claude-bypass"

    def to_dict(self) -> dict:
        return {
            "orchestrator": self.orchestrator,
            "roster": list(self.roster),
            "sessions": dict(self.sessions),
            "started_at": self.started_at,
            "cwd": self.cwd,
            "next_prompt_id": self.next_prompt_id,
            "session_type": self.session_type,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Sitting":
        return cls(
            orchestrator=d.get("orchestrator", ""),
            roster=list(d.get("roster", [])),
            sessions=dict(d.get("sessions", {})),
            started_at=d.get("started_at", ""),
            cwd=d.get("cwd", ""),
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


def read_sitting(name: str) -> Sitting | None:
    """Return the named sitting, or None if absent (or corrupt state)."""
    path = sitting_path(name)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return Sitting.from_dict(data)


def write_sitting(name: str, sitting: Sitting) -> None:
    _atomic_write(sitting_path(name), sitting.to_dict())


def clear_sitting(name: str) -> None:
    """End the sitting. Prompt history under ``prompts/`` is kept."""
    try:
        sitting_path(name).unlink()
    except FileNotFoundError:
        pass


def allocate_prompt_id(name: str) -> int:
    """Bump and persist the sitting's prompt counter; return the new id.

    Raises ``RuntimeError`` if the named sitting doesn't exist.
    """
    sitting = read_sitting(name)
    if sitting is None:
        raise RuntimeError(
            f"no council sitting '{name}' — run 'agentwire council start'"
        )
    prompt_id = sitting.next_prompt_id
    sitting.next_prompt_id = prompt_id + 1
    write_sitting(name, sitting)
    return prompt_id


def latest_prompt_id(name: str) -> int | None:
    """The most recently allocated prompt id, or None if none yet."""
    sitting = read_sitting(name)
    if sitting is None or sitting.next_prompt_id <= 1:
        return None
    return sitting.next_prompt_id - 1
