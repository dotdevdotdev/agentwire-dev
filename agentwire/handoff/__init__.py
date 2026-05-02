"""
Shareable conversation handoff bundles.

Two artifacts produced from one source:
- ai-handoff.md: XML-tagged markdown for pasting into another LLM
- show-the-story.html: single-file human presentation with tabs and scroll-slides

The in-conversation agent writes ai-handoff.md (it has full context for free).
The CLI/MCP renders show-the-story.html from it via Jinja2 (deterministic).
"""

from .schema import (
    BundleData,
    Decision,
    DeadEnd,
    Instruction,
    JourneyBeat,
    Metadata,
    OpenThread,
    Stats,
    Theme,
)

__all__ = [
    "BundleData",
    "Decision",
    "DeadEnd",
    "Instruction",
    "JourneyBeat",
    "Metadata",
    "OpenThread",
    "Stats",
    "Theme",
]
