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


def submit_confirmed(
    capture: str,
    message: str,
    marker: str | None = None,
    allow_unparsed: bool = True,
) -> bool:
    """Phase-2 confirm: did the (already-landed) paste actually *submit*? (#621)

    Distinct from :func:`submitted`, which also has to defend Phase 1's "did the
    paste even land?" question and so demands positive activity/scrollback
    evidence before trusting an empty box. By Phase 2 we have ALREADY proven the
    text landed in the input box, so the submission signal is simply: **the box
    no longer holds our text.** Requiring a spinner or the echoed turn on top of
    that is what false-negatived a landed-and-submitted paste under a quiet or
    very fast agent — reporting ``delivery_unverified`` (→ inbox redelivery loop)
    or an unconfirmed Enter (→ notify-parent "sat there unsent").

    - Box parseable → submitted iff our message is no longer visible in it. An
      empty box, or a box now showing a different/next prompt, both count. While
      a multi-line paste's ``[Pasted text …]`` placeholder (or the expanded text)
      still occupies the box, it reads as not-yet-submitted, so the caller keeps
      pressing Enter (dismiss-then-submit).
    - Box unparseable (tool output / dialog covering it) → fall back to positive
      evidence: the explicit marker line, the message echoed in scrollback, or
      visible activity. This fallback is inherently permissive — activity glyphs
      sit in almost every agent pane's scrollback, and the "visible" text may be
      our paste still sitting in the very box we failed to parse — so it is only
      trusted once at least one Enter has actually been pressed (#689): callers
      pass ``allow_unparsed=False`` before the first press, forcing an Enter
      instead of declaring a busy re-rendering pane submitted with zero
      keystrokes ever sent.
    """
    box = input_box(capture)
    if box is not None:
        return not message_visible(box, message)
    if not allow_unparsed:
        return False
    if marker is not None and marker in capture:
        return True
    return pane_shows_activity(capture) or message_visible(capture, message)


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


def message_visible(capture: str, message: str) -> bool:
    """Did *message* land in the pane?

    Keys on the **full** whitespace-normalized message, never a fixed-length
    prefix (#667): all worktree idle notifications share a long
    ``[NOTIFY from agentwire-dev-issue-…`` prefix, so a 32-char fragment
    false-matched a *pile* of other sessions' notifications sitting in the box
    — Phase 1 passed against text that wasn't ours, and Phase 2's
    "box no longer shows our text" could never come true. tmux
    ``capture-pane`` wraps long lines at pane width mid-word, so both sides
    are compared with all whitespace stripped. Large multiline pastes may
    render only as Claude's ``[Pasted text #N +M lines]`` placeholder, which
    counts as landed (the failure mode being defended against is the paste
    vanishing entirely).
    """
    needle = "".join(message.split())
    if needle and needle in "".join(capture.split()):
        return True
    return "[Pasted text" in capture


