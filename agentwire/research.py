"""Research dropbox resolver for Briefing Mode.

Correspondents file their deep reports into a per-anchor directory under
``~/.agentwire/research/<anchor-session>/``. The anchor pulls passive ``ingest``
messages whose typed ``ref`` field points at those files, then reads them on the
human's cue. This module is the single source of truth for that path — so the
roles, CLI, and MCP don't hand-compute it (mirrors ``inbox.INBOX_ROOT`` and
``usage_limit`` state paths).
"""

from __future__ import annotations

from pathlib import Path

RESEARCH_ROOT = Path.home() / ".agentwire" / "research"


def research_dir(anchor_session: str) -> Path:
    """The dropbox directory for an anchor's line of research (not created)."""
    return RESEARCH_ROOT / anchor_session


def ensure_research_dir(anchor_session: str) -> Path:
    """Ensure the anchor's dropbox exists and return its path."""
    path = research_dir(anchor_session)
    path.mkdir(parents=True, exist_ok=True)
    return path
