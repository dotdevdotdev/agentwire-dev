"""Tests for agentwire.session_ready — readiness detection + verified delivery."""

from agentwire import session_ready

BANNER = "❯ \nBypassing Permissions"
TRUST = "Do you trust this folder?\nPress Enter to confirm"


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

    def test_banner_then_stable_returns_true(self, monkeypatch):
        self._no_sleep(monkeypatch)
        _scripted_capture(monkeypatch, ["booting...", BANNER, BANNER])
        assert session_ready.wait_for_session_ready("s", timeout=10)

    def test_churning_screen_times_out(self, monkeypatch):
        self._no_sleep(monkeypatch)
        # Banner up but screen never stabilizes: every frame differs
        frames = [BANNER + f"\nline{i}" for i in range(1000)]
        _scripted_capture(monkeypatch, frames)
        # Fake clock: advance time per capture so the deadline passes
        clock = {"t": 0.0}

        def fake_time():
            clock["t"] += 0.1
            return clock["t"]

        monkeypatch.setattr(session_ready.time, "time", fake_time)
        assert not session_ready.wait_for_session_ready("s", timeout=5)

    def test_trust_prompt_accepted_then_ready(self, monkeypatch):
        self._no_sleep(monkeypatch)
        pressed = []
        from agentwire import pane_manager
        monkeypatch.setattr(
            pane_manager, "run_command",
            lambda cmd, timeout=5: pressed.append(cmd))
        _scripted_capture(monkeypatch, [TRUST, BANNER, BANNER])
        assert session_ready.wait_for_session_ready("s", timeout=10)
        assert len(pressed) == 1
        assert pressed[0][:2] == ["tmux", "send-keys"]
        assert pressed[0][-1] == "Enter"

    def test_capture_exception_tolerated(self, monkeypatch):
        self._no_sleep(monkeypatch)
        _scripted_capture(monkeypatch, [RuntimeError("no session"), BANNER, BANNER])
        assert session_ready.wait_for_session_ready("s", timeout=10)


class TestMessageVisible:
    def test_exact_match(self):
        msg = "build a voice diary app"
        assert session_ready.message_visible(f"❯ {msg}\n", msg)

    def test_wrapped_mid_word(self):
        # tmux wraps at pane width with no regard for word boundaries
        msg = "build a voice diary app with daily summaries"
        capture = "❯ build a voice di\nary app with dai\nly summaries"
        assert session_ready.message_visible(capture, msg)

    def test_pasted_placeholder_fallback(self):
        msg = "line one\nline two\nline three"
        assert session_ready.message_visible("❯ [Pasted text #1 +3 lines]", msg)

    def test_miss(self):
        assert not session_ready.message_visible("❯ \nBypassing Permissions", "my unique idea")

    def test_same_prefix_pile_does_not_false_match(self):
        # #667 fragment-collision repro: every worktree idle notification
        # shares a >32-char prefix. A pile of OTHER sessions' notifications
        # sitting in the box must NOT read as ours landing.
        pile = (
            "❯ [NOTIFY from agentwire-dev-issue-655-foo] is idle and done working\n"
            "[NOTIFY from agentwire-dev-issue-659-shift-tab] is idle and done working"
        )
        ours = "[NOTIFY from agentwire-dev-issue-661-bar] is idle and done working"
        assert not session_ready.message_visible(pile, ours)
        # ...while the actual message in the pile still matches.
        theirs = "[NOTIFY from agentwire-dev-issue-655-foo] is idle and done working"
        assert session_ready.message_visible(pile, theirs)

    def test_full_message_keying_not_prefix(self):
        # Two messages identical for well past 32 chars, differing in the tail.
        a = "[NOTIFY from agentwire-dev-issue-100] finished task alpha"
        b = "[NOTIFY from agentwire-dev-issue-100] finished task bravo"
        assert not session_ready.message_visible(f"❯ {a}", b)


RULE = "─" * 20


def render_box(content: str = "") -> str:
    """A parseable Claude input box wrapped in horizontal rules."""
    glyph = f"❯ {content}" if content else "❯"
    return f"{RULE}\n{glyph}\n{RULE}"


