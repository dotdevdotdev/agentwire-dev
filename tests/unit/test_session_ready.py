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


class TestDeriveCheckFragment:
    def test_first_nonempty_line(self):
        assert session_ready.derive_check_fragment("\n\n  hello world  \nmore") == "hello world"

    def test_truncates(self):
        assert session_ready.derive_check_fragment("x" * 100) == "x" * 32

    def test_empty_message(self):
        assert session_ready.derive_check_fragment("   \n  ") == ""


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


class TestSendVerified:
    def _quiet(self, monkeypatch):
        monkeypatch.setattr(session_ready.time, "sleep", lambda _: None)

    def test_marker_mode_confirms(self, monkeypatch):
        self._quiet(monkeypatch)
        monkeypatch.setattr(session_ready, "send_to_session", lambda s, m, pane_index=0: None)
        monkeypatch.setattr(
            session_ready, "capture_session",
            lambda s, lines=60, pane_index=0: "...[COUNCIL PROMPT #1]...")
        assert session_ready.send_verified("s", "msg", "[COUNCIL PROMPT #1]")

    def test_marker_mode_retries_then_fails(self, monkeypatch):
        self._quiet(monkeypatch)
        sends = []
        monkeypatch.setattr(session_ready, "send_to_session", lambda s, m, pane_index=0: sends.append(s))
        monkeypatch.setattr(
            session_ready, "capture_session", lambda s, lines=60, pane_index=0: "no marker here")
        assert not session_ready.send_verified("s", "msg", "[COUNCIL PROMPT #1]")
        assert len(sends) == 2  # initial + one retry

    def test_markerless_uses_message_visible(self, monkeypatch):
        self._quiet(monkeypatch)
        monkeypatch.setattr(session_ready, "send_to_session", lambda s, m, pane_index=0: None)
        monkeypatch.setattr(
            session_ready, "capture_session",
            lambda s, lines=60, pane_index=0: "❯ build a voice diary app")
        assert session_ready.send_verified("s", "build a voice diary app")

    def test_markerless_confirms_on_retry(self, monkeypatch):
        self._quiet(monkeypatch)
        sends = []
        captures = ["", "❯ my idea text"]

        def fake_capture(s, lines=60, pane_index=0):
            return captures[min(len(sends) - 1, 1)]

        monkeypatch.setattr(session_ready, "send_to_session", lambda s, m, pane_index=0: sends.append(s))
        monkeypatch.setattr(session_ready, "capture_session", fake_capture)
        assert session_ready.send_verified("s", "my idea text")
        assert len(sends) == 2

    def test_capture_exception_counts_as_miss(self, monkeypatch):
        self._quiet(monkeypatch)

        def boom(s, lines=60, pane_index=0):
            raise RuntimeError("gone")

        monkeypatch.setattr(session_ready, "send_to_session", lambda s, m, pane_index=0: None)
        monkeypatch.setattr(session_ready, "capture_session", boom)
        assert not session_ready.send_verified("s", "msg")

    def test_pane_index_threads_through(self, monkeypatch):
        self._quiet(monkeypatch)
        seen = {}
        monkeypatch.setattr(session_ready, "send_to_session",
                            lambda s, m, pane_index=0: seen.update(send=pane_index))
        monkeypatch.setattr(session_ready, "capture_session",
                            lambda s, lines=60, pane_index=0: seen.update(cap=pane_index) or "❯ hi there")
        assert session_ready.send_verified("s", "hi there", pane_index=2)
        assert seen == {"send": 2, "cap": 2}


class TestPaneShowsActivity:
    def test_esc_to_interrupt(self):
        assert session_ready.pane_shows_activity("✶ Cogitating… (esc to interrupt)")

    def test_token_counter(self):
        assert session_ready.pane_shows_activity("· 1.2k tokens · esc")

    def test_tool_output_glyph(self):
        assert session_ready.pane_shows_activity("⏺ Bash(ls)\n  ⎿ file.py")

    def test_idle_no_activity(self):
        assert not session_ready.pane_shows_activity("❯ \nBypassing Permissions")


class TestSendVerifiedRegression:
    """Pins #478 — fast bypass agent submits + scrolls the paste away."""

    def _quiet(self, monkeypatch):
        monkeypatch.setattr(session_ready.time, "sleep", lambda _: None)

    def test_scrolled_out_but_working_is_delivered(self, monkeypatch):
        # Prompt fragment/placeholder has scrolled OUT of the capture, the
        # input box is empty, and the agent is visibly working → DELIVERED.
        self._quiet(monkeypatch)
        monkeypatch.setattr(session_ready, "send_to_session", lambda s, m, pane_index=0: None)
        monkeypatch.setattr(
            session_ready, "capture_session",
            lambda s, lines=60, pane_index=0: "⏺ Read(foo.py)\n  ⎿ 42 lines\n✶ Working… (esc to interrupt)")
        from agentwire import prompt_router
        monkeypatch.setattr(prompt_router, "prompt_is_empty", lambda s, p=0: True)
        assert session_ready.send_verified("s", "my unique multiline prompt")

    def test_placeholder_in_scrollback_is_delivered(self, monkeypatch):
        # Large paste renders only as the placeholder, still in scrollback.
        self._quiet(monkeypatch)
        monkeypatch.setattr(session_ready, "send_to_session", lambda s, m, pane_index=0: None)
        monkeypatch.setattr(
            session_ready, "capture_session",
            lambda s, lines=60, pane_index=0: "older output...\n❯ [Pasted text #1 +40 lines]\nmore")
        assert session_ready.send_verified("s", "line one\nline two\n...forty lines")

    def test_genuine_vanish_is_not_delivered(self, monkeypatch):
        # Empty input box, no activity, nothing in scrollback → NOT delivered.
        self._quiet(monkeypatch)
        monkeypatch.setattr(session_ready, "send_to_session", lambda s, m, pane_index=0: None)
        monkeypatch.setattr(
            session_ready, "capture_session",
            lambda s, lines=60, pane_index=0: "❯ \nBypassing Permissions")
        from agentwire import prompt_router
        monkeypatch.setattr(prompt_router, "prompt_is_empty", lambda s, p=0: True)
        assert not session_ready.send_verified("s", "this prompt vanished entirely")

    def test_verification_reads_scrollback(self, monkeypatch):
        # send_verified must request scrollback, not just the visible tail.
        self._quiet(monkeypatch)
        seen = {}
        monkeypatch.setattr(session_ready, "send_to_session", lambda s, m, pane_index=0: None)

        def fake_capture(s, lines=60, pane_index=0):
            seen["lines"] = lines
            return ""
        monkeypatch.setattr(session_ready, "capture_session", fake_capture)
        from agentwire import prompt_router
        monkeypatch.setattr(prompt_router, "prompt_is_empty", lambda s, p=0: True)
        session_ready.send_verified("s", "anything")
        assert seen["lines"] == session_ready.VERIFY_SCROLLBACK_LINES


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
