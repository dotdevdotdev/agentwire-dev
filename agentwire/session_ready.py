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

# Readiness hardening (#695). Two identical 500ms frames proved too weak a
# stability signal: a render pause while a notice panel / effort banner loads
# (or model-init on a high effort tier) yields an identical pair with the input
# handler still unwired, and the premature paste fragments + its Enters are
# swallowed. Require a longer identical run, then demand POSITIVE proof the
# handler consumes keystrokes: type a probe char, watch it render in the input
# box, erase it. Only then is the real paste allowed.
READY_STABLE_SNAPSHOTS = 3  # consecutive identical frames (~1s of stability)
PROBE_CHAR = "."
# Per-probe wait for the typed char to render in the box. Short: an unwired
# handler swallows (or buffers) the keystroke, and the outer loop re-sends.
PROBE_APPEAR_TIMEOUT = 3.0
# Wait for backspace(s) to clear the rendered probe(s) out of the box.
PROBE_ERASE_TIMEOUT = 3.0

# Seed-failure recovery (#695): bounded attempts at clearing a partial paste
# out of the input box (Escape clears Claude Code's current input, including a
# large paste's ``[Pasted text]`` chip), and the per-attempt confirm budget.
CLEAR_BOX_ATTEMPTS = 3
CLEAR_BOX_TIMEOUT = 3.0
# Escape is a no-op on a draft taller than the box's visible region (#851 —
# measured: 3 Escapes, 9.4s, box unchanged), so clearing escalates to erasing
# the draft a character at a time. Backspaces are sent in chunks (one
# ``send-keys`` call each) and the box is re-checked between chunks, so a short
# draft costs one round and a tall one is bounded at CHUNK * ROUNDS characters.
ERASE_CHUNK = 200
ERASE_ROUNDS = 12

