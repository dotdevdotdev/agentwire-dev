"""Fleet detectors produce typed-kind mail; the interrupt tier gets a producer (#982).

Two halves are tested here and they fail in opposite directions:

* **The false-REJECT half** — a detector that fires and reaches nobody. That is
  the state before this module existed: `kind: escalation` rode `canInterrupt`
  and no fleet detector ever sent one, so the alarm bell was wired to nothing.
* **The false-ACCEPT half, which is the expensive one.** `escalation` is the
  only kind allowed to cut across the buddy's own speech. A detector that
  over-produces escalations does not merely add noise — it destroys the tier,
  because the owner learns to ignore it. So the rulings in
  :data:`fleet_alerts.DETECTOR_KINDS` are pinned here as DATA: changing what
  earns an interrupt has to be a deliberate edit to a test that says why.

Nothing here asserts anything about *speed*. Against an announcer item an
escalation still queues behind it plus up to one 6s in-flight deferral and up
to three owner-speaking ones (~30s worst case); pre-emption is real only
against a VAD response. "Escalation" means "worth cutting the buddy off within
half a minute", never "immediately".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agentwire import auth_expired, core, fleet_alerts, inbox, prompt_router, usage_limit


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    """A throwaway config dir: session records, inboxes and event logs."""
    root = tmp_path / "agentwire"
    (root / "sessions").mkdir(parents=True)
    monkeypatch.setattr(core, "CONFIG_DIR", root)
    monkeypatch.setattr(inbox, "INBOX_ROOT", root / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", root / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
    return root


def _subscribe(name: str = "buddy") -> str:
    fleet_alerts.subscribe(name)
    return name


def _inbox_messages(session: str) -> list:
    return inbox.list_messages(session) + inbox.list_ingest(session)


# =============================================================================
# The ruling — which detector earns which kind
# =============================================================================


class TestRuling:
    def test_only_two_detectors_may_interrupt(self):
        """Escalation is the interrupt tier; exactly two producers hold it.

        Both share one property: the condition cannot clear without a human,
        and it is burning something while it waits. auth-expired refuses every
        turn on the machine until `/login`; a root session blocked on a prompt
        with no parent is stalled with nobody able to answer it.
        """
        interrupting = {
            name for name, kind in fleet_alerts.DETECTOR_KINDS.items()
            if kind == "escalation"
        }
        assert interrupting == {"auth_expired", "blocked_pane_no_parent"}

    def test_usage_limit_park_is_a_note(self):
        """A parked session is self-healing — reset parsed, auto-resume armed.

        Nothing is asked of the owner ("no action needed" is literally in the
        email), so it fails the interrupt test even though it is a real fleet
        event worth hearing about at a gap.
        """
        assert fleet_alerts.DETECTOR_KINDS["usage_limit_park"] == "note"

    def test_dead_letter_floor_is_request_not_escalation(self):
        """A lost report-back needs owner attention, not the owner's sentence.

        The batch can inherit `escalation` when what was LOST was itself an
        escalation (tested below) — but the floor is `request`, because the
        stuck-recipient case dead-lettered 147 messages in ~2s once and that
        shape must not be able to buy 147 interrupts.
        """
        assert fleet_alerts.DETECTOR_KINDS["dead_letter"] == "request"

    def test_dangling_pr_is_deliberately_unwired(self):
        """Not an oversight — a ruling.

        `worktree --dangling` has no autonomous trigger (only `doctor` and the
        explicit flag, both run by a human who is already looking) and no
        per-finding throttle state to reuse, so a producer there would re-alert
        the same durable, passive condition every invocation. Wiring it means
        revisiting this test.
        """
        assert "dangling_pr" not in fleet_alerts.DETECTOR_KINDS

    def test_every_ruling_names_a_real_kind(self):
        for name, kind in fleet_alerts.DETECTOR_KINDS.items():
            assert kind in inbox.KINDS, name


# =============================================================================
# Subscription — a lease, not a permanent flag
# =============================================================================


class TestSubscription:
    def test_no_subscriber_means_no_behavior_change(self, isolate):
        assert fleet_alerts.subscribers() == []
        assert fleet_alerts.emit("anything", kind="escalation") == []
        assert not (isolate / "inbox").exists()

    def test_subscribe_records_a_lease_and_is_listed(self, isolate):
        _subscribe("buddy")
        assert fleet_alerts.subscribers() == ["buddy"]
        record = core.load_session_metadata("buddy")[fleet_alerts.SUBSCRIBE_KEY]
        assert record["expires_at"] > record["since"]

    def test_subscribe_preserves_the_rest_of_the_record(self, isolate):
        core.store_session_metadata("buddy", {"role": "buddy", "delivery": "voice"})
        _subscribe("buddy")
        meta = core.load_session_metadata("buddy")
        assert meta["role"] == "buddy" and meta["delivery"] == "voice"

    def test_an_expired_lease_stops_producing(self, isolate):
        """The dormancy bound. A buddy that ran once in July must not collect
        August's escalations in a spool it will replay at next start."""
        _subscribe("buddy")
        stale = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        meta = core.load_session_metadata("buddy")
        meta[fleet_alerts.SUBSCRIBE_KEY]["expires_at"] = stale
        core.store_session_metadata("buddy", meta)

        assert fleet_alerts.subscribers() == []
        assert fleet_alerts.emit("x", kind="escalation") == []

    def test_a_malformed_subscription_is_ignored_not_honored(self, isolate):
        core.store_session_metadata("buddy", {fleet_alerts.SUBSCRIBE_KEY: True})
        assert fleet_alerts.subscribers() == []

    def test_unsubscribe(self, isolate):
        _subscribe("buddy")
        assert fleet_alerts.unsubscribe("buddy") is True
        assert fleet_alerts.subscribers() == []
        assert fleet_alerts.unsubscribe("buddy") is False