def strip_input_box(capture: str) -> "str | None":
    """*capture* with the input-box region removed, or None if no box parses.

    The #689 building block: "on scrollback" must mean *outside the input box*.
    A pasted-but-unsubmitted message renders inside the box, which is part of
    every pane capture — matching against the raw capture reads a swallowed
    Enter as a delivered message. Mirrors ``prompt_router.input_box_content``'s
    parse (region between the last two horizontal rules, prompt glyph first);
    when the box can't be located we return None so callers stay conservative
    (can't prove the text is outside the box → treat as not on scrollback).
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
    return "\n".join(lines[:rules[-2] + 1] + lines[rules[-1]:])


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
    # The short-circuit demands POSITIVE full-message identity — the full
    # whitespace-normalized message visible in the box (→ landed: skip the
    # paste, retry only the submit) or on scrollback outside the box
    # (→ already submitted). Nothing weaker counts before we have pasted:
    # NOT empty-box+activity (real agent panes show ⏺/⎿/spinner glyphs in
    # scrollback almost always — accepting that as "delivered" makes the msg
    # drain unlink queued messages that were never sent: silent deletion),
    # NOT the ``[Pasted text]`` placeholder (pre-paste it can only be someone
    # ELSE's draft — skipping our paste and pressing Enter would force-submit
    # a foreign draft; see message_on_scrollback), and NOT the caller marker
    # (markers like council's are constant across messages, so a hit may be
    # the PREVIOUS message). When identity is not provable, paste again — the
    # existing full-line dedup makes duplicates recoverable; deletions aren't.
    already_landed = False
    try:
        cap = _snapshot(session, pane_index)
        box = input_box(cap)
        needle = "".join(message.split())
        if box is not None and needle:
            if needle in "".join(box.split()):
                already_landed = True
            elif message_on_scrollback(cap, message):
                return True
    except Exception:
        pass

    if not already_landed:
        paste_no_enter(session, message, pane_index=pane_index)

    # Phase 1 — wait for the paste to actually land in the input box (or for a
    # very fast bypass agent to have already consumed AND submitted it). If it
    # never lands, the paste vanished — let the caller retry the whole send.
    def landed_or_done() -> bool:
        cap = _snapshot(session, pane_index)
        return submitted(cap, message, marker) or text_landed(cap, message)

    if not _poll(landed_or_done, LAND_TIMEOUT):
        return False

    return _submit_phase(session, message, marker, pane_index)


def _submit_phase(
    session: str, message: str, marker: str | None, pane_index: int
) -> bool:
    """Phase 2 — press Enter and confirm the box cleared. Enter-only, no paste.

    Re-press is driven by a wall-clock budget, not a fixed count: a single Enter
    can be swallowed under load, a large paste needs a second Enter (dismiss the
    ``[Pasted text]`` banner, then submit), and on a bogged-down host the box
    renders slowly. We keep pressing until the box clears or SUBMIT_BUDGET
    elapses — an idle Enter on an already-submitted/empty box is a harmless
    no-op, so over-pressing is safe, while a tight count would give up before a
    laggy box ever caught up.

    The pre-press confirm is STRICT (``allow_unparsed=False``): before any Enter
    has been sent, an unparseable box must never read as submitted — that was
    the #689 zero-keystroke false positive (busy pane + activity glyphs →
    "delivered" with the Enter never fired). Once at least one Enter is pressed,
    the permissive fallback is trusted again.
    """
    if submit_confirmed(
        _snapshot(session, pane_index), message, marker, allow_unparsed=False
    ):
        return True
    deadline = time.time() + SUBMIT_BUDGET
    attempts = 0
    while True:
        press_enter(session, pane_index=pane_index)
        attempts += 1
        if _poll(
            lambda: submit_confirmed(_snapshot(session, pane_index), message, marker),
            SUBMIT_TIMEOUT,
        ):
            return True
        if attempts >= MIN_ENTER_ATTEMPTS and time.time() >= deadline:
            return False


def finish_submit(
    session: str, message: str, marker: str | None = None, pane_index: int = 0
) -> bool:
    """Enter-only submit retry for a message already sitting in the input box.

    The #689 healing primitive: when a prior delivery pasted the text but its
    Enter was swallowed, the fix is to press Enter again — NEVER to re-paste
    (the #621 idempotency dedup must keep holding; a second paste doubles the
    draft). Runs the same strict-then-press phase-2 loop as a fresh delivery.
    Returns True only once submission is confirmed; never raises.
    """
    try:
        return _submit_phase(session, message, marker, pane_index)
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


def clear_input_box(session: str, pane_index: int = 0) -> bool:
    """Best-effort: clear whatever sits unsubmitted in the input box (#695).

    A failed seed can leave a partial paste in the box, which (a) confuses a
    human attaching to the session and (b) blocks the msg-inbox fallback
    forever — the drain only pastes into an EMPTY box. Sends Escape (Claude
    Code: clears the current input, including a large paste's ``[Pasted
    text]`` chip) and confirms emptiness with the drain's own SGR-aware gate
    (``prompt_router.prompt_is_empty``), so "cleared" here means exactly
    "the drain will deliver". Returns True iff the box ends up empty.
    """
    from agentwire import pane_manager, prompt_router

    target = f"{session}.{pane_index}"

    def empty() -> bool:
        return prompt_router.prompt_is_empty(session, pane_index)

    for _ in range(CLEAR_BOX_ATTEMPTS):
        try:
            if empty():
                return True
            pane_manager.run_command(
                ["tmux", "send-keys", "-t", target, "Escape"], timeout=5
            )
            if _poll(empty, CLEAR_BOX_TIMEOUT):
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

    Returns ``"inbox"`` when the prompt was queued, None when even the
    fallback failed (the caller must tell the user to deliver manually).
    Never raises.
    """
    try:
        clear_input_box(session, pane_index=pane_index)
    except Exception:
        pass
    try:
        from agentwire import inbox

        inbox.enqueue(session, message, kind="request", sender=sender or "agentwire")
        return "inbox"
    except Exception:
        return None