def render_working(content: str = "") -> str:
    """An empty input box plus a visible activity marker (submitted+working)."""
    return "✶ Working… (esc to interrupt)\n" + render_box(content)


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
    return actions


class TestSendVerified:
    def test_marker_mode_confirms(self, monkeypatch):
        _fake_clock(monkeypatch)
        # Marker already in scrollback, box cleared → submitted, no Enter needed.
        _env(monkeypatch, lambda a: "...[COUNCIL PROMPT #1]...\n" + render_box())
        assert session_ready.send_verified("s", "msg", "[COUNCIL PROMPT #1]")

    def test_marker_mode_retries_then_fails(self, monkeypatch):
        _fake_clock(monkeypatch)
        # Marker never appears and the box never shows our text → never lands,
        # so each whole-send attempt times out. Two pastes (initial + 1 retry).
        actions = _env(monkeypatch, lambda a: "no marker here\n" + render_box())
        assert not session_ready.send_verified("s", "msg", "[COUNCIL PROMPT #1]")
        assert actions["pastes"] == 2

    def test_markerless_lands_then_submits(self, monkeypatch):
        _fake_clock(monkeypatch)

        def frame(a):
            if a["enters"] == 0:
                return render_box("build a voice diary app")  # landed, unsent
            return render_working()  # Enter registered, box cleared, working

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", "build a voice diary app")
        assert actions["enters"] == 1

    def test_capture_exception_counts_as_miss(self, monkeypatch):
        _fake_clock(monkeypatch)

        def boom(a):
            raise RuntimeError("gone")

        actions = _env(monkeypatch, boom)
        assert not session_ready.send_verified("s", "msg")
        assert actions["pastes"] == 2  # retried, still failed

    def test_pane_index_threads_through(self, monkeypatch):
        _fake_clock(monkeypatch)

        def frame(a):
            if a["enters"] == 0:
                # Empty until the paste actually happens (the #667 pre-paste
                # guard skips the paste if the text already sits in the box).
                return render_box("hi there") if a["pastes"] else render_box()
            return render_working()

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", "hi there", pane_index=2)
        assert actions["paste_pane"] == 2
        assert actions["enter_pane"] == 2
        assert actions["cap_pane"] == 2


class TestPaneShowsActivity:
    def test_esc_to_interrupt(self):
        assert session_ready.pane_shows_activity("✶ Cogitating… (esc to interrupt)")

    def test_token_counter(self):
        assert session_ready.pane_shows_activity("· 1.2k tokens · esc")

    def test_tool_output_glyph(self):
        assert session_ready.pane_shows_activity("⏺ Bash(ls)\n  ⎿ file.py")

    def test_idle_no_activity(self):
        assert not session_ready.pane_shows_activity("❯ \nBypassing Permissions")


