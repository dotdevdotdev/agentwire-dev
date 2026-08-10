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


class TestBuddyLease:
    def test_registering_leases_fleet_alerts(self, isolate):
        identity.register("buddy")
        assert fleet_alerts.subscribers() == ["buddy"]

    def test_registering_does_not_disturb_the_identity_record(self, isolate):
        identity.register("buddy", model="m", voice="v")
        meta = core.load_session_metadata("buddy")
        assert meta["kind"] == identity.KIND
        assert meta[delivery.DELIVERY_KEY] == delivery.VOICE_ADAPTER
        assert meta["realtime_model"] == "m"

    def test_unregistering_stops_production(self, isolate):
        identity.register("buddy")
        identity.unregister("buddy")
        assert fleet_alerts.subscribers() == []

    def test_serving_renews_the_lease(self, isolate, monkeypatch):
        """A lease expires; a buddy that is being STARTED plainly still wants mail.

        Renewal at serve is what keeps the dormancy bound from punishing an
        active buddy — register alone would mean a buddy started a fortnight
        after registration hears nothing at all.
        """
        from agentwire import buddy_cli
        from agentwire.voice_layer import server

        identity.register("buddy")
        meta = core.load_session_metadata("buddy")
        meta[fleet_alerts.SUBSCRIBE_KEY]["expires_at"] = "2020-01-01T00:00:00+00:00"
        core.store_session_metadata("buddy", meta)
        assert fleet_alerts.subscribers() == []

        monkeypatch.setattr(
            server, "serve", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        with pytest.raises(KeyboardInterrupt):
            buddy_cli.cmd_buddy_serve(
                SimpleNamespace(name="buddy", port=1, model="", voice="", json=False)
            )
        assert fleet_alerts.subscribers() == ["buddy"]


class TestAlertingCannotBreakTheBuddy:
    """The alerting subsystem must never be able to stop the thing it alerts on.

    Every detector call site is wrapped; these two were not, and they are the
    two that can actually take something down. ``store_session_metadata`` raises
    BY DESIGN (#885), so an unwritable store turned a subscription — a strictly
    optional extra — into a failed registration and a bridge that refuses to
    serve.
    """

    @pytest.fixture
    def broken_subscribe(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("session store is read-only")

        monkeypatch.setattr(fleet_alerts, "subscribe", boom)

    def test_register_still_completes_and_stays_atomic(self, isolate, broken_subscribe):
        identity.register("buddy")
        assert identity.is_registered("buddy")
        # The half that used to be skipped when the subscribe raised between
        # the record write and the directory creation.
        assert identity.inbox_dir("buddy").is_dir()
        assert delivery.session_state_dir("buddy").is_dir()

    def test_serve_still_serves(self, isolate, broken_subscribe, monkeypatch):
        from agentwire import buddy_cli
        from agentwire.voice_layer import server

        identity.register("buddy")
        served: list = []
        monkeypatch.setattr(
            server, "serve", lambda *a, **k: served.append(1) or (_ for _ in ()).throw(
                KeyboardInterrupt()
            ),
        )
        with pytest.raises(KeyboardInterrupt):
            buddy_cli.cmd_buddy_serve(
                SimpleNamespace(name="buddy", port=1, model="", voice="", json=False)
            )
        assert served == [1]


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
