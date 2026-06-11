"""Tests for usage-limit dialog recovery (agentwire/usage_limit.py).

The dialog fixture is the real pane capture from the 2026-06-10 incident
(two scheduler verification runs parked on the dialog for ~11 hours).
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agentwire import usage_limit


# Real capture from the incident (fragmentz/scheduler-fragmentz-leads-daily).
REAL_DIALOG = """\
  ⎿  You've hit your session limit · resets 11:40pm (America/Toronto)

✻ Baked for 23m 20s

❯ /rate-limit-options

────────────────────────────────────────────────────────────────────────────────
  What do you want to do?

  ❯ 1. Stop and wait for limit to reset
    2. Switch to usage credits
    3. Switch to Team plan

  Enter to confirm · Esc to cancel

"""

# An orchestrator pane *displaying* a captured dialog (its own prompt below).
DISPLAYED_DIALOG = REAL_DIALOG + """\
```

### jordan
- Idle: 100min | Nagged: 49x
- Type: claude-bypass

❯ ready for your next instruction
"""

# A live menu that is NOT the usage-limit dialog (drift / other dialogs).
OTHER_DIALOG = """\
  What do you want to do?

  ❯ 1. Yes, proceed
    2. No, cancel

  Enter to confirm · Esc to cancel
"""

# Narrow pane: the option line wraps mid-phrase.
WRAPPED_DIALOG = """\
  What do you want to do?

  ❯ 1. Stop and wait for limit
  to reset
    2. Switch to usage credits

  Enter to confirm · Esc to
  cancel
"""


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point all state/event paths into tmp and never send real email."""
    state_dir = tmp_path / "usage-limit"
    monkeypatch.setattr(usage_limit, "STATE_DIR", state_dir)
    monkeypatch.setattr(usage_limit, "DONE_DIR", state_dir / "done")
    monkeypatch.setattr(usage_limit, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(
        usage_limit, "_send_notification", lambda *a, **k: False
    )
    monkeypatch.setattr(usage_limit.time, "sleep", lambda s: None)
    return state_dir


def events(tmp_path=None):
    path = usage_limit.EVENTS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


# =============================================================================
# Detection
# =============================================================================


class TestDetectDialog:
    def test_real_incident_capture(self):
        assert usage_limit.detect_dialog(REAL_DIALOG) is True

    def test_wrapped_narrow_pane(self):
        assert usage_limit.detect_dialog(WRAPPED_DIALOG) is True

    def test_displayed_capture_is_not_live(self):
        # A pane quoting the dialog (orchestrator review) must not match.
        assert usage_limit.detect_dialog(DISPLAYED_DIALOG) is False

    def test_other_menus_dont_match(self):
        assert usage_limit.detect_dialog(OTHER_DIALOG) is False

    def test_plain_output_doesnt_match(self):
        assert usage_limit.detect_dialog("$ make test\nAll green\n") is False
        assert usage_limit.detect_dialog("") is False

    def test_scrollback_remnant_with_prompt_below(self):
        # After parking, the menu text may linger above a live prompt.
        text = REAL_DIALOG + "\n❯ \n"
        assert usage_limit.detect_dialog(text) is False


class TestDetectDialogLike:
    def test_unknown_menu_is_dialog_like(self):
        assert usage_limit.detect_dialog_like(OTHER_DIALOG) is True

    def test_known_dialog_is_not_dialog_like(self):
        assert usage_limit.detect_dialog_like(REAL_DIALOG) is False

    def test_plain_output_is_not_dialog_like(self):
        assert usage_limit.detect_dialog_like("compiling...") is False


# =============================================================================
# Reset time parsing
# =============================================================================

TORONTO = ZoneInfo("America/Toronto")


class TestParseResetTime:
    def test_real_capture(self):
        now = datetime(2026, 6, 10, 22, 30, tzinfo=TORONTO)
        result = usage_limit.parse_reset_time(REAL_DIALOG, now)
        assert result == datetime(2026, 6, 10, 23, 40, tzinfo=TORONTO)
        assert result.tzinfo == timezone.utc

    def test_rolls_past_midnight(self):
        now = datetime(2026, 6, 10, 23, 0, tzinfo=TORONTO)
        result = usage_limit.parse_reset_time("resets 1:05am (America/Toronto)", now)
        assert result == datetime(2026, 6, 11, 1, 5, tzinfo=TORONTO)

    def test_stated_time_already_passed_means_reset_done(self):
        # Dialog said 11:40pm; we only noticed at 11:50pm. Tomorrow-11:40pm
        # is outside one 5h window, so the reset already happened.
        now = datetime(2026, 6, 10, 23, 50, tzinfo=TORONTO)
        result = usage_limit.parse_reset_time("resets 11:40pm (America/Toronto)", now)
        assert result == now

    def test_no_minutes_and_at_variant(self):
        now = datetime(2026, 6, 10, 13, 0, tzinfo=TORONTO)
        result = usage_limit.parse_reset_time("resets at 3pm (America/Toronto)", now)
        assert result == datetime(2026, 6, 10, 15, 0, tzinfo=TORONTO)

    def test_12am_and_12pm(self):
        now = datetime(2026, 6, 10, 22, 0, tzinfo=TORONTO)
        result = usage_limit.parse_reset_time("resets 12:15am (America/Toronto)", now)
        assert result == datetime(2026, 6, 11, 0, 15, tzinfo=TORONTO)

        now = datetime(2026, 6, 10, 10, 0, tzinfo=TORONTO)
        result = usage_limit.parse_reset_time("resets 12:30pm (America/Toronto)", now)
        assert result == datetime(2026, 6, 10, 12, 30, tzinfo=TORONTO)

    def test_last_match_wins(self):
        now = datetime(2026, 6, 10, 20, 0, tzinfo=TORONTO)
        text = "resets 9:00pm (America/Toronto)\n...\nresets 11:40pm (America/Toronto)"
        result = usage_limit.parse_reset_time(text, now)
        assert result == datetime(2026, 6, 10, 23, 40, tzinfo=TORONTO)

    def test_unknown_timezone_falls_back_to_local(self):
        now = datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc)
        result = usage_limit.parse_reset_time("resets 11:40pm (Mars/Olympus)", now)
        assert result is not None

    def test_no_timezone_uses_local(self):
        now = datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc)
        assert usage_limit.parse_reset_time("resets 11:40pm", now) is not None

    def test_unparseable_returns_none(self):
        assert usage_limit.parse_reset_time("no limits here", _now_utc()) is None
        assert usage_limit.parse_reset_time("", _now_utc()) is None


def _now_utc():
    return datetime.now(timezone.utc)


# =============================================================================
# State files
# =============================================================================


class TestState:
    def test_roundtrip_and_is_parked(self):
        state = {"session": "mysession", "status": "parked"}
        usage_limit.write_park_state(state)
        assert usage_limit.is_parked("mysession") is True
        assert usage_limit.read_park_state("mysession") == state
        assert usage_limit.list_parked() == [state]

    def test_worktree_session_names_nest(self):
        state = {"session": "fragmentz/scheduler-leads-daily", "status": "parked"}
        usage_limit.write_park_state(state)
        assert usage_limit.is_parked("fragmentz/scheduler-leads-daily") is True
        assert usage_limit.list_parked() == [state]

    def test_archive_moves_out_of_active(self):
        state = {"session": "proj/branch", "status": "parked"}
        usage_limit.write_park_state(state)
        usage_limit.archive_state(state, "resumed")
        assert usage_limit.is_parked("proj/branch") is False
        assert usage_limit.list_parked() == []
        archived = list(usage_limit.DONE_DIR.glob("*.json"))
        assert len(archived) == 1
        data = json.loads(archived[0].read_text())
        assert data["status"] == "resumed"
        assert data["archived_at"]

    def test_not_parked_when_nothing_written(self):
        assert usage_limit.is_parked("ghost") is False
        assert usage_limit.list_parked() == []


# =============================================================================
# Park
# =============================================================================


class FakeTmux:
    """Scriptable stand-in for usage_limit._tmux."""

    def __init__(self, screens):
        # screens: list of visible-screen strings; each capture pops the next
        # (last one repeats).
        self.screens = list(screens)
        self.sent_keys = []

    def __call__(self, args, timeout=5):
        cmd = args[0]
        if cmd == "capture-pane":
            text = self.screens[0]
            if len(self.screens) > 1:
                self.screens.pop(0)
            return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
        if cmd == "send-keys":
            self.sent_keys.append(args[-1])
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd == "display-message":
            return subprocess.CompletedProcess(args, 0, stdout="/tmp/proj\n", stderr="")
        if cmd == "has-session":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd == "list-panes":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="unknown")


class TestPark:
    def test_parks_and_writes_state(self, monkeypatch):
        # Captures: park() detect → scrollback → confirm (dismissed)
        fake = FakeTmux([REAL_DIALOG, REAL_DIALOG, "❯ waiting...\n"])
        monkeypatch.setattr(usage_limit, "_tmux", fake)

        now = datetime(2026, 6, 11, 2, 30, tzinfo=timezone.utc)  # 22:30 Toronto
        monkeypatch.setattr(usage_limit, "_now", lambda: now)

        state = usage_limit.park("fragmentz/leads", pane_index=0)

        assert state is not None
        assert fake.sent_keys == ["1", "Enter"]
        assert state["status"] == "parked"
        assert state["reset_parse_failed"] is False
        # 11:40pm Toronto == 03:40 UTC
        assert state["reset_at"] == "2026-06-11T03:40:00+00:00"
        assert state["resume_at"] == "2026-06-11T03:42:00+00:00"
        assert usage_limit.is_parked("fragmentz/leads")
        assert any(e["event"] == "session_parked" for e in events())

    def test_idempotent_when_already_parked(self, monkeypatch):
        usage_limit.write_park_state({"session": "s1", "status": "parked"})
        fake = FakeTmux([REAL_DIALOG])
        monkeypatch.setattr(usage_limit, "_tmux", fake)
        assert usage_limit.park("s1") is None
        assert fake.sent_keys == []

    def test_no_dialog_no_park(self, monkeypatch):
        fake = FakeTmux(["just normal output\n"])
        monkeypatch.setattr(usage_limit, "_tmux", fake)
        assert usage_limit.park("s1") is None
        assert not usage_limit.is_parked("s1")
        assert fake.sent_keys == []

    def test_unparseable_reset_falls_back_5h(self, monkeypatch):
        dialog = WRAPPED_DIALOG  # no "resets ..." line anywhere
        fake = FakeTmux([dialog, dialog, ""])
        monkeypatch.setattr(usage_limit, "_tmux", fake)
        now = datetime(2026, 6, 11, 2, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(usage_limit, "_now", lambda: now)

        state = usage_limit.park("s1")
        assert state["reset_parse_failed"] is True
        assert state["reset_at"] == "2026-06-11T07:00:00+00:00"
        assert any(e["event"] == "reset_parse_failed" for e in events())

    def test_retries_menu_confirm_once(self, monkeypatch):
        # Menu survives the first 1+Enter, dismissed after the second.
        fake = FakeTmux([REAL_DIALOG, REAL_DIALOG, REAL_DIALOG, "❯\n"])
        monkeypatch.setattr(usage_limit, "_tmux", fake)
        state = usage_limit.park("s1")
        assert state is not None
        assert fake.sent_keys == ["1", "Enter", "1", "Enter"]


# =============================================================================
# Resume
# =============================================================================


class TestResume:
    def _parked(self, session="s1", resume_at=None, **extra):
        state = {
            "session": session,
            "pane": 0,
            "status": "parked",
            "detected_at": "2026-06-11T02:30:00+00:00",
            "parked_at": "2026-06-11T02:30:05+00:00",
            "reset_at": "2026-06-11T03:40:00+00:00",
            "resume_at": resume_at or "2026-06-11T03:42:00+00:00",
            "notified": True,
            "resume_attempts": 0,
            **extra,
        }
        usage_limit.write_park_state(state)
        return state

    def test_resume_sends_nudge_and_archives(self, monkeypatch):
        state = self._parked()
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: True)
        sent = []
        monkeypatch.setattr(
            usage_limit, "_capture",
            lambda target, scrollback=None: f"> {usage_limit.RESUME_NUDGE}\n",
        )
        import agentwire.pane_manager as pm
        monkeypatch.setattr(
            pm, "send_to_target", lambda target, text, enter=True: sent.append((target, text))
        )

        assert usage_limit.resume_session(state) is True
        assert sent == [("s1.0", usage_limit.RESUME_NUDGE)]
        assert not usage_limit.is_parked("s1")
        assert any(e["event"] == "session_resumed" for e in events())

    def test_resume_dead_session_archives_orphaned(self, monkeypatch):
        state = self._parked()
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: False)
        assert usage_limit.resume_session(state) is False
        assert not usage_limit.is_parked("s1")
        archived = json.loads(next(usage_limit.DONE_DIR.glob("*.json")).read_text())
        assert archived["status"] == "orphaned"

    def test_resume_failure_increments_attempts(self, monkeypatch):
        state = self._parked()
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: True)
        monkeypatch.setattr(usage_limit, "_capture", lambda *a, **k: "no echo here")
        import agentwire.pane_manager as pm
        monkeypatch.setattr(pm, "send_to_target", lambda *a, **k: None)

        assert usage_limit.resume_session(state) is False
        assert usage_limit.read_park_state("s1")["resume_attempts"] == 1

    def test_resume_gives_up_after_max_attempts(self, monkeypatch):
        state = self._parked(resume_attempts=usage_limit.MAX_RESUME_ATTEMPTS - 1)
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: True)
        monkeypatch.setattr(usage_limit, "_capture", lambda *a, **k: "")
        import agentwire.pane_manager as pm
        monkeypatch.setattr(pm, "send_to_target", lambda *a, **k: None)

        assert usage_limit.resume_session(state) is False
        assert not usage_limit.is_parked("s1")
        archived = json.loads(next(usage_limit.DONE_DIR.glob("*.json")).read_text())
        assert archived["status"] == "resume_failed"

    def test_resume_due_only_past_resume_at(self, monkeypatch):
        self._parked(session="due", resume_at="2026-06-11T03:42:00+00:00")
        self._parked(session="later", resume_at="2026-06-11T09:00:00+00:00")
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: True)
        resumed_calls = []

        def fake_resume(state, force=False):
            resumed_calls.append(state["session"])
            usage_limit.archive_state(state, "resumed")
            return True

        monkeypatch.setattr(usage_limit, "resume_session", fake_resume)

        now = datetime(2026, 6, 11, 4, 0, tzinfo=timezone.utc)
        assert usage_limit.resume_due(now) == ["due"]
        assert resumed_calls == ["due"]
        assert usage_limit.is_parked("later")

    def test_resume_due_archives_orphans_early(self, monkeypatch):
        self._parked(session="gone", resume_at="2026-06-11T09:00:00+00:00")
        monkeypatch.setattr(usage_limit, "_session_exists", lambda s: False)
        now = datetime(2026, 6, 11, 4, 0, tzinfo=timezone.utc)
        assert usage_limit.resume_due(now) == []
        assert not usage_limit.is_parked("gone")


# =============================================================================
# Sweep
# =============================================================================


class TestSweep:
    def test_sweep_parks_dialog_panes(self, monkeypatch):
        panes = "work\t0\tnode\nidle\t0\tzsh\nworker\t2\tclaude"
        screens = {
            "work.0": REAL_DIALOG,
            "worker.2": "normal output",
        }

        def fake_tmux(args, timeout=5):
            if args[0] == "list-panes":
                return subprocess.CompletedProcess(args, 0, stdout=panes, stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(usage_limit, "_tmux", fake_tmux)
        monkeypatch.setattr(
            usage_limit, "_capture",
            lambda target, scrollback=None: screens.get(target, ""),
        )
        parked_calls = []
        monkeypatch.setattr(
            usage_limit, "park",
            lambda session, pane_index=0, source="watchdog": parked_calls.append(
                (session, pane_index)
            ) or {"session": session},
        )

        result = usage_limit.sweep()
        assert parked_calls == [("work", 0)]  # zsh pane skipped, normal pane no match
        assert [s["session"] for s in result] == ["work"]

    def test_sweep_logs_unmatched_dialog_once(self, monkeypatch):
        panes = "odd\t0\tnode"

        def fake_tmux(args, timeout=5):
            if args[0] == "list-panes":
                return subprocess.CompletedProcess(args, 0, stdout=panes, stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(usage_limit, "_tmux", fake_tmux)
        monkeypatch.setattr(
            usage_limit, "_capture", lambda target, scrollback=None: OTHER_DIALOG
        )

        usage_limit.sweep()
        usage_limit.sweep()  # same screen → no duplicate event
        unmatched = [e for e in events() if e["event"] == "unmatched_dialog"]
        assert len(unmatched) == 1
        assert "Yes, proceed" in unmatched[0]["excerpt"]

    def test_sweep_skips_already_parked(self, monkeypatch):
        usage_limit.write_park_state({"session": "work", "status": "parked"})
        panes = "work\t0\tnode"

        def fake_tmux(args, timeout=5):
            if args[0] == "list-panes":
                return subprocess.CompletedProcess(args, 0, stdout=panes, stderr="")
            raise AssertionError("should not capture a parked session")

        monkeypatch.setattr(usage_limit, "_tmux", fake_tmux)
        assert usage_limit.sweep() == []


# =============================================================================
# Tick
# =============================================================================


class TestTick:
    def test_tick_runs_sweep_then_resume(self, monkeypatch):
        order = []
        monkeypatch.setattr(
            usage_limit, "sweep", lambda: order.append("sweep") or []
        )
        monkeypatch.setattr(
            usage_limit, "resume_due", lambda now=None: order.append("resume") or []
        )
        result = usage_limit.tick()
        assert order == ["sweep", "resume"]
        assert result == {"parked": [], "resumed": [], "waiting": []}
