"""Is a session busy, idle, or blocked right now? (#1198)

"Which sessions are busy" had no answer: the session list carries no activity
state, and the only existing heuristic (``session_ready.pane_shows_activity``)
matches "tokens" or a tool-result glyph anywhere in scrollback, so it reads
yesterday's work as busy.

The live signal is Claude Code's status row, read only at its LIVE position:
the rows directly above the input box. While a turn runs, that row is the
spinner, ``· Discombobulating… (31s · ↓ 2.8k tokens · thought for 9s)``
(older builds append ``esc to interrupt``); once the turn ends it becomes
``✻ Worked for 6s · done 8:08 AM`` or disappears. A live dialog (permission,
plan, question) is ``blocked``: the agent can't move until someone answers.

States: ``busy`` · ``idle`` · ``blocked`` · ``unknown`` (no input box to read:
a bare shell, a non-agent pane, an unreadable capture). Unknown is never
reported as idle.
"""

from __future__ import annotations

import re

STATES = ("busy", "idle", "blocked", "unknown")

#: How many non-blank rows above the box count as the live status position.
#: Claude Code can render one hint row (e.g. "✔ Update installed") between the
#: spinner and the box.
LIVE_ROWS = 3

#: The running spinner: a glyph, a verb ending in an ellipsis, then a
#: parenthesised elapsed time — or the older "esc to interrupt" hint.
_SPINNER = re.compile(
    r"^\s*\S\s+\S[^()]*…\s*\((?:\d+[hms]\b|[^)]*esc to interrupt)",
)


#: Rows that belong to the transcript, not the live status region.
_TRANSCRIPT_GLYPHS = ("⏺", "⎿", "❯", ">")

#: The finished-turn line: "✻ Worked for 6s · done 8:08 AM".
_TURN_END = re.compile(r"\bfor\s+\d+[hms]\b.*\bdone\b|\bdone\s+\d{1,2}:\d{2}")


def classify_capture(capture: str) -> str:
    """The state a pane capture shows. Pure; see the module docstring."""
    from . import prompt_router
    from .session_ready import strip_input_box

    if not capture.strip():
        return "unknown"
    if prompt_router.detect_prompt(capture) is not None:
        return "blocked"
    outside = strip_input_box(capture)
    if outside is None:
        return "unknown"
    rows = [r for r in outside.split("\n")
            if r.strip() and not prompt_router._is_rule_line(r)]
    # Walk up from the box through the status region only: a transcript row
    # (reply, tool call/result, echoed prompt) or a turn-end line ends it, so
    # a spinner further up is history, not the live row.
    for row in reversed(rows[-LIVE_ROWS:]):
        if _SPINNER.match(row):
            return "busy"
        if row.lstrip()[:1] in _TRANSCRIPT_GLYPHS or _TURN_END.search(row):
            break
    return "idle"


def session_state(session: str, pane_index: int = 0) -> str:
    """The live state of *session*'s agent pane; ``unknown`` if unreadable."""
    from .session_ready import capture_session

    try:
        return classify_capture(capture_session(session, pane_index=pane_index))
    except Exception:  # noqa: BLE001  # a dead pane or missing tmux is "unknown", not idle
        return "unknown"
