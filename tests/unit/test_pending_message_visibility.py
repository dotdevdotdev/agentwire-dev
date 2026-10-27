"""Long-pending report-backs are surfaced by doctor (#879).

A penalty-free defer never dead-letters, so it never reaches doctor's
dead-letter section OR the owner email that section's existence justifies.
#872 made that gap load-bearing: ``target_parked`` legitimately defers for
hours, so a worker's ``done`` can now sit in a parked parent's queue with
nothing announcing it in either direction.

Covers ``inbox.stale_pending`` (which messages qualify) and
``doctor_cli._render_pending_messages_section`` (how they're reported).
"""

import pytest

from agentwire import inbox

HOUR_MS = 3_600_000


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    monkeypatch.setattr(inbox, "INBOX_ROOT", root)
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    return root


def _age(session: str, hours: float, reason: str = "target_parked", **kw):
    """Enqueue a message and backdate it *hours* into the past."""
    msg = inbox.enqueue(
        session,
        kw.pop("text", "PR done"),
        kind=kw.pop("kind", "done"),
        sender=kw.pop("sender", "worker"),
    )[0]
    msg.ts = inbox._now_ms() - int(hours * HOUR_MS)
    msg.reason = reason
    inbox._write_message(msg.path, msg)
    return msg


class TestStalePending:
    def test_message_older_than_threshold_is_reported(self, isolate):
        _age("s", hours=5)
        stale = inbox.stale_pending()
        assert len(stale) == 1
        session, msg = stale[0]
        assert session == "s"
        assert msg.kind == "done" and msg.reason == "target_parked"
