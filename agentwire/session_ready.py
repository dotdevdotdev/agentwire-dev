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

# Adaptive submit tuning (replaces the old fixed pre-Enter sleep). Delivery is a
# two-phase poll: wait for the paste to land in the input box, then press Enter
# and wait for the box to clear — re-pressing Enter a bounded number of times if
# the first keystroke is swallowed under load (or a large paste needs a second
# Enter to dismiss its ``[Pasted text]`` banner before submitting).
POLL_INTERVAL = 0.15
# Max wait for a paste to appear in the input box before giving up on this try.
# Generous because under host load tmux ``capture-pane`` itself lags and a large
# paste renders slowly — a tight budget here is the #1 cause of "the text landed
# but delivery reported failure" on a bogged-down machine.
LAND_TIMEOUT = 8.0
# Max wait, after a single Enter, for the box to clear (the keystroke to
# register). Per-press, not the whole submit phase — see SUBMIT_BUDGET.
SUBMIT_TIMEOUT = 4.0
# Wall-clock ceiling on the whole press-Enter-and-confirm phase. Re-pressing is
# driven by this deadline rather than a fixed count so a laggy box is waited out
# (each idle Enter on an already-submitted/empty box is a harmless no-op), but a
# genuinely wedged session still fails in bounded time instead of hanging.
SUBMIT_BUDGET = 20.0
# Floor on re-presses regardless of how fast the budget burns (slow snapshots
# can eat the wall-clock before we've pressed Enter enough times).
MIN_ENTER_ATTEMPTS = 4

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


def paste_no_enter(session: str, message: str, pane_index: int = 0) -> None:
    """Paste a message into a session's pane WITHOUT pressing Enter.

    Decoupling the paste from the Enter is the whole point of the adaptive
    submit: it lets us poll the input box and confirm the text actually landed
    before we commit it. Pressing Enter in the same breath (the old behavior)
    fires the keystroke on a fixed delay that, under load, lands before the
    paste does — Enter is swallowed and the message sits unsent.
    """
    from agentwire import pane_manager

    pane_manager.send_to_target(f"{session}.{pane_index}", message, enter=False)


def press_enter(session: str, pane_index: int = 0) -> None:
    """Send a single Enter keystroke to a session's pane."""
    from agentwire import pane_manager

    pane_manager.run_command(
        ["tmux", "send-keys", "-t", f"{session}.{pane_index}", "Enter"], timeout=5
    )


def capture_session(session: str, lines: int = 60, pane_index: int = 0) -> str:
    from agentwire import pane_manager

    return pane_manager.capture_pane(session, pane_index, lines=lines)


def pane_shows_activity(capture: str) -> bool:
    """Does the pane show Claude actively working (spinner / tokens / output)?"""
    lowered = capture.lower()
    return any(marker.lower() in lowered for marker in ACTIVITY_MARKERS)


def input_box(capture: str) -> "str | None":
    """The input-box content for *capture*, or None if it can't be parsed.

    Thin pass-through to :func:`prompt_router.input_box_content` so the verified
    submit can reason about exactly one thing: is our pasted text still sitting
    unsent in the box, or has it cleared?
    """
    from agentwire import prompt_router

    return prompt_router.input_box_content(capture)


def text_landed(capture: str, message: str) -> bool:
    """Has *message* landed in the input box, ready to submit?

    True iff the box is parseable and shows the message fragment (or Claude's
    ``[Pasted text …]`` placeholder for a large paste).
    """
    box = input_box(capture)
    if box is None:
        return False
    return message_visible(box, message)


def submitted(capture: str, message: str, marker: str | None = None) -> bool:
    """Did the paste actually get *submitted* (Enter registered)?

    The discriminating signal is the input box: if our text still sits in a
    readable box, it is NOT submitted — this is the exact false-positive the old
    check made (treating "text visible anywhere" as success while it sat unsent).

    Once the box no longer holds our text, we confirm with positive evidence:
      - *marker* given → the marker line is present in scrollback.
      - empty box → the pane shows activity OR the message scrolled into history
        (an empty box with neither is the idle/never-received vanish case).
      - unparseable box (tool output / dialog covering it) → activity AND the
        message visible in scrollback.
    """
    box = input_box(capture)
    if box is not None and message_visible(box, message):
        return False  # still sitting unsent in the box
    if marker is not None:
        return marker in capture
    if box == "":
        return pane_shows_activity(capture) or message_visible(capture, message)
    return pane_shows_activity(capture) and message_visible(capture, message)


