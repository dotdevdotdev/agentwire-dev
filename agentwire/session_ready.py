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


def send_to_session(session: str, message: str, pane_index: int = 0) -> None:
    """Inject a message into a session's pane (pane 0 by default)."""
    from agentwire import pane_manager

    pane_manager.send_to_target(f"{session}.{pane_index}", message, enter=True)


def capture_session(session: str, lines: int = 60, pane_index: int = 0) -> str:
    from agentwire import pane_manager

    return pane_manager.capture_pane(session, pane_index, lines=lines)


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

    *pane_index* targets a worker pane (1+) instead of the session's pane 0.
    """
    for _ in range(retries + 1):
        send_to_session(session, message, pane_index=pane_index)
        time.sleep(settle)
        try:
            capture = capture_session(session, pane_index=pane_index)
            if marker is not None:
                if marker in capture:
                    return True
            elif message_visible(capture, message):
                return True
        except Exception:
            pass
    return False