class TestSendVerifiedAdaptive:
    """#579 — paste/Enter is decoupled; Enter waits for the paste to land and
    re-presses if swallowed, never leaving text unsent."""

    def test_paste_lands_late_enter_waits(self, monkeypatch):
        # The paste hasn't rendered for the first few polls (empty box, no
        # activity). Enter must NOT fire until the text actually lands.
        _fake_clock(monkeypatch)

        def frame(a):
            if a["enters"] == 0:
                if a["caps"] <= 3:
                    return render_box()  # empty: paste not landed yet
                return render_box("seed prompt")  # landed
            return render_working()  # submitted + working

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", "seed prompt")
        # Enter was pressed exactly once, and only after the paste landed.
        assert actions["enters"] == 1

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

    def test_fast_agent_already_submitted(self, monkeypatch):
        # A fast bypass agent consumed+submitted before our first poll: empty
        # box + visible activity → delivered, no Enter needed.
        _fake_clock(monkeypatch)
        actions = _env(monkeypatch, lambda a: render_working())
        assert session_ready.send_verified("s", "my unique multiline prompt")
        assert actions["enters"] == 0

    def test_large_paste_placeholder_lands(self, monkeypatch):
        # Large paste renders as the [Pasted text] placeholder in the box; that
        # counts as landed, then submits.
        _fake_clock(monkeypatch)

        def frame(a):
            if a["enters"] == 0:
                return render_box("[Pasted text #1 +40 lines]")
            return render_working()

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", "line one\nline two\n...forty lines")
        assert actions["enters"] == 1

    def test_genuine_failure_returns_false_not_silent(self, monkeypatch):
        # Empty box, no activity, never lands → hard False (caller learns), and
        # Enter is never blindly pressed into a box that never received the paste.
        _fake_clock(monkeypatch)
        actions = _env(monkeypatch, lambda a: render_box())
        assert not session_ready.send_verified("s", "this prompt vanished entirely")
        assert actions["enters"] == 0
        assert actions["pastes"] == 2  # initial + one retry

    def test_verification_reads_scrollback(self, monkeypatch):
        # Submission verification must request scrollback, not just the tail.
        _fake_clock(monkeypatch)
        actions = _env(monkeypatch, lambda a: render_working())
        session_ready.send_verified("s", "anything")
        assert actions["cap_lines"] == session_ready.VERIFY_SCROLLBACK_LINES


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

    def test_no_paste_at_all_when_already_landed(self, monkeypatch):
        _fake_clock(monkeypatch)

        def frame(a):
            if a["enters"] == 0:
                return render_box("leftover from a prior attempt")
            return render_working()

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", "leftover from a prior attempt")
        assert actions["pastes"] == 0
        assert actions["enters"] == 1

    def test_pile_of_other_notifications_is_not_our_landing(self, monkeypatch):
        _fake_clock(monkeypatch)
        pile = (
            "[NOTIFY from agentwire-dev-issue-655-foo] is idle and done working "
            "[NOTIFY from agentwire-dev-issue-663-tab] is idle and done working"
        )
        ours = "[NOTIFY from agentwire-dev-issue-661-bar] is idle and done working"
        # Box shows only OTHER sessions' same-prefix notifications, ours never
        # renders: Phase 1 must fail (no false landing) and Enter must never be
        # pressed into the pile (the old 32-char fragment matched instantly and
        # then hammered Enter for 20s against a pile that could never clear).
        actions = _env(monkeypatch, lambda a: render_box(pile))
        assert not session_ready.send_verified("s", ours)
        assert actions["enters"] == 0


class TestPrePasteGuardIdentity:
    """#668 review — the pre-paste short-circuit may fire ONLY on positive
    full-message identity. Ambient evidence (activity glyphs beside an empty
    box, a foreign [Pasted text] placeholder, a constant caller marker) must
    never count as delivery before we have pasted: a false 'submitted' makes
    the msg drain unlink queued messages that were never sent."""

    def test_empty_box_with_transcript_glyphs_does_not_short_circuit(self, monkeypatch):
        # A real agent pane: 200-line scrollback full of tool glyphs/spinner,
        # empty input box. The old guard called submitted() pre-paste, which
        # returned True on empty-box+activity → nothing pasted, reported sent.
        _fake_clock(monkeypatch)
        transcript = "⏺ Bash(ls)\n  ⎿ file.py\n✻ Thinking…\n· 1.2k tokens\n"

        def frame(a):
            if a["pastes"] == 0:
                return transcript + render_box()  # busy-looking, empty box
            if a["enters"] == 0:
                return transcript + render_box("fresh report")  # our paste landed
            return render_working()

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", "fresh report")
        assert actions["pastes"] == 1  # the guard did NOT skip the paste

    def test_constant_marker_on_scrollback_does_not_short_circuit(self, monkeypatch):
        # Council-style constant marker already on scrollback from the PREVIOUS
        # nudge: pre-paste it proves nothing about THIS message. The paste must
        # happen (Phase 1 may then legitimately confirm via the marker).
        _fake_clock(monkeypatch)

        def frame(a):
            return "[COUNCIL FOLLOW-UP]\nold nudge text\n" + render_box()

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", "second nudge", "[COUNCIL FOLLOW-UP]")
        assert actions["pastes"] == 1

    def test_foreign_pasted_placeholder_is_not_our_landing(self, monkeypatch):
        # A human's half-composed large paste sits in the target box as
        # [Pasted text ...]. Pre-paste that placeholder can only be someone
        # ELSE's draft: we must not skip our paste, and we must never press
        # Enter before pasting (which would force-submit the foreign draft).
        _fake_clock(monkeypatch)

        def frame(a):
            if a["pastes"] == 0:
                return render_box("[Pasted text #1 +57 lines]")
            if a["enters"] == 0:
                return render_box("our own report")
            return render_working()

        actions = _env(monkeypatch, frame)

        real_enter = session_ready.press_enter

        def guarded_enter(s, pane_index=0):
            assert actions["pastes"] > 0, "Enter pressed into a foreign draft before pasting"
            real_enter(s, pane_index=pane_index)

        monkeypatch.setattr(session_ready, "press_enter", guarded_enter)
        assert session_ready.send_verified("s", "our own report")
        assert actions["pastes"] == 1

    def test_full_message_on_scrollback_short_circuits_as_submitted(self, monkeypatch):
        # Positive identity: our FULL rendered message already on scrollback
        # (not in the box) → a prior attempt submitted it. No paste, no Enter.
        _fake_clock(monkeypatch)
        msg = "[MSG from worker · done] finished  ⟨#abc123⟩"
        actions = _env(monkeypatch, lambda a: f"{msg}\n" + render_box())
        assert session_ready.send_verified("s", msg)
        assert actions["pastes"] == 0
        assert actions["enters"] == 0


