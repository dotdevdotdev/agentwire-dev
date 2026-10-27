"""The buddy is the interrupt tier's first consumer — and now it has a producer.

`kind: escalation` shipped riding `canInterrupt` (#967/#971) with nothing in the
fleet ever sending one: an alarm bell wired to nothing. The generic half of the
fix is `fleet_alerts` (main-bound, tested in ``test_fleet_alerts.py``); this file
covers the buddy-specific half — leasing the subscription — and walks one alert
all the way to the spool the voice layer actually reads, because "a detector
produced it" and "the buddy can hear it" are two different claims and only the
second one is the point.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentwire import auth_expired, core, fleet_alerts, inbox
from agentwire.voice_layer import delivery, identity


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    root = tmp_path / "agentwire"
    (root / "sessions").mkdir(parents=True)
    monkeypatch.setattr(core, "CONFIG_DIR", root)
    monkeypatch.setattr(inbox, "INBOX_ROOT", root / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", root / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
    return root


class TestReachesTheSpool:
    def test_an_outage_escalation_lands_in_the_spool_the_buddy_reads(
        self, isolate, monkeypatch
    ):
        """End to end: detector -> typed mail -> drain -> spool, kind intact.

        The kind has to survive the whole trip. It is what `canInterrupt` keys
        on, so an alert that arrives as a `note` is indistinguishable from the
        state before #982 — heard eventually, at a gap, which for an outage
        gating every dispatch on the machine is the wrong tier.
        """
        identity.register("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=False, error="no key"),
        )
        auth_expired.record_outage({"session": "task-a", "transcript": "/t.jsonl"})

        result = inbox.flush_session("buddy")
        assert result.get("delivered") == 1

        spooled = delivery.read_spool("buddy", unread_only=True, ack=False)
        assert len(spooled) == 1
        assert spooled[0]["kind"] == "escalation"
        assert spooled[0]["from"] == fleet_alerts.SENDER
        assert "login" in spooled[0]["text"].lower()

    def test_no_buddy_registered_means_nothing_is_produced(self, isolate, monkeypatch):
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=False, error="no key"),
        )
        auth_expired.record_outage({"session": "task-a", "transcript": "/t.jsonl"})
        assert not (isolate / "inbox").exists()
