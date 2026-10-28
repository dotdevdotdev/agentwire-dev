"""Tests for agentwire.session_ready — readiness detection + verified delivery."""

from agentwire import session_ready

RULE = "─" * 20


def render_box(content: str = "") -> str:
    """A parseable Claude input box wrapped in horizontal rules."""
    glyph = f"❯ {content}" if content else "❯"
    return f"{RULE}\n{glyph}\n{RULE}"


def render_working(content: str = "") -> str:
    """An empty input box plus a visible activity marker (submitted+working)."""
    return "✶ Working… (esc to interrupt)\n" + render_box(content)


# Readiness frames (#695): banner up with a parseable input box, empty or
# holding the rendered probe char.
READY = "Bypassing Permissions\n" + render_box()
READY_PROBE = "Bypassing Permissions\n" + render_box(session_ready.PROBE_CHAR)


def _scripted_capture(monkeypatch, frames):
    """Make capture_pane return successive frames (last one repeats)."""
    state = {"i": 0}

    def fake_capture(session, pane_index, lines=20):
        frame = frames[min(state["i"], len(frames) - 1)]
        state["i"] += 1
        if isinstance(frame, Exception):
            raise frame
        return frame

    from agentwire import pane_manager
    monkeypatch.setattr(pane_manager, "capture_pane", fake_capture)


class TestWaitForSessionReady:
    def setup_method(self):
        self._sleeps = []

    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr(session_ready.time, "sleep", lambda s: self._sleeps.append(s))

    def _keys(self, monkeypatch):
        """Stub tmux key sends (trust-Enter + readiness probe); returns the log."""
        sent = []
        from agentwire import pane_manager
        monkeypatch.setattr(
            pane_manager, "run_command",
            lambda cmd, timeout=5: sent.append(cmd))
        return sent

    def _fake_time(self, monkeypatch, step=0.1):
        clock = {"t": 0.0}

        def fake_time():
            clock["t"] += step
            return clock["t"]

        monkeypatch.setattr(session_ready.time, "time", fake_time)

    # Happy-path frames: banner, READY_STABLE_SNAPSHOTS identical frames, the
    # probe char rendering in the box (poll + erase-count captures), erased.
    HAPPY = ["booting...", READY, READY, READY, READY_PROBE, READY_PROBE, READY]

    def test_banner_stable_then_probe_roundtrip_returns_true(self, monkeypatch):
        self._no_sleep(monkeypatch)
        sent = self._keys(monkeypatch)
        _scripted_capture(monkeypatch, self.HAPPY)
        assert session_ready.wait_for_session_ready("s", timeout=10)
        # The probe was typed and erased — never left in the box.
        assert any(c[-2:] == ["-l", session_ready.PROBE_CHAR] for c in sent)
        assert any(c[-1] == "BSpace" for c in sent)


class TestTallDraftWindow:
    """#851 — the input box has a bounded visible height and SCROLLS. A draft
    taller than that height renders only a window of itself, so full-message
    identity is impossible for it: Phase 1 could never pass, Enter was never
    pressed, and a 517-char single-line --first-message sat unsent (4/4
    children of the 2026-08-01 memory-manager fan-out)."""

    # The shape of the production message: one long line, no newlines, so
    # Claude never collapses it to a [Pasted text] chip.
    MSG = (
        "Review the file-based memory store for this project and prune what has "
        "rotted. The current memories are in the store's REVIEW.md. Verify each "
        "one against this repo before deciding anything. Bump verified: on the "
        "memories you confirm, delete the ones the code now contradicts, and "
        "merge duplicates into a single file. Report back with a one-paragraph "
        "summary naming any systemic pattern you noticed (vs a one-off), via "
        "agentwire msg send --to memory-manager --kind done."
    )

    def _tail(self, chars: int) -> str:
        return self.MSG[-chars:]

    def test_delivers_end_to_end(self, monkeypatch):
        # The acceptance case: the box only EVER shows the tail window, so the
        # pre-#851 gate pressed Enter zero times and the prompt sat unsent.
        _fake_clock(monkeypatch)

        def frame(a):
            if a["pastes"] == 0:
                return render_box()
            if a["enters"] == 0:
                return render_box(self._tail(454))
            return render_working()

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", self.MSG)
        assert actions["pastes"] == 1
        assert actions["enters"] == 1


def _fake_clock(monkeypatch, step: float = 0.5):
    """No-op sleep + a monotonically advancing clock so bounded polls time out
    fast in tests instead of busy-waiting real wall-clock seconds."""
    t = {"v": 0.0}

    def now():
        t["v"] += step
        return t["v"]

    monkeypatch.setattr(session_ready.time, "sleep", lambda _: None)
    monkeypatch.setattr(session_ready.time, "time", now)