# Minimum box-content length that may be accepted as a *window* of a longer
# message (#851). A draft taller than the input box's visible region renders
# only part of itself, so full-message identity can never hold; the fragment
# test fills that gap. The floor keeps the false-positive direction closed: a
# short foreign draft that happens to be a substring of our message
# ("ok", "yes") must never read as our paste landing. Real scrolled windows are
# hundreds of characters (~460 on an 80x24 pane), so this is far below any
# genuine window and far above any plausible accidental collision.
MIN_BOX_FRAGMENT = 80

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

    True iff the box is parseable and holds the message — whole, as Claude's
    ``[Pasted text …]`` placeholder, or as the rendered window of a draft too
    tall to display at once (see :func:`box_shows_message`).
    """
    box = input_box(capture)
    if box is None:
        return False
    return box_shows_message(box, message)


def submit_confirmed(capture: str, message: str) -> bool:
    """Phase-2 confirm: did the (already-landed) paste actually *submit*? (#621)

    By Phase 2 the text has been proven to land in the input box, so the
    submission signal is: **a parseable box no longer holds our text** — not
    even as the rendered window of a too-tall draft (#851), or a message
    bigger than the box would read as submitted the instant it landed. An
    empty box, or a box now showing a different/next prompt, both count —
    including the submitted-while-generating case, where the text queues
    invisibly and never echoes until the turn ends. While a multi-line paste's
    ``[Pasted text …]`` placeholder (or the expanded text) still occupies the
    box, it reads as not-yet-submitted, so the caller keeps pressing Enter
    (dismiss-then-submit).

    An UNPARSEABLE box (mid-redraw frame, tool output / dialog covering it) is
    never confirmed (#698). The old fallback accepted activity glyphs, a
    caller marker, or the message visible in the raw capture — all of which a
    stuck paste satisfies, because activity glyphs sit in every agent pane's
    scrollback, the marker is part of the pasted text, and the raw capture
    *includes* the very box we failed to parse. One garbled snapshot during
    the confirm poll was enough to log ``delivered`` for a message still
    sitting unsent (the 2026-07-04 12:40 loss, #698). Proving the text is
    *outside* the box requires the same box parse that just failed, so nothing
    can be proven: not confirmed. The caller keeps pressing Enter (a no-op on
    an already-submitted box) until a parseable frame decides, and a genuinely
    landed-but-scrolled message is consumed by the next drain tick's box-aware
    dedup instead — a duplicate press is recoverable, a false ``delivered``
    deletes the message.
    """
    box = input_box(capture)
    if box is None:
        return False
    return not box_shows_message(box, message)


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


def _box_is_probe(box: "str | None") -> bool:
    """Is the input box showing nothing but our probe char(s)?

    Swallowed-then-buffered keystrokes can pile up, so any run of probe chars
    counts. Ghost/placeholder hint text (a sentence) never matches, and a
    leftover draft never matches — both correctly read as "probe not proven".
    """
    if not box:
        return False
    s = "".join(box.split())
    return bool(s) and set(s) == {PROBE_CHAR}


def _probe_input_handler(session: str, pane_index: int, deadline: float) -> bool:
    """Positive proof the input handler consumes keystrokes (#695).

    Types PROBE_CHAR, polls the input box until the char actually renders,
    then erases it and confirms the box moved on. A banner-up-but-unwired
    session swallows (or buffers) the keystroke, so the probe never confirms
    and we re-send until *deadline* — failing safe instead of pasting into a
    session that would fragment the paste and swallow its Enters.
    """
    from agentwire import pane_manager

    target = f"{session}.{pane_index}"

    def box() -> "str | None":
        return input_box(capture_session(session, pane_index=pane_index))

    while time.time() < deadline:
        pane_manager.run_command(
            ["tmux", "send-keys", "-t", target, "-l", PROBE_CHAR], timeout=5
        )
        appear_budget = min(
            PROBE_APPEAR_TIMEOUT, max(deadline - time.time(), POLL_INTERVAL)
        )
        if not _poll(lambda: _box_is_probe(box()), appear_budget):
            continue  # swallowed — handler not wired yet; re-send and re-check
        # Erase: buffered keystrokes may have piled up ("..."), so backspace
        # what's visible and confirm the box no longer shows probe chars.
        while time.time() < deadline:
            try:
                visible = box() or ""
            except Exception:
                visible = ""
            for _ in range(len("".join(visible.split())) or 1):
                pane_manager.run_command(
                    ["tmux", "send-keys", "-t", target, "BSpace"], timeout=5
                )
            if _poll(lambda: not _box_is_probe(box()), PROBE_ERASE_TIMEOUT):
                return True
        return False
    return False


def wait_for_session_ready(
    session_full_name: str, timeout: float = 30.0, pane_index: int = 0
) -> bool:
    """Poll a session's pane until Claude is fully ready to accept input.

    Three-phase wait:

    1. Detect the Claude prompt banner (``❯`` or ``Bypassing Permissions``).
    2. Wait until the screen is *stable* — READY_STABLE_SNAPSHOTS consecutive
       500ms snapshots are identical. Two identical frames proved too weak
       (#695): a render pause while a notice panel / effort banner loads
       yields an identical pair with the input handler still unwired.
    3. Probe the input handler (:func:`_probe_input_handler`): type a char,
       confirm it renders in the input box, erase it. Without this positive
       proof, a premature paste gets fragmented into multiple
       ``[Pasted text +N]`` chunks and Enter keys land in a state where
       Claude can't process them — the prompt sits in the input box, never
       submitted.

    Also auto-accepts the first-time "trust this folder" prompt, which a
    fresh project directory always triggers (and which contains neither
    banner string, so it would otherwise stall the wait until timeout).

    Returns True once the probe round-trips after banner + stability. False
    on timeout.
    """
    from agentwire import pane_manager

    deadline = time.time() + timeout
    banner_seen = False
    trust_accepted = False
    last_snapshot: str | None = None
    stable_count = 0

    while time.time() < deadline:
        try:
            out = pane_manager.capture_pane(session_full_name, pane_index, lines=20)
        except Exception:
            time.sleep(0.5)
            last_snapshot = None
            stable_count = 0
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

        # Banner is up; require READY_STABLE_SNAPSHOTS identical snapshots.
        if last_snapshot is not None and out == last_snapshot:
            stable_count += 1
        else:
            stable_count = 0
        last_snapshot = out
        if stable_count >= READY_STABLE_SNAPSHOTS - 1:
            return _probe_input_handler(session_full_name, pane_index, deadline)
        time.sleep(0.5)

    return False


def box_shows_message(box: str, message: str, allow_chip: bool = True) -> bool:
    """Does the input-box content *box* hold *message*?

    Three ways it can, in descending strength:

    1. **Whole-message identity** — the full whitespace-normalized message is
       visible in the box. Keyed on the FULL message, never a fixed-length
       prefix (#667): all worktree idle notifications share a long
       ``[NOTIFY from agentwire-dev-issue-…`` prefix, so a 32-char fragment
       false-matched a *pile* of other sessions' notifications sitting in the
       box — Phase 1 passed against text that wasn't ours, and Phase 2's
       "box no longer shows our text" could never come true. tmux
       ``capture-pane`` wraps long lines at pane width mid-word, so both sides
       are compared with all whitespace stripped.
    2. **The ``[Pasted text #N +M lines]`` placeholder**, which is all a large
       multiline paste renders (the failure mode being defended against is the
       paste vanishing entirely). Suppressed by ``allow_chip=False`` for
       callers reasoning about a box they have not pasted into yet, where a
       chip can only be somebody ELSE's draft.
    3. **A rendered window of the message** (#851). The input box has a bounded
       visible height and scrolls: a draft taller than that height renders only
       part of itself, so (1) is *impossible* for it and a long single-line
       message — exactly what programmatic ``--first-message`` callers produce
       — could never pass the landing gate, so Enter was never pressed and the
       prompt sat unsent. Accepted when the box content is a contiguous
       fragment of the message (in practice its tail: the cursor sits at the
       end), is at least ``MIN_BOX_FRAGMENT`` long, and is shorter than the
       message itself — a box holding MORE than we sent is somebody else's
       draft, not our window (that is what keeps the #667 pile out).
    """
    nb = "".join(box.split())
    nm = "".join(message.split())
    if not nm:
        return False
    if nb and nm in nb:
        return True
    if allow_chip and "[Pasted text" in box:
        return True
    if len(nb) < MIN_BOX_FRAGMENT or len(nb) >= len(nm):
        return False
    return nb in nm


def strip_input_box(capture: str) -> "str | None":
    """*capture* with the input-box region removed, or None if no box parses.

    The #689 building block: "on scrollback" must mean *outside the input box*.
    A pasted-but-unsubmitted message renders inside the box, which is part of
    every pane capture — matching against the raw capture reads a swallowed
    Enter as a delivered message. Mirrors ``prompt_router.input_box_content``'s
    parse (region between the last two horizontal rules, prompt glyph first);
    when the box can't be located we return None so callers stay conservative
    (can't prove the text is outside the box → treat as not on scrollback).

    Only lines strictly ABOVE the box's top border count as outside (#698).
    A mid-redraw frame can drop the box's bottom border, making the last two
    rules bracket some other region — in which case the real box (and the
    stuck text in it) sits BELOW the last rule, where the old version happily
    matched it as "scrollback". Below the top border nothing but the box and
    the footer hints ever renders, so excluding it all loses no real echo.
    """
    from agentwire import prompt_router

    clean = prompt_router.ANSI_PATTERN.sub("", capture)
    lines = clean.split("\n")
    rules = [i for i, ln in enumerate(lines) if prompt_router._is_rule_line(ln)]
    if len(rules) < 2:
        return None
    box = lines[rules[-2] + 1:rules[-1]]
    if not box:
        return None
    text = "\n".join(box).lstrip()
    if text[:1] not in prompt_router._PROMPT_GLYPHS:
        return None
    return "\n".join(lines[:rules[-2] + 1])


def message_on_scrollback(capture: str, rendered: str) -> bool:
    """Strict per-message scrollback check for idempotent redelivery (#621).

    Matches the message's **full** whitespace-normalized rendered line against
    the pane scrollback — NOT a fixed-length prefix. A short prefix collides on
    the shared ``[MSG from <sender> · <kind>] `` header (a worktree sender name
    alone can fill 32 chars), which would consume a *different* same-sender
    message that never actually delivered — silent loss of exactly the
    report-backs #621 protects. The full rendered line is distinct per distinct
    message. tmux wraps long lines at pane width, so both sides are
    whitespace-stripped before the substring test.

    The input-box region is EXCLUDED from the match (#689): a message pasted
    into the box whose Enter was swallowed is landed-but-unsubmitted, and
    counting it as "on scrollback" is exactly how the drain unlinked messages
    the recipient never received. When the box can't be parsed at all we return
    False — can't prove the text is outside it.

    Deliberately does NOT fall back to the generic ``"[Pasted text"`` placeholder
    (which would mark EVERY queued message visible). A message that scrolled past
    the window returns False — the safe direction: keep it pending and retry
    rather than silently drop it.
    """
    needle = "".join(rendered.split())
    if not needle:
        return False
    outside = strip_input_box(capture)
    if outside is None:
        return False
    return needle in "".join(outside.split())


def scrollback(session: str, pane_index: int = 0) -> str:
    """Public capture of a pane's verify-window scrollback (#621 dedup)."""
    return _snapshot(session, pane_index)


def _snapshot(session: str, pane_index: int) -> str:
    return capture_session(
        session, lines=VERIFY_SCROLLBACK_LINES, pane_index=pane_index
    )


def _deliver_once(
    session: str, message: str, marker: str | None, pane_index: int
) -> bool:
    """One adaptive paste→land→submit attempt. True iff the message submitted."""
    # Idempotent paste guard (#667): a previous attempt (an earlier whole-send
    # retry, or a prior tick) may have left this exact message sitting in the
    # box — landed but unsubmitted. Blindly pasting again doubles the draft
    # (the observed "issue-659 twice" pile).
    #
    # The short-circuit demands POSITIVE identity — our message in the box
    # (→ landed: skip the paste, retry only the submit) or on scrollback
    # outside the box (→ already submitted). "In the box" is window-aware
    # (#851): a draft taller than the box's visible region renders only part of
    # itself, so a full-message test can't recognize our OWN previous paste
    # either — which is how the whole-send retry re-pasted and left the child a
    # 1034-char box holding the 517-char instruction twice. Nothing weaker
    # counts before we have pasted:
    # NOT empty-box+activity (real agent panes show ⏺/⎿/spinner glyphs in
    # scrollback almost always — accepting that as "delivered" makes the msg
    # drain unlink queued messages that were never sent: silent deletion),
    # NOT the ``[Pasted text]`` placeholder (pre-paste it can only be someone
    # ELSE's draft — skipping our paste and pressing Enter would force-submit
    # a foreign draft; hence ``allow_chip=False``), and NOT the caller marker
    # (markers like council's are constant across messages, so a hit may be
    # the PREVIOUS message). When identity is not provable, paste again — the
    # existing full-line dedup makes duplicates recoverable; deletions aren't.
    already_landed = False
    try:
        cap = _snapshot(session, pane_index)
        box = input_box(cap)
        if box is not None:
            if box_shows_message(box, message, allow_chip=False):
                already_landed = True
            elif message_on_scrollback(cap, message):
                return True
        if not already_landed:
            # A live select-menu owns the pane's keystrokes: pasted text (or
            # the digits in it) could SELECT an option. safe_deliver gates the
            # first attempt, but the whole-send retry re-enters here after the
            # target's state may have changed (#698) — refuse, defer to the
            # next tick.
            from agentwire import prompt_router

            if prompt_router.screen_shows_live_menu(cap):
                return False
    except Exception:
        pass

    if not already_landed:
        paste_no_enter(session, message, pane_index=pane_index)

    # Phase 1 — wait for the paste to actually land in the input box, or for
    # positive proof it already submitted: the message (or the caller's
    # marker) echoed on scrollback OUTSIDE the box. Ambient evidence must
    # never end this wait (#698): the old check accepted "empty box +
    # activity glyphs" as already-submitted, which is what every agent pane
    # looks like in the instants BEFORE a paste renders — Phase 2 then
    # confirmed against the same still-empty box and reported ``delivered``
    # with zero Enter presses, seconds before the paste appeared and stuck
    # (the 2026-07-04 12:40 loss). If the paste never renders, it vanished —
    # let the caller retry the whole send.
    def landed_or_done() -> bool:
        cap = _snapshot(session, pane_index)
        if text_landed(cap, message):
            return True
        if message_on_scrollback(cap, message):
            return True
        return marker is not None and message_on_scrollback(cap, marker)

    if not _poll(landed_or_done, LAND_TIMEOUT):
        return False

    return _submit_phase(session, message, pane_index)


def _submit_phase(session: str, message: str, pane_index: int) -> bool:
    """Phase 2 — press Enter and confirm the box cleared. Enter-only, no paste.

    Re-press is driven by a wall-clock budget, not a fixed count: a single Enter
    can be swallowed under load, a large paste needs a second Enter (dismiss the
    ``[Pasted text]`` banner, then submit), and on a bogged-down host the box
    renders slowly. We keep pressing until the box clears or SUBMIT_BUDGET
    elapses — an idle Enter on an already-submitted/empty box is a harmless
    no-op, so over-pressing is safe, while a tight count would give up before a
    laggy box ever caught up.

    ``submit_confirmed`` only ever trusts a parseable box (#698), so every
    exit path — including the pre-press short-circuit — has positively seen a
    box that no longer holds our text. A live select-menu appearing on the
    target (a permission dialog the recipient's own work raised) aborts the
    loop instead of pressing Enter into it, which would CONFIRM the
    highlighted option; the delivery reports unverified and the next tick's
    ``safe_deliver`` dialog guard + box-aware dedup take it from there.
    """
    cap = _snapshot(session, pane_index)
    if submit_confirmed(cap, message):
        return True
    from agentwire import prompt_router

    deadline = time.time() + SUBMIT_BUDGET
    attempts = 0
    while True:
        if prompt_router.screen_shows_live_menu(cap):
            return False
        press_enter(session, pane_index=pane_index)
        attempts += 1
        if _poll(
            lambda: submit_confirmed(_snapshot(session, pane_index), message),
            SUBMIT_TIMEOUT,
        ):
            return True
        if attempts >= MIN_ENTER_ATTEMPTS and time.time() >= deadline:
            return False
        cap = _snapshot(session, pane_index)


def finish_submit(session: str, message: str, pane_index: int = 0) -> bool:
    """Enter-only submit retry for a message already sitting in the input box.

    The #689 healing primitive: when a prior delivery pasted the text but its
    Enter was swallowed, the fix is to press Enter again — NEVER to re-paste
    (the #621 idempotency dedup must keep holding; a second paste doubles the
    draft). Runs the same strict-then-press phase-2 loop as a fresh delivery.
    Returns True only once submission is confirmed; never raises.
    """
    try:
        return _submit_phase(session, message, pane_index)
    except Exception:
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
    2. Press Enter and poll (bounded, ``SUBMIT_TIMEOUT`` per press) until the
       box clears. If a keystroke is swallowed, re-press until ``SUBMIT_BUDGET``
       elapses (at least ``MIN_ENTER_ATTEMPTS`` presses).

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


def _erase_box_draft(session: str, pane_index: int = 0) -> bool:
    """Backspace an unsubmitted draft out of the input box (#851).

    The escalation behind :func:`clear_input_box` when Escape doesn't take.
    Backspace is the one key that acts on the draft regardless of how it
    renders — it deletes a large paste's ``[Pasted text]`` chip whole, and it
    joins lines at a line start, so a wrapped multi-line draft erases the same
    way a short one does.

    The draft's true length is unknowable from a box that shows only a window
    of it, so this sends ``ERASE_CHUNK`` backspaces at a time and re-checks
    emptiness between chunks: a short draft costs one round, a tall one is
    bounded at ``ERASE_CHUNK * ERASE_ROUNDS`` characters rather than looping
    forever. Over-pressing is harmless (backspace on an empty box is a no-op),
    but a live select-menu aborts before the first keystroke — the same
    protection Escape gets, because a decision belonging to the recipient's own
    work must not be edited by us.
    """
    from agentwire import pane_manager, prompt_router

    target = f"{session}.{pane_index}"

    def empty() -> bool:
        return prompt_router.prompt_is_empty(session, pane_index)

    for _ in range(ERASE_ROUNDS):
        try:
            if prompt_router.screen_shows_live_menu(_snapshot(session, pane_index)):
                return False
            pane_manager.run_command(
                ["tmux", "send-keys", "-t", target] + ["BSpace"] * ERASE_CHUNK,
                timeout=15,
            )
            if _poll(empty, CLEAR_BOX_TIMEOUT):
                return True
        except Exception:
            return False
    return False


def clear_input_box(session: str, pane_index: int = 0) -> bool:
    """Best-effort: clear whatever sits unsubmitted in the input box (#695).

    A failed seed can leave a partial paste in the box, which (a) confuses a
    human attaching to the session and (b) blocks the msg-inbox fallback
    forever — the drain only pastes into an EMPTY box. Sends Escape (Claude
    Code: clears the current input, including a large paste's ``[Pasted
    text]`` chip) and confirms emptiness with the drain's own SGR-aware gate
    (``prompt_router.prompt_is_empty``), so "cleared" here means exactly
    "the drain will deliver". Returns True iff the box ends up empty.

    Escape alone is not enough (#851). A draft taller than the box's visible
    region ignores it — measured on the wedged pane: 3 Escapes, 9.4s, box
    unchanged, and ``C-u`` (kill-to-line-start) is equally inert on a draft
    that wrapped onto many lines. That made ``"inbox_stuck"`` terminal: the
    durable copy was queued but the drain's empty-box gate stayed blocked by
    the very draft it was meant to replace, so the session never received the
    prompt by ANY route until a human pressed Enter. So each Escape that
    doesn't take escalates to :func:`_erase_box_draft`, which backspaces the
    draft away in bounded chunks — slower, but it works on the render form
    Escape can't touch.

    Refuses to press Escape while the screen shows a live select-menu/dialog
    (#835 review): this function was written for a brand-new session's
    failed first-message seed, where nothing else could plausibly be on
    screen. It is also reused for an already-running, independently-busy
    session (``agentwire send --verify`` falling back after a failed
    delivery) — there, "box not empty" can mean a permission dialog or
    ``AskUserQuestion`` menu belonging to the recipient's own unrelated
    work, not our stuck paste. Escape is the conventional cancel/decline key
    for those, so pressing it blind would silently cancel a decision that
    had nothing to do with us. Returns False without touching the pane in
    that case; the caller's msg-inbox fallback still gets enqueued and
    waits for the box to become genuinely idle on its own.
    """
    from agentwire import pane_manager, prompt_router

    target = f"{session}.{pane_index}"

    def empty() -> bool:
        return prompt_router.prompt_is_empty(session, pane_index)

    for _ in range(CLEAR_BOX_ATTEMPTS):
        try:
            if empty():
                return True
            if prompt_router.screen_shows_live_menu(_snapshot(session, pane_index)):
                return False
            pane_manager.run_command(
                ["tmux", "send-keys", "-t", target, "Escape"], timeout=5
            )
            if _poll(empty, CLEAR_BOX_TIMEOUT):
                return True
            if _erase_box_draft(session, pane_index):
                return True
        except Exception:
            pass
    return False


def recover_failed_seed(
    session: str, message: str, sender: "str | None" = None, pane_index: int = 0
) -> "str | None":
    """Recover a first-message seed that failed to deliver (#695).

    1. Clear the partial paste out of the input box (it would block the
       inbox drain's empty-box gate indefinitely).
    2. Enqueue the prompt as a ``request`` into the session's msg inbox —
       the watchdog delivers it once the box is truly ready, and a request
       that can never deliver dead-letters LOUDLY (owner email) instead of
       vanishing.

    Step 1's outcome is NOT assumed (#843): if ``clear_input_box`` can't
    confirm the box ended up empty (Escape did nothing, or the call raised),
    the ORIGINAL stale, unsubmitted draft may still be sitting in the box —
    live for whatever unrelated Enter reaches the pane next (a later
    message's own retry, a human keypress) to submit it, looking exactly
    like a fresh, legitimate instruction. The durable copy still gets
    queued either way — still better than nothing — but the return value
    is honest about which happened instead of reporting blanket success.

    Returns ``"inbox"`` when the box was confirmed cleared AND the prompt
    was queued, ``"inbox_stuck"`` when the prompt was queued but the box
    could NOT be confirmed cleared (a stale draft may still be sitting
    there — needs manual intervention), or None when even the durable
    enqueue failed. Never raises.
    """
    try:
        cleared = clear_input_box(session, pane_index=pane_index)
    except Exception:
        cleared = False
    try:
        from agentwire import inbox

        inbox.enqueue(session, message, kind="request", sender=sender or "agentwire")
        return "inbox" if cleared else "inbox_stuck"
    except Exception:
        return None