# =============================================================================
# emit — best-effort, never the detector's problem
# =============================================================================


class TestEmit:
    def test_enqueues_to_every_subscriber(self, isolate):
        _subscribe("buddy")
        _subscribe("second")
        assert sorted(fleet_alerts.emit("hi", kind="note")) == ["buddy", "second"]
        msg = _inbox_messages("buddy")[0]
        assert msg.kind == "note" and msg.sender == fleet_alerts.SENDER
        assert msg.text == "hi"

    def test_exclude_skips_a_target(self, isolate):
        _subscribe("buddy")
        assert fleet_alerts.emit("hi", kind="note", exclude=["buddy"]) == []

    def test_never_raises_when_the_inbox_fails(self, isolate, monkeypatch):
        _subscribe("buddy")

        def boom(*a, **k):
            raise OSError("disk gone")

        monkeypatch.setattr(inbox, "enqueue", boom)
        assert fleet_alerts.emit("hi", kind="note") == []

    def test_one_bad_target_does_not_abandon_the_rest(self, isolate, monkeypatch):
        _subscribe("aaa")
        _subscribe("zzz")
        real = inbox.enqueue

        def selective(to, *a, **k):
            if to == "aaa":
                raise OSError("nope")
            return real(to, *a, **k)

        monkeypatch.setattr(inbox, "enqueue", selective)
        assert fleet_alerts.emit("hi", kind="note") == ["zzz"]

    def test_a_bogus_kind_is_a_coding_bug_not_a_silent_drop(self, isolate):
        _subscribe("buddy")
        with pytest.raises(ValueError):
            fleet_alerts.emit("hi", kind="urgent")


# =============================================================================
# Detector: expired login (#906) — escalation, once per outage hour
# =============================================================================


class TestAuthExpired:
    def test_records_outage_and_escalates_to_the_buddy(self, isolate, monkeypatch):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=False, error="no key"),
        )
        auth_expired.record_outage({"session": "task-a", "transcript": "/t.jsonl"})

        msgs = _inbox_messages("buddy")
        assert len(msgs) == 1
        assert msgs[0].kind == "escalation"
        assert "login" in msgs[0].text.lower()

    def test_throttled_by_the_same_state_record(self, isolate, monkeypatch):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=False, error="no key"),
        )
        auth_expired.record_outage({"session": "a", "transcript": "/t.jsonl"})
        auth_expired.record_outage({"session": "b", "transcript": "/t.jsonl"})
        assert len(_inbox_messages("buddy")) == 1

        state = json.loads(auth_expired.state_path().read_text())
        assert state["alerted_at"]

        # ...and it fires again once the window is over.
        state["alerted_at"] = (
            datetime.now(timezone.utc) - auth_expired.ESCALATE_TTL - timedelta(minutes=1)
        ).isoformat()
        auth_expired.write_state(state)
        auth_expired.record_outage({"session": "c", "transcript": "/t.jsonl"})
        assert len(_inbox_messages("buddy")) == 2

    def test_a_broken_alert_never_breaks_the_gate(self, isolate, monkeypatch):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=False, error="no key"),
        )
        monkeypatch.setattr(
            fleet_alerts, "emit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
        )
        state = auth_expired.record_outage({"session": "a", "transcript": "/t.jsonl"})
        assert state["last_seen"] and auth_expired.outage_active()