class TestSubmitConfirmed:
    """#621 — Phase-2 confirm keys on 'the box no longer holds our text', since
    Phase 1 already proved the paste landed. The old `submitted` additionally
    demanded a spinner or the echoed turn, which false-negatived a landed-and-
    submitted paste under a quiet/fast agent (→ inbox redelivery loop / notify
    'sat there unsent')."""

    def test_quiet_cleared_box_is_confirmed(self):
        # Box cleared, NO activity marker, message already scrolled out of view.
        cap = render_box()  # empty box, nothing else
        assert session_ready.submit_confirmed(cap, "report text")
        # The old, over-strict check would NOT confirm this — the regression.
        assert not session_ready.submitted(cap, "report text")

    def test_text_still_in_box_is_not_confirmed(self):
        cap = render_box("report text")
        assert not session_ready.submit_confirmed(cap, "report text")

    def test_unparseable_box_falls_back_to_marker(self):
        cap = "tool output everywhere, no box at all\n[MARKER-LINE]"
        assert session_ready.submit_confirmed(cap, "zzz", marker="[MARKER-LINE]")
        assert not session_ready.submit_confirmed(cap, "zzz", marker="[ABSENT]")

    def test_quiet_submit_no_activity_delivers(self, monkeypatch):
        # End-to-end: paste lands in the box, one Enter clears it, and the pane
        # goes quiet (no spinner, message scrolled off). Must report delivered.
        _fake_clock(monkeypatch)

        def frame(a):
            if a["enters"] == 0:
                return render_box("quiet prompt")  # landed, sitting unsent
            return render_box()  # submitted: box cleared, no activity at all

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", "quiet prompt")
        assert actions["enters"] == 1


class TestCouncilDelegation:
    """council.cli.send_verified / wait_ready now delegate here."""

    def test_council_send_verified_delegates(self, monkeypatch):
        from agentwire.council import cli
        calls = {}

        def fake(session, message, marker=None, retries=1, settle=2.0):
            calls["args"] = (session, message, marker, retries)
            return True

        monkeypatch.setattr(session_ready, "send_verified", fake)
        assert cli.send_verified("council-gut", "msg", "[COUNCIL PROMPT #1]")
        assert calls["args"] == ("council-gut", "msg", "[COUNCIL PROMPT #1]", 1)


class TestStripInputBox:
    """#689 — 'on scrollback' must mean OUTSIDE the input box."""

    def test_removes_box_region(self):
        cap = "history line\n" + render_box("draft text")
        outside = session_ready.strip_input_box(cap)
        assert "history line" in outside
        assert "draft text" not in outside

    def test_unparseable_returns_none(self):
        assert session_ready.strip_input_box("no rules anywhere") is None

    def test_empty_box(self):
        outside = session_ready.strip_input_box("above\n" + render_box())
        assert "above" in outside


