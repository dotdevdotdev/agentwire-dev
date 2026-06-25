"""Agent readiness detection and verified message delivery.

Consolidates the readiness/verification primitives that grew up separately
in worktree dispatch (wait_for_session_ready) and council (send_verified): a
freshly-booted Claude session renders its banner before its input handler
is wired, so a paste in that window vanishes silently. Every subsystem
that injects a first prompt into a new session — council,
`agentwire send --wait-ready`, `agentwire new --first-message` — routes
through here.
"""

import time

# How far back into scrollback to look when verifying delivery. A fast bypass
# agent consumes the paste, submits it, and emits tool output within the settle
# window, scrolling the ``[Pasted text …]`` placeholder / first-line fragment
# past the visible tail. Verification reads scrollback (not just the visible
# 60 lines) so a submitted prompt that scrolled up is still found.
VERIFY_SCROLLBACK_LINES = 200

# Delay for the early snapshot — catches the placeholder/fragment before a fast
# agent scrolls it away. The remaining settle gives a slow agent time to render.
EARLY_SETTLE = 0.15

# Substrings that mean "Claude is actively working" — a spinner footer, the
# token counter, the esc-to-interrupt hint, or tool-output glyphs. A
# submitted-and-working agent is the success case the old check mistook for a
# vanished paste.
ACTIVITY_MARKERS = (
    "esc to interrupt",
    "esc-to-interrupt",
    "tokens",
    "⎿",  # tool-result indent
    "⏺",  # tool-call bullet
    "✶",  # spinner glyphs Claude cycles while thinking
    "✻",
    "✽",
    "✢",
)


def send_to_session(session: str, message: str, pane_index: int = 0) -> None:
    """Inject a message into a session's pane (pane 0 by default)."""
    from agentwire import pane_manager

    pane_manager.send_to_target(f"{session}.{pane_index}", message, enter=True)


def capture_session(session: str, lines: int = 60, pane_index: int = 0) -> str:
    from agentwire import pane_manager

    return pane_manager.capture_pane(session, pane_index, lines=lines)


def pane_shows_activity(capture: str) -> bool:
    """Does the pane show Claude actively working (spinner / tokens / output)?"""
    lowered = capture.lower()
    return any(marker.lower() in lowered for marker in ACTIVITY_MARKERS)


def consumed_and_working(session: str, capture: str, pane_index: int = 0) -> bool:
    """Positive "consumed" signal: input box empty AND pane shows activity.

    A submitted prompt leaves the input box empty while the agent works. We
    require *both* — empty alone is also the idle/never-received state, so an
    empty box with no activity is NOT treated as delivered (guards the genuine
    vanish case against a false positive).
    """
    if not pane_shows_activity(capture):
        return False
    from agentwire import prompt_router

    try:
        return prompt_router.prompt_is_empty(session, pane_index)
    except Exception:
        return False


def wait_for_session_ready(
    session_full_name: str, timeout: float = 30.0, pane_index: int = 0
) -> bool:
    """Poll a session's pane until Claude is fully ready to accept input.

    Two-phase wait:

    1. Detect the Claude prompt banner (``❯`` or ``Bypassing Permissions``).
    2. Wait until the screen is *stable* — two consecutive 500ms snapshots
       are identical. This catches the case where Claude has rendered its
       banner but is still wiring its input handler. A premature paste at
       that point gets fragmented into multiple ``[Pasted text +N]`` chunks
       and Enter keys land in a state where Claude can't process them —
       the prompt sits in the input box, never submitted.

    Also auto-accepts the first-time "trust this folder" prompt, which a
    fresh project directory always triggers (and which contains neither
    banner string, so it would otherwise stall the wait until timeout).

    Returns True once the screen is stable after banner-detection. False on
    timeout.
    """
    from agentwire import pane_manager

    deadline = time.time() + timeout
    banner_seen = False
    trust_accepted = False
    last_snapshot: str | None = None

    while time.time() < deadline:
        try:
            out = pane_manager.capture_pane(session_full_name, pane_index, lines=20)
        except Exception:
            time.sleep(0.5)
            last_snapshot = None
            continue

        if not banner_seen:
            lowered = out.lower()
            if not trust_accepted and (
                "trust this folder" in lowered or "enter to confirm" in lowered
            ):
                pane_manager.run_command(
                    ["tmux", "send-keys", "-t",
                     f"{session_full_name}.{pane_index}", "Enter"],
                    timeout=5,
                )
                trust_accepted = True
                time.sleep(2.0)
                continue
            if "❯" in out or "Bypassing Permissions" in out:
                banner_seen = True
                last_snapshot = out
            time.sleep(0.5)
            continue

        # Banner is up; wait for two consecutive identical snapshots.
        if last_snapshot is not None and out == last_snapshot:
            return True
        last_snapshot = out
        time.sleep(0.5)

    return False


def derive_check_fragment(message: str, length: int = 32) -> str:
    """A distinctive fragment of *message* to look for in pane output.

    First non-empty line, first *length* characters.
    """
    for line in message.splitlines():
        line = line.strip()
        if line:
            return line[:length]
    return ""


def message_visible(capture: str, message: str) -> bool:
    """Did *message* land in the pane?

    Whitespace-normalized substring check — tmux ``capture-pane`` wraps long
    lines at pane width mid-word, so both sides are compared with all
    whitespace stripped. Large multiline pastes may render only as Claude's
    ``[Pasted text #N +M lines]`` placeholder, which counts as landed (the
    failure mode being defended against is the paste vanishing entirely).
    """
    frag = derive_check_fragment(message)
    if frag and "".join(frag.split()) in "".join(capture.split()):
        return True
    return "[Pasted text" in capture


def send_verified(
    session: str,
    message: str,
    marker: str | None = None,
    retries: int = 1,
    settle: float = 2.0,
    pane_index: int = 0,
) -> bool:
    """Send a message and verify it actually landed in the pane.

    After sending, confirm the message is visible in the pane — via
    *marker* if given (council's explicit-marker pattern), otherwise via
    :func:`message_visible` on the message itself. Retry once if not.

    Verification reads *scrollback* (not just the visible tail) and snapshots
    twice — once ~150ms after the paste (to catch the ``[Pasted text …]``
    placeholder / fragment before a fast agent scrolls it away) and once after
    *settle*. A hit at *either* time counts. For the markerless case a positive
    "consumed" signal (input box went empty AND the pane shows activity) also
    counts — a submitted-and-working agent is delivery, not failure. The
    genuine vanish case (empty box, no activity, nothing in scrollback) still
    returns False.

    *pane_index* targets a worker pane (1+) instead of the session's pane 0.
    """

    def confirmed() -> bool:
        capture = capture_session(
            session, lines=VERIFY_SCROLLBACK_LINES, pane_index=pane_index
        )
        if marker is not None:
            return marker in capture
        if message_visible(capture, message):
            return True
        return consumed_and_working(session, capture, pane_index=pane_index)

    for _ in range(retries + 1):
        send_to_session(session, message, pane_index=pane_index)
        # Early snapshot — placeholder/fragment still on screen before the
        # agent consumes and scrolls it away.
        time.sleep(EARLY_SETTLE)
        try:
            if confirmed():
                return True
        except Exception:
            pass
        # Late snapshot — gives a slow agent time to render the paste.
        remaining = settle - EARLY_SETTLE
        if remaining > 0:
            time.sleep(remaining)
        try:
            if confirmed():
                return True
        except Exception:
            pass
    return False