# =============================================================================
# Detector: usage-limit park — a note, once per park
# =============================================================================


class TestUsageLimitPark:
    def _state(self) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "session": "worker-1",
            "task": "nightly",
            "detected_at": now.isoformat(),
            "parked_at": now.isoformat(),
            "reset_at": (now + timedelta(hours=2)).isoformat(),
            "resume_at": (now + timedelta(hours=2, minutes=5)).isoformat(),
            "excerpt": "",
            "notified": False,
        }

    def test_park_notice_reaches_the_buddy_as_a_note(self, isolate, monkeypatch):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        usage_limit._notify_parked(self._state())

        msgs = _inbox_messages("buddy")
        assert len(msgs) == 1
        assert msgs[0].kind == "note"
        assert "worker-1" in msgs[0].text

    def test_the_notice_survives_a_dead_email_channel(self, isolate, monkeypatch):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: (_ for _ in ()).throw(RuntimeError("no provider")),
        )
        usage_limit._notify_parked(self._state())
        assert len(_inbox_messages("buddy")) == 1


# =============================================================================
# Detector: dead-lettered load-bearing mail — inherits the lost kind
# =============================================================================


def _dead(kind: str, to: str = "someone", sender: str = "worker") -> inbox.Message:
    return inbox.Message(
        id="1-abc", sender=sender, to=to, kind=kind, text="report", ts=1, attempts=40
    )


class TestDeadLetters:
    def test_a_lost_done_is_a_request(self, isolate, monkeypatch):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters([_dead("done")], "target_gone")
        msgs = _inbox_messages("buddy")
        assert len(msgs) == 1 and msgs[0].kind == "request"

    def test_a_lost_escalation_stays_an_escalation(self, isolate, monkeypatch):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters([_dead("done"), _dead("escalation")], "target_gone")
        assert _inbox_messages("buddy")[0].kind == "escalation"

    def test_the_buddys_own_undelivered_mail_does_not_loop(self, isolate, monkeypatch):
        """The recursion guard, in both directions.

        Alerts addressed TO the buddy that dead-letter would otherwise alert
        the buddy about the alert failing to reach the buddy — forever.
        """
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters(
            [_dead("escalation", to="buddy", sender=fleet_alerts.SENDER)], "target_gone"
        )
        assert _inbox_messages("buddy") == []

    def test_a_stranded_alert_is_not_reported_to_a_second_subscriber(
        self, isolate, monkeypatch
    ):
        """The half the recipient guard cannot cover.

        With two subscribers, an alert stranded on the way to `buddy` is not
        addressed to `second` — so excluding recipients alone would let it be
        reported there, once per drain, about a delivery that is stuck for the
        same reason `second`'s own copy is. Only the SENDER guard stops it.
        """
        _subscribe("buddy")
        _subscribe("second")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters(
            [_dead("escalation", to="buddy", sender=fleet_alerts.SENDER)], "target_gone"
        )
        assert _inbox_messages("second") == []

    def test_mail_lost_on_the_way_to_the_buddy_still_excludes_it(
        self, isolate, monkeypatch
    ):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters([_dead("done", to="buddy")], "target_gone")
        assert _inbox_messages("buddy") == []

    def test_one_alert_per_batch_not_per_message(self, isolate, monkeypatch):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters([_dead("done") for _ in range(147)], "target_gone")
        msgs = _inbox_messages("buddy")
        assert len(msgs) == 1
        assert "147" in msgs[0].text


# =============================================================================
# Detector: a root session blocked with nowhere to route (#905) — escalation
# =============================================================================


def _prompt_info() -> prompt_router.PromptInfo:
    return prompt_router.PromptInfo(
        kind="permission",
        question="Allow Bash(rm -rf build)?",
        options=[{"number": "1", "label": "Yes"}, {"number": "2", "label": "No"}],
        summary="",
    )


class TestBlockedRootPane:
    def test_no_parent_escalation_reaches_the_buddy(self, isolate, monkeypatch):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        prompt_router._escalate_no_parent("root-1", 0, _prompt_info(), None)

        msgs = _inbox_messages("buddy")
        assert len(msgs) == 1
        assert msgs[0].kind == "escalation"
        assert "root-1" in msgs[0].text

    def test_throttled_by_the_markers_own_stamp(self, isolate, monkeypatch):
        _subscribe("buddy")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        info = _prompt_info()
        stamp = prompt_router._escalate_no_parent("root-1", 0, info, None)
        prompt_router._escalate_no_parent("root-1", 0, info, {"escalated_at": stamp})
        assert len(_inbox_messages("buddy")) == 1