def _env(monkeypatch, frame):
    """Wire paste/enter/capture stubs. *frame(actions)* returns the current
    capture given the running tally of pastes/enters/captures."""
    actions = {"pastes": 0, "enters": 0, "caps": 0}

    def paste(s, m, pane_index=0):
        actions["pastes"] += 1
        actions["paste_pane"] = pane_index

    def enter(s, pane_index=0):
        actions["enters"] += 1
        actions["enter_pane"] = pane_index

    def capture(s, lines=60, pane_index=0):
        actions["caps"] += 1
        actions["cap_lines"] = lines
        actions["cap_pane"] = pane_index
        return frame(actions)

    monkeypatch.setattr(session_ready, "paste_no_enter", paste)
    monkeypatch.setattr(session_ready, "press_enter", enter)
    monkeypatch.setattr(session_ready, "capture_session", capture)

    # The #845 foreign-draft guard asks prompt_router's SGR-aware gate for a
    # second opinion before refusing (dim ghost text is not a draft). These
    # frames carry no SGR, so mirror the plain parse -- and, crucially, keep
    # the guard from shelling out to the developer's LIVE tmux server.
    from agentwire import prompt_router

    def is_empty(session, pane_index=0):
        return session_ready.input_box(frame(actions)) == ""

    monkeypatch.setattr(prompt_router, "prompt_is_empty", is_empty)
    return actions


class TestSendVerifiedAdaptive:
    """#579 — paste/Enter is decoupled; Enter waits for the paste to land and
    re-presses if swallowed, never leaving text unsent."""

    def test_swallowed_enter_is_re_pressed(self, monkeypatch):
        # Text lands, but the first Enter is swallowed (box still holds it).
        # The bounded retry must re-press until the box clears.
        _fake_clock(monkeypatch)

        def frame(a):
            if a["enters"] < 2:
                return render_box("resilient prompt")  # still sitting unsent
            return render_working()  # second Enter registered

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", "resilient prompt")
        assert actions["enters"] == 2

    def test_genuine_failure_returns_false_not_silent(self, monkeypatch):
        # Empty box, no activity, never lands → hard False (caller learns), and
        # Enter is never blindly pressed into a box that never received the paste.
        _fake_clock(monkeypatch)
        actions = _env(monkeypatch, lambda a: render_box())
        assert not session_ready.send_verified("s", "this prompt vanished entirely")
        assert actions["enters"] == 0
        assert actions["pastes"] == 2  # initial + one retry


class TestNoDoublePaste:
    """#667 — a landed-but-unsubmitted copy means retry the SUBMIT, not the
    paste. The whole-send retry must never blindly paste a second copy on top
    of one already sitting in the box (the observed 'issue-659 twice' pile)."""

    def test_retry_skips_paste_when_copy_already_in_box(self, monkeypatch):
        _fake_clock(monkeypatch)
        msg = "[NOTIFY from agentwire-dev-issue-659-shift-tab] is idle and done working"
        # After the first paste the box permanently holds our text and Enter
        # never registers: the retry sees the landed copy and does NOT paste
        # again.
        actions = _env(
            monkeypatch, lambda a: render_box(msg) if a["pastes"] else render_box()
        )
        assert not session_ready.send_verified("s", msg, retries=1)
        assert actions["pastes"] == 1
        assert actions["enters"] > 0  # it kept retrying the SUBMIT


class TestHealSubmit:
    """#689 healing primitive — Enter-only, never a paste, and (#1105 review)
    re-verified before every Enter with POSITIVE confirmation only."""

    MSG = "[MSG from w · done] PR drafted  ⟨#abc123⟩"

    def _delivered_frame(self):
        # Box cleared AND the message echoed above it: the positive signal.
        return f"❯ {self.MSG}\n⏺ ok\n" + render_box()

    def test_submits_stuck_message(self, monkeypatch):
        _fake_clock(monkeypatch)

        def frame(a):
            if a["enters"] == 0:
                return render_box(self.MSG)  # stuck in the box
            return self._delivered_frame()  # Enter registered + echoed

        actions = _env(monkeypatch, frame)
        assert session_ready.heal_submit("s", self.MSG) == "submitted"
        assert actions["pastes"] == 0  # NEVER pastes (#621 dedup holds)
        assert actions["enters"] >= 1

    def test_box_changed_between_presses_stops_pressing(self, monkeypatch):
        _fake_clock(monkeypatch)

        def frame(a):
            if a["enters"] == 0:
                return render_box(self.MSG)
            return render_box("human took over the box")

        actions = _env(monkeypatch, frame)
        assert session_ready.heal_submit("s", self.MSG) != "submitted"
        assert actions["enters"] == 1  # one press, then re-verify stopped it


