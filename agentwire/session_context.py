"""Session context observability — read the Claude Code context bar from a pane.

Phase 0 of context-bloat management (issue #442): make bloat *visible* and
queryable. This module only OBSERVES — it never auto-``/clear``s or
``/compact``s anything (that is Phase 1, a separate change).

What the bar means (verified empirically, 2026-06-22)
-----------------------------------------------------
The Claude Code footer renders a line like::

    [████████████████████████░░░░░] 92%

The percentage is **context REMAINING** — the headroom left before Claude
Code's own auto-compaction kicks in, NOT the amount consumed. Evidence:

- A fresh session reads high (~94%); the gap from 100% is the baseline
  system-prompt + tools + CLAUDE.md overhead that every session pays.
- Driving a live session and watching the number move: it *decreases* as the
  conversation grows (observed 92% → 91% after loading ~1.4k lines of files
  into one session's context).
- It does NOT correlate with age — a 6-day-old service session read 92% while
  a freshly-created session read 83% — because Claude Code auto-compacts and
  the number jumps back up afterward. So it is a live, resettable headroom
  gauge, not a lifetime-usage counter.

Therefore "bloated" == **LOW remaining %**. A session is flagged when its
remaining context drops to/below the warn threshold (default 20% remaining,
i.e. ~80% of the way toward the limit — the framing used in #442).

How it is parsed
----------------
Straight from ``tmux capture-pane`` text, the same mechanism agentwire already
uses for usage-limit dialogs (:func:`usage_limit._capture`) and prompt boxes
(:func:`prompt_router.input_box_content`). No new API into Claude Code.

Daemon sessions (scheduler / portal / tts / stt / kokoro) run plain processes,
not Claude conversations — they have no bar and nothing to bloat. They are
detected via the pane's current command (not an agent binary) and skipped
gracefully (surfaced as non-interactive, never flagged).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass

from .usage_limit import _capture, _tmux

# The context bar: a bracketed run of block-element glyphs (U+2580–U+259F
# covers full/partial/empty blocks) followed by "NN%". ANSI is stripped first.
_ANSI = re.compile(r"\x1b\[[0-9;]*m|\x1b\].*?\x07")
_CONTEXT_BAR_RE = re.compile(r"\[(?:[▀-▟]|\s)+\]\s*(\d{1,3})\s*%")
# The meta line above the bar: "… main   opus  $1805.19  7988m".
_MODEL_RE = re.compile(r"\b(opus|sonnet|haiku|fable)\b", re.IGNORECASE)

# pane_current_command values that mean an interactive agent runs in the pane.
# Claude Code panes report the node binary or a bare version string
# (e.g. "2.1.185"); daemons report python3.13 / uv / a bare shell. Mirrors
# prompt_router._AGENT_COMMAND_RE — kept local to avoid an import cycle risk.
_AGENT_COMMAND_RE = re.compile(r"^(node|claude|\d+\.\d+\.\d+\S*)$")

DEFAULT_WARN_REMAINING_PCT = 20


@dataclass
class SessionContext:
    """Context state of a single session's pane."""

    session: str
    pane: int
    is_agent: bool  # interactive Claude session (vs daemon / bare shell)
    remaining_pct: int | None  # % context HEADROOM left; None when no bar
    model: str | None
    flagged: bool  # remaining_pct <= warn threshold (agents only)
    note: str  # human-readable one-liner

    def to_dict(self) -> dict:
        return asdict(self)


def parse_context_bar(visible: str) -> int | None:
    """Remaining-context % from a Claude Code status bar, or None.

    None means no bar on screen — a daemon pane, a busy/starting render, or a
    pane that simply isn't a Claude conversation.
    """
    clean = _ANSI.sub("", visible)
    m = _CONTEXT_BAR_RE.search(clean)
    if not m:
        return None
    pct = int(m.group(1))
    return pct if 0 <= pct <= 100 else None


def parse_model(visible: str) -> str | None:
    clean = _ANSI.sub("", visible)
    m = _MODEL_RE.search(clean)
    return m.group(1).lower() if m else None


def _pane_command(session: str, pane: int) -> str:
    try:
        result = _tmux(
            ["display", "-t", f"{session}.{pane}", "-p", "#{pane_current_command}"]
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _is_agent_command(command: str) -> bool:
    return bool(_AGENT_COMMAND_RE.match(command.strip()))


def session_context(
    session: str, pane: int = 0, warn_threshold: int | None = None
) -> SessionContext:
    """Read one session's context state from its pane.

    ``warn_threshold`` is the *remaining* % at/below which the session is
    flagged (default :data:`DEFAULT_WARN_REMAINING_PCT`). A daemon / non-agent
    pane is surfaced as ``is_agent=False`` and never flagged.
    """
    threshold = warn_threshold if warn_threshold is not None else _warn_threshold()
    command = _pane_command(session, pane)
    is_agent = _is_agent_command(command)
    visible = _capture(f"{session}.{pane}")
    remaining = parse_context_bar(visible)
    model = parse_model(visible) if remaining is not None else None

    if remaining is None:
        flagged = False
        note = (
            f"daemon / non-agent ({command or 'unknown'}) — no context bar"
            if not is_agent
            else "agent pane but no bar visible (busy render or starting up)"
        )
    else:
        flagged = remaining <= threshold
        note = f"{remaining}% context remaining" + (
            f" — LOW (<= {threshold}% warn threshold)" if flagged else ""
        )

    return SessionContext(
        session=session,
        pane=pane,
        is_agent=is_agent,
        remaining_pct=remaining,
        model=model,
        flagged=flagged,
        note=note,
    )


def _list_local_sessions() -> list[str]:
    try:
        result = _tmux(["list-sessions", "-F", "#{session_name}"])
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.strip().splitlines() if s]


def all_session_contexts(
    warn_threshold: int | None = None,
) -> list[SessionContext]:
    """Context state for every local session (pane 0), sorted bloated-first.

    Pane 0 is the orchestrator / main conversation — the long-lived surface
    that accumulates. Daemons fall out as ``is_agent=False`` and sort last.
    """
    threshold = warn_threshold if warn_threshold is not None else _warn_threshold()
    contexts = [
        session_context(name, 0, threshold) for name in _list_local_sessions()
    ]
    # Bloated agents first (lowest remaining), then other agents, then daemons.
    contexts.sort(
        key=lambda c: (
            not c.is_agent,
            c.remaining_pct if c.remaining_pct is not None else 999,
            c.session,
        )
    )
    return contexts


def _warn_threshold() -> int:
    """The remaining-% warn threshold from config (best-effort default)."""
    try:
        from .config import get_config

        return int(get_config().session_context.warn_remaining_pct)
    except Exception:
        return DEFAULT_WARN_REMAINING_PCT
