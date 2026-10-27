"""Tests for usage-limit dialog recovery (agentwire/usage_limit.py).

The dialog fixture is the real pane capture from the 2026-06-10 incident
(two scheduler verification runs parked on the dialog for ~11 hours).
"""

import json
import subprocess
from datetime import datetime, timezone

import pytest

from agentwire import usage_limit

# Original reader, captured before the autouse fixture stubs it per-test.


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

# A live menu that is NOT the usage-limit dialog (drift / other dialogs).
OTHER_DIALOG = """\
  What do you want to do?

  ❯ 1. Yes, proceed
    2. No, cancel

  Enter to confirm · Esc to cancel
"""

# Narrow pane: the option line wraps mid-phrase.


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point all state/event paths into tmp and never send real email."""
    state_dir = tmp_path / "usage-limit"
    monkeypatch.setattr(usage_limit, "STATE_DIR", state_dir)
    monkeypatch.setattr(usage_limit, "DONE_DIR", state_dir / "done")
    monkeypatch.setattr(usage_limit, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(usage_limit, "_send_notification", lambda *a, **k: False)
    monkeypatch.setattr(usage_limit.time, "sleep", lambda s: None)
    # Default knobs — individual tests override to exercise the config gate.
    monkeypatch.setattr(usage_limit, "_recovery_config", lambda: (True, set()))
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

    def test_other_menus_dont_match(self):
        assert usage_limit.detect_dialog(OTHER_DIALOG) is False


# =============================================================================
# Reset time parsing
# =============================================================================


# =============================================================================
# State files
# =============================================================================


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
            usage_limit,
            "_capture",
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


# =============================================================================
# Sweep
# =============================================================================


# =============================================================================
# check_and_park (ensure's fast-path probe)
# =============================================================================


# =============================================================================
# Config knobs (usage_limit: section in config.yaml)
# =============================================================================


# =============================================================================
# Status / exit-code mappings (ensure + scheduler integration)
# =============================================================================


# =============================================================================
# Tick
# =============================================================================