def _poll(predicate, timeout: float) -> bool:
    """Poll *predicate* until it returns truthy or *timeout* elapses."""
    deadline = time.time() + timeout
    while True:
        try:
            if predicate():
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


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


def _snapshot(session: str, pane_index: int) -> str:
    return capture_session(
        session, lines=VERIFY_SCROLLBACK_LINES, pane_index=pane_index
    )


def _deliver_once(
    session: str, message: str, marker: str | None, pane_index: int
) -> bool:
    """One adaptive paste→land→submit attempt. True iff the message submitted."""
    paste_no_enter(session, message, pane_index=pane_index)

    # Phase 1 — wait for the paste to actually land in the input box (or for a
    # very fast bypass agent to have already consumed AND submitted it). If it
    # never lands, the paste vanished — let the caller retry the whole send.
    def landed_or_done() -> bool:
        cap = _snapshot(session, pane_index)
        return submitted(cap, message, marker) or text_landed(cap, message)

    if not _poll(landed_or_done, LAND_TIMEOUT):
        return False

    # Phase 2 — press Enter and confirm the box cleared. Re-press is driven by a
    # wall-clock budget, not a fixed count: a single Enter can be swallowed under
    # load, a large paste needs a second Enter (dismiss the ``[Pasted text]``
    # banner, then submit), and on a bogged-down host the box renders slowly. We
    # keep pressing until the box clears or SUBMIT_BUDGET elapses — an idle Enter
    # on an already-submitted/empty box is a harmless no-op, so over-pressing is
    # safe, while a tight count would give up before a laggy box ever caught up.
    deadline = time.time() + SUBMIT_BUDGET
    attempts = 0
    while True:
        if submitted(_snapshot(session, pane_index), message, marker):
            return True
        press_enter(session, pane_index=pane_index)
        attempts += 1
        if _poll(
            lambda: submitted(_snapshot(session, pane_index), message, marker),
            SUBMIT_TIMEOUT,
        ):
            return True
        if attempts >= MIN_ENTER_ATTEMPTS and time.time() >= deadline:
            return False


def send_verified(
    session: str,
    message: str,
    marker: str | None = None,
    retries: int = 1,
    settle: float = 2.0,
    pane_index: int = 0,
) -> bool:
    """Paste a message and verify it was actually *submitted* — adaptively.

    Replaces the old blind fixed-delay-then-Enter with a verified, two-phase
    submit (the same compare-and-send rigor ``agentwire prompts answer`` uses):

    1. Paste WITHOUT Enter, then poll the input box (bounded, ``LAND_TIMEOUT``)
       until the pasted text is actually visible there — never press Enter on a
       box that hasn't received the paste yet.
    2. Press Enter and poll (bounded, ``SUBMIT_TIMEOUT``) until the box clears.
       If a keystroke is swallowed, re-press up to ``MAX_ENTER_ATTEMPTS`` times.

    Returns True once submission is confirmed. Returns False — a hard, surfaced
    failure the caller must handle — only after exhausting *retries* whole-send
    attempts; it NEVER silently leaves text sitting unsent.

    *marker* uses council's explicit-marker scrollback check; otherwise the
    message's own fragment is used. *settle* is retained for signature
    stability — the adaptive poll supersedes the old fixed pre-Enter sleep.
    *pane_index* targets a worker pane (1+) instead of the session's pane 0.
    """
    del settle  # superseded by adaptive polling; kept for signature stability
    for _ in range(retries + 1):
        try:
            if _deliver_once(session, message, marker, pane_index):
                return True
        except Exception:
            pass
    return False
