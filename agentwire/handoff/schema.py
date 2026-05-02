"""
Dataclasses for the handoff bundle.

The in-conversation agent produces ai-handoff.md following the XML-tagged
structure declared here. The parser materializes that into a BundleData,
which the renderer consumes to produce show-the-story.html.

Pydantic isn't a core dep, so we use stdlib dataclasses + light validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Instruction:
    """A single CLAUDE.md / rules / memory file inlined into the bundle."""

    path: str
    content: str
    kind: str = "claude_md"  # claude_md | rule | memory | project_claude_md


@dataclass
class Stats:
    turns: int = 0
    files_touched: int = 0
    duration_minutes: int | None = None
    tools_used: int = 0


@dataclass
class Decision:
    title: str
    rationale: str
    alternatives_considered: list[str] = field(default_factory=list)


@dataclass
class DeadEnd:
    title: str
    why_rejected: str


@dataclass
class OpenThread:
    title: str
    note: str


@dataclass
class JourneyBeat:
    title: str
    what_happened: str
    quote: str | None = None


@dataclass
class Metadata:
    cwd: str
    repo_url: str | None
    branch: str | None
    commit: str | None
    session_type: str | None
    model: str | None
    started_at: str | None
    ended_at: str | None
    user_identity: str | None
    mcp_servers: list[str] = field(default_factory=list)


@dataclass
class ProjectState:
    git_status: str = ""
    git_diff: str = ""
    git_log: str = ""
    key_files: dict[str, str] = field(default_factory=dict)


@dataclass
class ConversationSummary:
    goal: str
    tldr: str
    decisions: list[Decision] = field(default_factory=list)
    dead_ends: list[DeadEnd] = field(default_factory=list)
    open_threads: list[OpenThread] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)


@dataclass
class HandoffNote:
    one_sentence: str
    resume_at: str
    caveats: list[str] = field(default_factory=list)


@dataclass
class Theme:
    """Theme JSON the agent picks based on session vibe.

    Drops directly into theme.css.j2 to populate CSS variables.
    """

    name: str = "default"
    mood: str = "neutral"
    palette: dict[str, str] = field(
        default_factory=lambda: {
            "bg": "#0e0f13",
            "surface": "#1a1d24",
            "fg": "#e2e8f0",
            "muted": "#64748b",
            "accent": "#5eead4",
            "accent_2": "#fbbf24",
            "border": "#2a2f3a",
        }
    )
    fonts: dict[str, str] = field(
        default_factory=lambda: {
            "heading": "ui-monospace, 'JetBrains Mono', monospace",
            "body": "ui-sans-serif, system-ui, sans-serif",
        }
    )
    motion: str = "subtle"  # subtle | none | playful


@dataclass
class BundleData:
    """The full handoff bundle: parser output, renderer input."""

    version: str
    title: str
    metadata: Metadata
    instructions: list[Instruction]
    project_state: ProjectState
    summary: ConversationSummary
    journey: list[JourneyBeat]
    recent_turns: str  # raw markdown of last N turns, filtered
    handoff: HandoffNote
    theme: Theme
    raw_markdown: str = ""  # original ai-handoff.md text, for embed-in-HTML

    def to_dict(self) -> dict[str, Any]:
        """Flatten for Jinja2 — dataclasses.asdict-like but tolerates Path objects."""
        from dataclasses import asdict

        return asdict(self)


REQUIRED_TAGS = (
    "metadata",
    "instructions",
    "project_state",
    "conversation_summary",
    "handoff",
    "theme",
)