class TestClearInputBox:
    """#695 — seed recovery clears the partial paste out of the box, keyed on
    the drain's own SGR-aware emptiness gate (so 'cleared' means exactly 'the
    inbox fallback can deliver')."""

    def _wire(self, monkeypatch, empty_after: int, live_menu: bool = False,
              erased_after: int | None = None):
        """prompt_is_empty flips True after N Escapes (or, when *erased_after*
        is given, after that many backspaces); returns the key log."""
        from agentwire import pane_manager, prompt_router
        state = {"escapes": 0, "bspaces": 0}

        def is_empty(s, p=0):
            if erased_after is not None and state["bspaces"] >= erased_after:
                return True
            return state["escapes"] >= empty_after

        monkeypatch.setattr(prompt_router, "prompt_is_empty", is_empty)
        # No live menu on screen by default -- the snapshot content itself
        # doesn't matter here, only screen_shows_live_menu's verdict on it.
        monkeypatch.setattr(session_ready, "capture_session", lambda *a, **k: "")
        monkeypatch.setattr(prompt_router, "screen_shows_live_menu", lambda cap: live_menu)

        def run(cmd, timeout=5):
            if cmd[-1] == "Escape":
                state["escapes"] += 1
            state["bspaces"] += sum(1 for k in cmd if k == "BSpace")

        monkeypatch.setattr(pane_manager, "run_command", run)
        return state

    def test_live_menu_refuses_to_press_escape(self, monkeypatch):
        """#835 review: reused against an already-running, independently-busy
        session (agentwire send --verify's fallback), 'box not empty' can
        mean a permission dialog or AskUserQuestion menu belonging to the
        recipient's own unrelated work. Escape is the conventional
        cancel/decline key for those -- must never press it blind."""
        _fake_clock(monkeypatch)
        state = self._wire(monkeypatch, empty_after=1, live_menu=True)
        assert session_ready.clear_input_box("s") is False
        assert state["escapes"] == 0


class TestForeignDraftGuard:
    """#845 — a stale, DIFFERENT draft in the box was invisible to the
    idempotent-paste guard, which only knew 'holds our message' vs 'live menu'.
    Pasting on top concatenates the two into one garbled submitted turn."""

    def _wire(self, monkeypatch, box_content):
        _fake_clock(monkeypatch)
        return _env(monkeypatch, lambda a: render_box(box_content))

    def test_refuses_to_paste_over_a_stale_draft(self, monkeypatch):
        actions = self._wire(monkeypatch, "half-typed thought from a human")
        assert not session_ready.send_verified("s", "our new instruction")
        assert actions["pastes"] == 0
        assert actions["enters"] == 0


class TestRecoverFailedSeed:
    """#695 — a failed seed must never be silent: the prompt falls back to the
    msg inbox (watchdog delivery, dead-letter escalation) and the caller learns
    which fallback fired."""

    def test_clears_box_then_queues_request(self, monkeypatch):
        from agentwire import inbox
        calls = {}
        monkeypatch.setattr(
            session_ready, "clear_input_box",
            lambda s, pane_index=0: calls.setdefault("cleared", (s, pane_index)) or True)

        def fake_enqueue(to, text, kind="note", sender=None, ref=""):
            calls["enqueue"] = (to, text, kind, sender)
            return []

        monkeypatch.setattr(inbox, "enqueue", fake_enqueue)
        result = session_ready.recover_failed_seed("sess", "the seed prompt", sender="orch")
        assert result == "inbox"
        assert calls["cleared"] == ("sess", 0)
        assert calls["enqueue"] == ("sess", "the seed prompt", "request", "orch")


class TestLiveMenuAbort:
    """#698 hardening — a live select-menu appearing on the target mid-send
    owns the pane's keystrokes: Enter would confirm the highlighted option and
    pasted text could select one. Both the re-press loop and the retry paste
    must refuse and defer to the next tick's safe_deliver gate."""

    MENU = (
        "Do you want to proceed?\n"
        "❯ 1. Yes\n"
        "  2. No\n"
        "Esc to cancel"
    )

    def test_no_enter_pressed_into_live_menu(self, monkeypatch):
        _fake_clock(monkeypatch)

        def frame(a):
            if a["caps"] <= 2:
                return render_box("report text")  # landed
            return self.MENU  # dialog replaced the screen mid-submit

        actions = _env(monkeypatch, frame)
        assert not session_ready.send_verified("s", "report text")
        assert actions["enters"] == 0