class TestMessageOnScrollbackBoxAware:
    """#689 regression — a pasted-but-unsubmitted message sitting in the input
    box must NOT read as 'on scrollback'. That false positive is exactly how
    the drain unlinked pending files while the recipient never got the message
    (2026-07-03 repro)."""

    def test_message_in_box_is_not_on_scrollback(self):
        msg = "[MSG from w · done] PR drafted  ⟨#abc123⟩"
        cap = "some history\n" + render_box(msg)
        assert not session_ready.message_on_scrollback(cap, msg)

    def test_message_above_box_is_on_scrollback(self):
        msg = "[MSG from w · done] PR drafted  ⟨#abc123⟩"
        cap = f"{msg}\n" + render_box()
        assert session_ready.message_on_scrollback(cap, msg)

    def test_unparseable_box_stays_pending(self):
        # Can't prove the text is outside the box → conservative False.
        msg = "[MSG from w · done] PR drafted  ⟨#abc123⟩"
        assert not session_ready.message_on_scrollback(f"junk\n{msg}\njunk", msg)


class TestZeroEnterFalsePositive:
    """#689 root cause 1 — a busy pane whose box is unparseable at phase-2
    start must NOT be declared submitted before at least one Enter has actually
    been pressed."""

    def test_strict_confirm_rejects_unparseable_box(self):
        busy = "⏺ Bash(ls)\n  ⎿ file.py\n✻ Thinking…\nsome text no box"
        assert not session_ready.submit_confirmed(
            busy, "our msg", allow_unparsed=False
        )
        # Permissive (post-Enter) still accepts activity evidence.
        assert session_ready.submit_confirmed(busy, "our msg")

    def test_enter_pressed_before_permissive_confirm(self, monkeypatch):
        # Paste lands (parseable box), then the pane re-renders busily and the
        # box becomes unparseable with activity glyphs. The old code confirmed
        # on the FIRST phase-2 snapshot with zero Enters.
        _fake_clock(monkeypatch)
        busy = "⏺ Bash(build)\n✶ Working… (esc to interrupt)\nno box parses here"

        def frame(a):
            if a["enters"] == 0:
                if a["caps"] <= 2:
                    return render_box("stuck report")  # landed
                return busy  # unparseable re-render — must NOT confirm yet
            return render_working()

        actions = _env(monkeypatch, frame)
        assert session_ready.send_verified("s", "stuck report")
        assert actions["enters"] >= 1, "confirmed with zero Enters pressed"


class TestFinishSubmit:
    """#689 healing primitive — Enter-only, never a paste."""

    def test_submits_stuck_message(self, monkeypatch):
        _fake_clock(monkeypatch)
        msg = "[MSG from w · done] PR drafted  ⟨#abc123⟩"

        def frame(a):
            if a["enters"] == 0:
                return render_box(msg)  # stuck in the box
            return render_box()  # Enter registered

        actions = _env(monkeypatch, frame)
        assert session_ready.finish_submit("s", msg)
        assert actions["pastes"] == 0  # NEVER pastes (#621 dedup holds)
        assert actions["enters"] >= 1

    def test_already_clear_box_no_enter(self, monkeypatch):
        _fake_clock(monkeypatch)
        actions = _env(monkeypatch, lambda a: render_box())
        assert session_ready.finish_submit("s", "gone message")
        assert actions["enters"] == 0
        assert actions["pastes"] == 0

    def test_wedged_box_returns_false(self, monkeypatch):
        _fake_clock(monkeypatch)
        msg = "immovable text"
        actions = _env(monkeypatch, lambda a: render_box(msg))
        assert not session_ready.finish_submit("s", msg)
        assert actions["pastes"] == 0

    def test_never_raises(self, monkeypatch):
        _fake_clock(monkeypatch)

        def boom(a):
            raise RuntimeError("gone")

        _env(monkeypatch, boom)
        assert session_ready.finish_submit("s", "msg") is False
