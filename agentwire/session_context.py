"""Session context observability + auto-management (issue #442).

Phase 0 made context bloat *visible* and queryable (parse the bar, flag low
sessions, expose via CLI/MCP). Phase 1 adds the *deterministic, zero-LLM*
auto-action: opted-in sessions whose remaining context crosses the warn
threshold get ``/clear`` (stateless service sessions) or ``/compact`` while
they sit idle at an empty prompt. See :func:`tick` and :func:`resolve_policy`.

**Opt-in only.** A session is auto-managed solely when it carries an explicit
``context_policy`` (``clear`` | ``compact``); the default everywhere is
``none``. Bundled stateless service sessions are default-on via their
service-registry entry — the ``agentwire-notifications`` idle-nag bridge is the
canonical case (it accumulates ~1440 prompts/day and needs none of its backlog).
The watchdog never touches a session without a policy.

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
from pathlib import Path

from .usage_limit import _capture, _tmux
from .utils.event_log import append_event

# The context bar: a bracketed run of block-element glyphs (U+2580–U+259F
# covers full/partial/empty blocks) followed by "NN%". ANSI is stripped first.
#
# HARDENING (Phase 1, issue #442) — because auto-``/clear`` now keys off the
# parsed value, a false read must never be able to trigger an action. Two
# anchors over the naive "any [blocks] N%":
#   1. **Trailing token** — the bar+pct must be the last thing on its line
#      (``[ \t]*$`` with re.MULTILINE). The real Claude footer renders the bar
#      as the trailing token; an inline ``CPU [███░░] 50% busy`` mid-line is
#      rejected.
#   2. **Longest glyph-run wins** — :func:`parse_context_bar` scans every
#      matching line and returns the percentage of the *widest* bar, so when a
#      worker pane renders its own short progress bar alongside the real (wide)
#      Claude footer, the footer dominates rather than the leftmost match.
# The lookahead requires ≥1 block glyph so an all-spaces ``[   ]`` never matches.
# Belt-and-suspenders: the auto-action sweep also gates on an idle, empty Claude
# input box (:func:`prompt_router.prompt_is_empty`), which a pane merely drawing
# a download meter does not present.
_ANSI = re.compile(r"\x1b\[[0-9;]*m|\x1b\].*?\x07")
_CONTEXT_BAR_RE = re.compile(
    r"\[(?=[^\]\n]*[▀-▟])([▀-▟ ]+)\][ \t]*(\d{1,3})[ \t]*%[ \t]*$",
    re.MULTILINE,
)
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
    pane that simply isn't a Claude conversation. When several bars are present
    (e.g. a worker pane drawing its own progress bar under the Claude footer),
    the widest one wins — see :data:`_CONTEXT_BAR_RE` hardening notes.
    """
    clean = _ANSI.sub("", visible)
    best_pct: int | None = None
    best_run = -1
    for m in _CONTEXT_BAR_RE.finditer(clean):
        pct = int(m.group(2))
        if not 0 <= pct <= 100:
            continue
        run = len(m.group(1))
        if run > best_run:
            best_run, best_pct = run, pct
    return best_pct


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


def _warn_threshold() -> int:
    """The remaining-% warn threshold from config (best-effort default)."""
    try:
        from .config import get_config

        return int(get_config().session_context.warn_remaining_pct)
    except Exception:
        return DEFAULT_WARN_REMAINING_PCT


# =============================================================================
# Phase 1 — opt-in auto-management (clear / compact)
# =============================================================================

POLICY_NONE = "none"
POLICY_CLEAR = "clear"
POLICY_COMPACT = "compact"
VALID_POLICIES = (POLICY_NONE, POLICY_CLEAR, POLICY_COMPACT)

# The slash command each acting policy sends to the session.
_POLICY_COMMAND = {POLICY_CLEAR: "/clear", POLICY_COMPACT: "/compact"}

EVENTS_FILE = Path.home() / ".agentwire" / "session-context-events.jsonl"


def _log_event(event: str, **fields) -> None:
    """Append one auto-action audit record (best-effort, never raises)."""
    from datetime import datetime, timezone

    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    append_event(EVENTS_FILE, record)


def resolve_policy(session: str, cfg=None) -> str:
    """The context-management policy for *session* — ``clear`` | ``compact`` | ``none``.

    Resolution, first match wins:
      1. Explicit per-session override in config ``session_context.policies``
         (a ``{name: policy}`` map — for arbitrary sessions like councils/anchors).
      2. The session's service-registry entry ``context_policy`` (bundled
         stateless services are default-on here — notifications => ``clear``).
      3. ``none`` — never auto-managed.

    An unknown/invalid value is treated as ``none`` (fail safe — never act).
    """
    if cfg is None:
        try:
            from .config import get_config

            cfg = get_config()
        except Exception:
            return POLICY_NONE

    policies = getattr(cfg.session_context, "policies", {}) or {}
    override = policies.get(session)
    if override in VALID_POLICIES:
        return override

    try:
        from . import services

        for svc in services.registry(cfg):
            if svc.name == session:
                pol = getattr(svc, "context_policy", POLICY_NONE)
                return pol if pol in VALID_POLICIES else POLICY_NONE
    except Exception:
        pass

    return POLICY_NONE