class TestDedupOnlyTrustsTheInputEcho:
    """#1113 — verbatim rendered text counts as delivery only inside the agent's
    echo of a submitted input (a prompt-glyph row + its continuation rows). The
    same line in the agent's prose, a tool result, or a quote of one proves
    nothing: a relay chain can display a never-delivered message in full."""

    RULE = "─" * 30
    RENDERED = "[MSG from w · request] " + "lorem ipsum " * 40 + " ⟨#cc3b38⟩"
    SHORT = "[MSG from w · done] PR drafted  ⟨#abc123⟩"

    def _cap(self, body: str) -> str:
        return f"{body}\n\n{self.RULE}\n❯ \n{self.RULE}\n  status"

    def _wrapped(self, text: str, width: int = 30) -> str:
        rows = [text[i:i + width] for i in range(0, len(text), width)]
        return "\n".join(rows[:1] + [f"  {r}" for r in rows[1:]])

    def test_a_quote_outside_the_input_echo_never_counts(self):
        elided = (f"{self.RENDERED[:60]}\n  …\n  {self.RENDERED[-40:]}")
        bad = []
        for label, rendered, body in (
            ("prose", self.SHORT, f"⏺ the relay saw: {self.SHORT}"),
            ("prose continuation", self.SHORT, f"⏺ the relay saw:\n  {self.SHORT}"),
            ("tool output", self.SHORT, f"⏺ Bash(msg inbox)\n  ⎿  {self.SHORT}"),
            ("tool output bare", self.SHORT, f"⏺ Bash(cat log)\n{self.SHORT}"),
            ("angle-bracket quote in prose", self.SHORT, f"⏺ they said:\n> {self.SHORT}"),
            ("quote after the agent reply began", self.SHORT,
             f"❯ earlier\n\n⏺ done\n\n  {self.SHORT}"),
            ("long prose", self.RENDERED, f"⏺ quoting it whole: {self.RENDERED}"),
            ("long wrapped prose", self.RENDERED, f"⏺ quoting it\n{self._wrapped(self.RENDERED)}"),
            ("elided in prose", self.RENDERED, f"⏺ they wrote\n  {elided}"),
            ("glyph in prose", self.SHORT, f"⏺ ❯ {self.SHORT}"),
            ("after reply", self.SHORT, f"❯ earlier prompt\n\n⏺ reply\n  {self.SHORT}"),
        ):
            if session_ready.message_on_scrollback(self._cap(body), rendered):
                bad.append(label)
        assert not bad, bad


class TestShortWindowedTail:
    """#1164 — a narrow pane windows a paste to a tail UNDER MIN_BOX_FRAGMENT.

    The id token is the identity: a tail that ends in this message's unique
    ``⟨#id⟩`` and is a suffix of it can only be our own paste, however short.
    """

    RULE = "─" * 30
    MSG = ("[MSG from agentwire · request] "
           + "Please review the collapse height change carefully " * 3
           + "  ⟨#6a589b⟩")
    TAIL = MSG[-81:]

    def test_short_tail_reads_as_ours(self):
        # Guards the guard: a tail over the floor would pass without the fix.
        assert len("".join(self.TAIL.split())) < session_ready.MIN_BOX_FRAGMENT
        assert session_ready.box_shows_message(self.TAIL, self.MSG, allow_chip=False)
        assert session_ready.box_is_exactly(self.TAIL, self.MSG)

    def test_short_tail_without_our_token_is_not_ours(self):
        # A slice that lost the token, and a suffix too short to carry it: the
        # token is the only identity under the floor, so neither is ours.
        bad = []
        for tail in (self.MSG[-81:-12], self.MSG[-4:]):
            if session_ready.box_is_exactly(tail, self.MSG):
                bad.append(("box_is_exactly", tail))
            if session_ready.box_shows_message(tail, self.MSG, allow_chip=False):
                bad.append(("box_shows_message", tail))
        assert not bad, bad

    def test_undelivered_reason_names_it_pasted_unsubmitted(self, monkeypatch):
        from agentwire import prompt_router

        cap = f"scroll\n{self.RULE}\n❯ {self.TAIL}\n{self.RULE}\n  status"
        monkeypatch.setattr(session_ready, "scrollback", lambda s, p=0: cap)
        monkeypatch.setattr(prompt_router, "prompt_is_empty", lambda s, p=0: False)
        assert prompt_router._undelivered_reason("s", 0, self.MSG) == "pasted_unsubmitted"
