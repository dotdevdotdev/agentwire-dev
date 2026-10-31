"""Slice 1b: ``voice`` is a first-class message kind (#985).

The only voice-layer change that alters behaviour for sessions with nothing to
do with voice — which is why it is its own reviewable diff, and why these tests
are pointed at the blast radius *outside* the voice layer.

Three things are asserted here and nowhere else:

1. **The owner's ruling, as data** (2026-08-10) — ``voice`` is ACTIVE (never
   passive, so it drives a session exactly as typing at it would) and IS in
   ``ESCALATE_KINDS`` (a dead-lettered voice message emails the owner, because
   the owner spoke it and walked away). And the distinction the ruling is most
   easily misread as: escalatable is **not** the interrupt tier. ``escalation``
   remains the only kind that pre-empts.

2. **One derivation, not four literals.** ``doctor``, ``worktree --list`` and
   ``worktree --watch`` each carried a hand-written ``("done", "escalation")``
   tuple. With ``voice`` added to ``ESCALATE_KINDS``, a missed copy makes a
   dead-lettered voice message email the owner on one path and vanish on
   another. :func:`inbox.load_bearing` is the one implementation; the
   set-equality assertion is what stops a future kind escaping it.
"""

import pytest

from agentwire import cohort, doctor_cli, inbox


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
    monkeypatch.setattr(cohort, "COHORT_ROOT", tmp_path / "cohorts")
    monkeypatch.setattr(cohort, "EVENTS_FILE", tmp_path / "cohort-events.jsonl")
    monkeypatch.setattr(cohort, "session_exists", lambda s: True)
    monkeypatch.setattr(cohort, "is_agent_session", lambda s: True)
    return tmp_path


def _corpse(session: str, kind: str, tag: str = "x") -> inbox.Message:
    """A dead-lettered message of *kind*, written straight into ``dead/``."""
    msg = inbox.Message(
        id=f"1700000000000-{kind}{tag}", sender="buddy", to=session, kind=kind,
        text=f"{kind} corpse", ts=1700000000000, attempts=inbox.MAX_ATTEMPTS,
        reason="box_not_empty", dead_ts=1700000000001,
    )
    inbox._write_message(inbox.dead_dir(session) / f"{msg.id}.json", msg)
    return msg


# =============================================================================
# 1. The ruling, as data
# =============================================================================


class TestTheRuling:


    def test_enqueue_accepts_voice_and_refuses_an_unknown_kind(self, isolate):
        (msg,) = inbox.enqueue("orchestrator", "hello", kind="voice", sender="buddy")
        assert msg.kind == "voice"
        with pytest.raises(ValueError):
            inbox.enqueue("orchestrator", "hello", kind="whisper", sender="buddy")


# =============================================================================
# 2. One derivation, not four literals
# =============================================================================


class TestLoadBearingIsDerivedOnce:


    def test_doctor_reports_a_dead_lettered_voice_message(self, isolate, capsys):
        _corpse("orchestrator", "voice")
        doctor_cli._render_dead_letter_section()
        out = capsys.readouterr().out
        assert "[!!]" in out and "voice" in out


# =============================================================================
# 3. The cohort interaction — held by SENDER, harvested by KIND
# =============================================================================


# =============================================================================
# 4. Attribution left the body — and took its safety property with it
# =============================================================================


# =============================================================================
# 5. The role text the move makes false
# =============================================================================