def act_on_session(session: str, policy: str, threshold: int | None = None) -> dict:
    """Evaluate one opted-in session and ``/clear`` | ``/compact`` if warranted.

    Returns a result dict (always; never raises). ``acted`` is True **only when
    the command was a verified delivery** — the paste is routed through
    :func:`prompt_router.safe_deliver`, so a guarded refusal or a silent paste
    failure is logged honestly as NOT acted (and retried next tick), never
    assumed sent.

    Delivery mirrors the sibling inbox drain (:func:`inbox.flush_session`):

    1. **Collision guard first** — :func:`prompt_router.prompt_is_empty` must
       see a clean, empty Claude box (it returns False for a live dialog, a
       busy render, a human's half-typed draft, or any unparseable screen). This
       also closes the TOCTOU window as far as a *human* draft goes: we never
       paste over uncommitted text.
    2. **safe_deliver** — adds the parked / non-agent / live-menu refusals AND a
       verified paste (:func:`session_ready.send_verified`, marker = the command
       text, which Claude Code echoes as ``❯ /clear`` after the slash command
       runs). A turn that races in between the two steps either leaves the box
       non-empty (→ refused) or shows a live render (→ refused), so the clear
       can't land mid-stream.
    """
    if policy not in _POLICY_COMMAND:
        return {"session": session, "acted": False, "skipped": "no_policy"}

    threshold = threshold if threshold is not None else _warn_threshold()
    ctx = session_context(session, 0, threshold)

    if not ctx.is_agent or ctx.remaining_pct is None:
        return {"session": session, "acted": False, "skipped": "no_bar"}
    if not ctx.flagged:
        return {
            "session": session, "acted": False, "skipped": "above_threshold",
            "remaining_pct": ctx.remaining_pct,
        }

    from . import prompt_router

    # Collision guard before anything is pasted (mirrors inbox.flush_session).
    if not prompt_router.prompt_is_empty(session, 0):
        _log_event(
            "deferred", session=session, policy=policy,
            remaining_pct=ctx.remaining_pct, reason="box_not_empty",
        )
        return {
            "session": session, "acted": False, "deferred": "box_not_empty",
            "remaining_pct": ctx.remaining_pct,
        }

    command = _POLICY_COMMAND[policy]
    try:
        delivered, reason = prompt_router.safe_deliver(session, 0, command)
    except Exception as exc:  # delivery must never break the watchdog
        _log_event(
            "send_failed", session=session, policy=policy,
            remaining_pct=ctx.remaining_pct, error=str(exc),
        )
        return {"session": session, "acted": False, "deferred": "send_failed"}

    if not delivered:
        # Guarded refusal (parked / not-agent / live-menu) or an unverified
        # paste — logged honestly as not acted, retried next tick.
        _log_event(
            "deferred", session=session, policy=policy,
            remaining_pct=ctx.remaining_pct, reason=reason,
        )
        return {
            "session": session, "acted": False, "deferred": reason,
            "remaining_pct": ctx.remaining_pct,
        }

    _log_event(
        "acted", session=session, policy=policy, command=command,
        remaining_pct=ctx.remaining_pct, threshold=threshold,
    )
    return {
        "session": session, "acted": True, "policy": policy, "command": command,
        "remaining_pct": ctx.remaining_pct,
    }


def tick() -> dict:
    """One auto-context-management pass over opted-in sessions.

    The 4th sweep on ``agentwire limits tick`` (after usage-limit park, prompt
    routing, and the inbox drain). For every local session carrying a
    ``clear``/``compact`` policy whose remaining context has crossed the warn
    threshold *and* which is idle at an empty prompt, send ``/clear`` |
    ``/compact``. Deterministic, zero-LLM. Never raises.

    A session that defers (busy / parked / mid-turn) is simply retried next
    tick; a session above threshold is left alone. No cooldown is needed — a
    successful ``/clear`` resets the bar above the threshold, so it won't be
    re-flagged, and a silently-failed one *should* be retried.
    """
    try:
        from .config import get_config

        cfg = get_config()
    except Exception:
        return {"skipped": "no_config"}

    if not getattr(cfg.session_context, "auto_enabled", True):
        return {"skipped": "disabled"}

    threshold = int(getattr(cfg.session_context, "warn_remaining_pct", DEFAULT_WARN_REMAINING_PCT))

    acted, deferred = [], []
    for session in _list_local_sessions():
        policy = resolve_policy(session, cfg)
        if policy not in _POLICY_COMMAND:
            continue
        result = act_on_session(session, policy, threshold)
        if result.get("acted"):
            acted.append(result)
        elif result.get("deferred"):
            deferred.append(result)
    return {"acted": acted, "deferred": deferred}
