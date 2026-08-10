"""Fleet detectors produce typed-kind mail; `escalation` gets a producer (#982).

Two halves are tested here and they fail in opposite directions:

* **The false-REJECT half** — a detector that fires and reaches nobody. That is
  the state before this module existed: the one kind a recipient may act on out
  of turn had no fleet detector sending it, so the bell was wired to nothing.
* **The false-ACCEPT half, which is the expensive one.** A detector that
  over-produces escalations does not merely add noise — it destroys the tier,
  because a recipient who learns escalations are usually ignorable will ignore
  the one that wasn't. So the rulings in :data:`fleet_alerts.DETECTOR_KINDS`
  are pinned here as DATA: changing what may interrupt has to be a deliberate
  edit to a test that says why.

Nothing here asserts anything about *speed*, and nothing should. An alert is
ordinary inbox mail, so it waits for the drain (a 60s watchdog tick) before any
recipient sees it: `escalation` is a statement of priority, not of latency.
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


def _subscribe(name: str = "listener") -> str:
    """A subscriber is a session that EXISTS — the record comes first.

    Not fixture ceremony: a subscription is a property of a session, and
    ``subscribe`` refuses a name with no record precisely so it can never mint
    one into the #871 store.
    """
    if not core.session_metadata_path(name).exists():
        core.store_session_metadata(name, {"role": "worker", "created_at": "x"})
    fleet_alerts.subscribe(name)
    return name


def _inbox_messages(session: str) -> list:
    return inbox.list_messages(session) + inbox.list_ingest(session)


# =============================================================================
# The ruling — which detector earns which kind
# =============================================================================


class TestRuling:
    def test_only_two_detectors_may_interrupt(self):
        """Escalation is the act-out-of-turn kind; exactly two producers hold it.

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
        _subscribe("listener")
        assert fleet_alerts.subscribers() == ["listener"]
        record = core.load_session_metadata("listener")[fleet_alerts.SUBSCRIBE_KEY]
        assert record["expires_at"] > record["since"]

    def test_subscribe_preserves_the_rest_of_the_record(self, isolate):
        core.store_session_metadata("listener", {"role": "worker", "created_at": "x"})
        _subscribe("listener")
        meta = core.load_session_metadata("listener")
        assert meta["role"] == "worker" and meta["created_at"] == "x"

    def test_an_expired_lease_stops_producing(self, isolate):
        """The dormancy bound. A listener that ran once in July must not collect
        August's escalations in a queue it hands over all at once at next
        start."""
        _subscribe("listener")
        stale = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        meta = core.load_session_metadata("listener")
        meta[fleet_alerts.SUBSCRIBE_KEY]["expires_at"] = stale
        core.store_session_metadata("listener", meta)

        assert fleet_alerts.subscribers() == []
        assert fleet_alerts.emit("x", kind="escalation") == []

    def test_a_malformed_subscription_is_ignored_not_honored(self, isolate):
        core.store_session_metadata("listener", {fleet_alerts.SUBSCRIBE_KEY: True})
        assert fleet_alerts.subscribers() == []

    def test_subscribing_an_unknown_session_refuses_and_writes_nothing(self, isolate):
        """The session-record store is the #871 SSOT for conversation identity.

        A verb that mints ``{}`` into it on a typo is the wrong shape whatever
        the typo case: the empty record is indistinguishable from a real one to
        ``core.recorded_sessions()``, which sweeps and doctor surfaces count.
        The help text already promised a record must exist; now the code does.
        """
        with pytest.raises(ValueError):
            fleet_alerts.subscribe("typoo")
        assert core.recorded_sessions() == []
        assert not core.session_metadata_path("typoo").exists()
        assert fleet_alerts.subscribers() == []

    def test_a_refused_subscription_leaves_no_index_entry(self, isolate):
        _subscribe("listener")
        with pytest.raises(ValueError):
            fleet_alerts.subscribe("typoo")
        assert fleet_alerts.subscribers() == ["listener"]

    def test_unsubscribing_an_unknown_session_creates_no_record(self, isolate):
        assert fleet_alerts.unsubscribe("typoo") is False
        assert core.recorded_sessions() == []

    def test_unsubscribe(self, isolate):
        _subscribe("listener")
        assert fleet_alerts.unsubscribe("listener") is True
        assert fleet_alerts.subscribers() == []
        assert fleet_alerts.unsubscribe("listener") is False


# =============================================================================
# emit — best-effort, never the detector's problem
# =============================================================================


class TestEmit:
    def test_enqueues_to_every_subscriber(self, isolate):
        _subscribe("listener")
        _subscribe("second")
        assert sorted(fleet_alerts.emit("hi", kind="note")) == ["listener", "second"]
        msg = _inbox_messages("listener")[0]
        assert msg.kind == "note" and msg.sender == fleet_alerts.SENDER
        assert msg.text == "hi"

    def test_exclude_skips_a_target(self, isolate):
        _subscribe("listener")
        assert fleet_alerts.emit("hi", kind="note", exclude=["listener"]) == []

    def test_never_raises_when_the_inbox_fails(self, isolate, monkeypatch):
        _subscribe("listener")

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
        _subscribe("listener")
        with pytest.raises(ValueError):
            fleet_alerts.emit("hi", kind="urgent")


# =============================================================================
# Detector: expired login (#906) — escalation, once per outage hour
# =============================================================================


class TestAuthExpired:
    def test_records_outage_and_escalates_to_the_listener(self, isolate, monkeypatch):
        _subscribe("listener")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=False, error="no key"),
        )
        auth_expired.record_outage({"session": "task-a", "transcript": "/t.jsonl"})

        msgs = _inbox_messages("listener")
        assert len(msgs) == 1
        assert msgs[0].kind == "escalation"
        assert "login" in msgs[0].text.lower()

    def test_throttled_by_the_same_state_record(self, isolate, monkeypatch):
        _subscribe("listener")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=False, error="no key"),
        )
        auth_expired.record_outage({"session": "a", "transcript": "/t.jsonl"})
        auth_expired.record_outage({"session": "b", "transcript": "/t.jsonl"})
        assert len(_inbox_messages("listener")) == 1

        state = json.loads(auth_expired.state_path().read_text())
        assert state["alerted_at"]

        # ...and it fires again once the window is over.
        state["alerted_at"] = (
            datetime.now(timezone.utc) - auth_expired.ESCALATE_TTL - timedelta(minutes=1)
        ).isoformat()
        auth_expired.write_state(state)
        auth_expired.record_outage({"session": "c", "transcript": "/t.jsonl"})
        assert len(_inbox_messages("listener")) == 2

    def test_a_broken_alert_never_breaks_the_gate(self, isolate, monkeypatch):
        _subscribe("listener")
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

    def test_park_notice_reaches_the_listener_as_a_note(self, isolate, monkeypatch):
        _subscribe("listener")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        usage_limit._notify_parked(self._state())

        msgs = _inbox_messages("listener")
        assert len(msgs) == 1
        assert msgs[0].kind == "note"
        assert "worker-1" in msgs[0].text

    def test_the_notice_survives_a_dead_email_channel(self, isolate, monkeypatch):
        _subscribe("listener")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: (_ for _ in ()).throw(RuntimeError("no provider")),
        )
        usage_limit._notify_parked(self._state())
        assert len(_inbox_messages("listener")) == 1


# =============================================================================
# Detector: dead-lettered load-bearing mail — inherits the lost kind
# =============================================================================


def _dead(kind: str, to: str = "someone", sender: str = "worker") -> inbox.Message:
    return inbox.Message(
        id="1-abc", sender=sender, to=to, kind=kind, text="report", ts=1, attempts=40
    )


class TestDeadLetters:
    def test_a_lost_done_is_a_request(self, isolate, monkeypatch):
        _subscribe("listener")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters([_dead("done")], "target_gone")
        msgs = _inbox_messages("listener")
        assert len(msgs) == 1 and msgs[0].kind == "request"

    def test_a_lost_escalation_stays_an_escalation(self, isolate, monkeypatch):
        _subscribe("listener")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters([_dead("done"), _dead("escalation")], "target_gone")
        assert _inbox_messages("listener")[0].kind == "escalation"

    def test_the_listeners_own_undelivered_mail_does_not_loop(self, isolate, monkeypatch):
        """The recursion guard, in both directions.

        Alerts addressed TO the listener that dead-letter would otherwise alert
        the listener about the alert failing to reach the listener — forever.
        """
        _subscribe("listener")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters(
            [_dead("escalation", to="listener", sender=fleet_alerts.SENDER)], "target_gone"
        )
        assert _inbox_messages("listener") == []

    def test_a_stranded_alert_is_not_reported_to_a_second_subscriber(
        self, isolate, monkeypatch
    ):
        """The half the recipient guard cannot cover.

        With two subscribers, an alert stranded on the way to `listener` is not
        addressed to `second` — so excluding recipients alone would let it be
        reported there, once per drain, about a delivery that is stuck for the
        same reason `second`'s own copy is. Only the SENDER guard stops it.
        """
        _subscribe("listener")
        _subscribe("second")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters(
            [_dead("escalation", to="listener", sender=fleet_alerts.SENDER)], "target_gone"
        )
        assert _inbox_messages("second") == []

    def test_mail_lost_on_the_way_to_the_listener_still_excludes_it(
        self, isolate, monkeypatch
    ):
        _subscribe("listener")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters([_dead("done", to="listener")], "target_gone")
        assert _inbox_messages("listener") == []

    def test_one_alert_per_batch_not_per_message(self, isolate, monkeypatch):
        _subscribe("listener")
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        inbox._escalate_dead_letters([_dead("done") for _ in range(147)], "target_gone")
        msgs = _inbox_messages("listener")
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


class TestKeylessMachine:
    """The shape the first round of throttle tests could not build.

    Every throttle test above patches ``send_email`` to RETURN a result — and a
    machine with no ``RESEND_API_KEY`` does not return, it RAISES
    ``EmailConfigError`` before any send is attempted. That is the ordinary
    state of a fresh install and of every machine in this fleet without the key.
    The email-shaped throttles (``escalated_at`` on an exception path,
    ``notified`` on a successful send) never close there, so anything riding
    them re-fires every 60s watchdog tick — 720 escalations over a 12h lease.

    The alert therefore may NEVER ride an email-shaped throttle. Each producer
    stamps its own state on successful ENQUEUE, which is a local write.
    """

    @pytest.fixture(autouse=True)
    def keyless(self, monkeypatch):
        from agentwire.channels.email import EmailConfigError

        def raises(**kwargs):
            raise EmailConfigError("Email API key not configured.")

        monkeypatch.setattr("agentwire.channels.email.send_email", raises)

    def test_no_parent_sweep_alerts_once_not_once_per_tick(
        self, isolate, monkeypatch, tmp_path
    ):
        """The reproduction: 5 sweeps of ONE prompt must be 1 escalation."""
        _subscribe("listener")
        monkeypatch.setattr(prompt_router, "STATE_DIR", tmp_path / "prompt-router")
        monkeypatch.setattr(prompt_router, "EVENTS_FILE", tmp_path / "pr-events.jsonl")
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)

        for _ in range(5):
            prompt_router.route_prompt("root-1", 0, _prompt_info(), source="sweep")

        assert len(_inbox_messages("listener")) == 1

    def test_a_genuinely_new_prompt_still_alerts(self, isolate, monkeypatch, tmp_path):
        """The false-reject half: the throttle is per PROMPT, not per pane.

        A session that answers one blocking question and immediately hits a
        different one is still stalled, and the second question is new
        information. Only a redraw of the SAME prompt is suppressed.
        """
        _subscribe("listener")
        monkeypatch.setattr(prompt_router, "STATE_DIR", tmp_path / "prompt-router")
        monkeypatch.setattr(prompt_router, "EVENTS_FILE", tmp_path / "pr-events.jsonl")
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)

        prompt_router.route_prompt("root-1", 0, _prompt_info(), source="sweep")
        other = prompt_router.PromptInfo(
            kind="plan", question="Proceed with the migration plan?", options=[]
        )
        prompt_router.route_prompt("root-1", 0, other, source="sweep")

        assert len(_inbox_messages("listener")) == 2

    def test_auth_outage_alerts_once_across_repeated_detections(self, isolate):
        _subscribe("listener")
        for _ in range(5):
            auth_expired.record_outage({"session": "a", "transcript": "/t.jsonl"})
        assert len(_inbox_messages("listener")) == 1

    def test_park_note_fires_once_across_the_whole_park(
        self, isolate, monkeypatch, tmp_path
    ):
        """The multi-caller reproduction: 1 park + 10 watchdog ticks = 1 note.

        ``_notify_parked`` has TWO callers, not one: ``park`` and — for as long
        as ``notified`` stays False, which on a keyless machine is forever —
        ``resume_due``, on every tick.
        """
        _subscribe("listener")
        monkeypatch.setattr(usage_limit, "STATE_DIR", tmp_path / "usage-limit")
        now = datetime.now(timezone.utc)
        state = {
            "session": "worker-1",
            "task": "nightly",
            "detected_at": now.isoformat(),
            "parked_at": now.isoformat(),
            "reset_at": (now + timedelta(hours=2)).isoformat(),
            "resume_at": (now + timedelta(hours=2, minutes=5)).isoformat(),
            "excerpt": "",
            "notified": False,
        }
        usage_limit.write_park_state(state)

        usage_limit._notify_parked(state)  # park()
        for _ in range(10):  # resume_due(), once per tick, notified still False
            usage_limit._notify_parked(usage_limit.read_park_state("worker-1"))

        assert len(_inbox_messages("listener")) == 1

    def test_a_new_park_of_the_same_session_alerts_again(
        self, isolate, monkeypatch, tmp_path
    ):
        """The false-reject half: the stamp lives on the PARK, not the session."""
        _subscribe("listener")
        monkeypatch.setattr(usage_limit, "STATE_DIR", tmp_path / "usage-limit")
        now = datetime.now(timezone.utc)

        for _ in range(2):
            state = {
                "session": "worker-1",
                "detected_at": now.isoformat(),
                "parked_at": now.isoformat(),
                "reset_at": (now + timedelta(hours=2)).isoformat(),
                "resume_at": (now + timedelta(hours=2)).isoformat(),
                "excerpt": "",
                "notified": False,
            }
            usage_limit.write_park_state(state)
            usage_limit._notify_parked(state)
            usage_limit.state_path("worker-1").unlink()  # the park cleared

        assert len(_inbox_messages("listener")) == 2


class TestNoStampWhenNobodyHeard:
    """A stamp records that somebody WAS TOLD, never that we tried.

    The expensive direction: an operator who subscribes during a live incident
    must still hear about it. If a throttle stamp were burned while nobody was
    listening, the alert would be suppressed for the rest of its TTL and the
    subscriber would sit through the incident in silence — the failure that is
    hardest to notice, because the system looks exactly like a quiet fleet.

    ``if not reached: return previous`` is what makes that work. It was correct
    but unasserted, which for behaviour in this direction is the same as
    undefended.
    """

    @pytest.fixture(autouse=True)
    def keyless(self, monkeypatch):
        from agentwire.channels.email import EmailConfigError

        def raises(**kwargs):
            raise EmailConfigError("Email API key not configured.")

        monkeypatch.setattr("agentwire.channels.email.send_email", raises)

    def test_auth_outage_alerts_a_subscriber_that_arrives_mid_incident(self, isolate):
        for _ in range(3):
            auth_expired.record_outage({"session": "a", "transcript": "/t.jsonl"})
        assert json.loads(auth_expired.state_path().read_text())["alerted_at"] is None

        _subscribe("listener")  # a listener comes up mid-outage
        auth_expired.record_outage({"session": "a", "transcript": "/t.jsonl"})
        assert len(_inbox_messages("listener")) == 1

    def test_no_parent_sweep_alerts_a_subscriber_that_arrives_mid_incident(
        self, isolate, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(prompt_router, "STATE_DIR", tmp_path / "prompt-router")
        monkeypatch.setattr(prompt_router, "EVENTS_FILE", tmp_path / "pr-events.jsonl")
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)

        for _ in range(3):
            prompt_router.route_prompt("root-1", 0, _prompt_info(), source="sweep")
        assert prompt_router.read_marker("root-1", 0)["alerted_at"] is None

        _subscribe("listener")
        prompt_router.route_prompt("root-1", 0, _prompt_info(), source="sweep")
        assert len(_inbox_messages("listener")) == 1

    def test_park_notes_a_subscriber_that_arrives_mid_park(
        self, isolate, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(usage_limit, "STATE_DIR", tmp_path / "usage-limit")
        now = datetime.now(timezone.utc)
        state = {
            "session": "worker-1",
            "detected_at": now.isoformat(),
            "parked_at": now.isoformat(),
            "reset_at": (now + timedelta(hours=2)).isoformat(),
            "resume_at": (now + timedelta(hours=2)).isoformat(),
            "excerpt": "",
            "notified": False,
        }
        usage_limit.write_park_state(state)
        for _ in range(3):
            usage_limit._notify_parked(usage_limit.read_park_state("worker-1"))
        assert not usage_limit.read_park_state("worker-1").get("fleet_alerted")

        _subscribe("listener")
        usage_limit._notify_parked(usage_limit.read_park_state("worker-1"))
        assert len(_inbox_messages("listener")) == 1


class TestCostWhenNobodyListens:
    """"Zero behavior change with no subscriber" has to mean zero WORK, too.

    ``subscribers()`` originally walked every session record — 1155 of them on
    this machine, ~326ms — and one of its callers sits on the SYNCHRONOUS
    permission-hook path. A detector that taxes the product's hot path to
    discover that nobody is listening is a regression, not an inert feature.
    """

    @staticmethod
    def _count_walks(monkeypatch) -> list:
        """Count store walks. A RAISING stub cannot be used here: ``subscribers``
        swallows exceptions by design, so a raise would be caught and the test
        would pass whether or not the walk happened — a pin that cannot fail."""
        calls: list = []
        real = core.recorded_sessions

        def counted():
            calls.append(1)
            return real()

        monkeypatch.setattr(core, "recorded_sessions", counted)
        return calls

    def test_no_index_means_the_record_store_is_never_walked(self, isolate, monkeypatch):
        calls = self._count_walks(monkeypatch)
        assert fleet_alerts.subscribers() == []
        assert fleet_alerts.emit("x", kind="escalation") == []
        assert calls == []

    def test_one_subscriber_reads_only_that_subscribers_record(
        self, isolate, monkeypatch
    ):
        _subscribe("listener")
        for name in ("other-1", "other-2", "other-3"):
            core.store_session_metadata(name, {"role": "worker"})

        calls = self._count_walks(monkeypatch)
        assert fleet_alerts.subscribers() == ["listener"]
        assert calls == []

    def test_the_index_is_a_candidate_list_not_the_truth(self, isolate):
        """A stale index entry must not resurrect a dropped subscription.

        The record is authoritative; the index only says who to ask. A name
        whose lease is gone (unregistered, killed, expired) is verified away.
        """
        _subscribe("listener")
        core.session_metadata_path("listener").unlink()
        assert fleet_alerts.subscribers() == []

    def test_reindex_rebuilds_a_lost_index_from_the_records(self, isolate):
        _subscribe("listener")
        fleet_alerts.subscribers_index_path().unlink()
        assert fleet_alerts.subscribers() == []  # fails quiet, as designed
        assert fleet_alerts.reindex() == ["listener"]
        assert fleet_alerts.subscribers() == ["listener"]


class TestCliSurface:
    """CLI is the SSOT: an API only a branch can reach is not shipped."""

    def _run(self, argv: list[str]) -> int:
        from agentwire.__main__ import build_parser

        args = build_parser().parse_args(argv)
        return args.func(args)

    @pytest.fixture(autouse=True)
    def a_real_session(self, isolate):
        """`listener` is an existing session here — the CLI refuses to invent one."""
        core.store_session_metadata("listener", {"role": "worker", "created_at": "x"})

    def test_subscribe_list_unsubscribe_round_trip(self, isolate, capsys):
        assert self._run(["alerts", "subscribe", "listener"]) == 0
        capsys.readouterr()
        assert self._run(["alerts", "list", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["subscribers"] == ["listener"]
        assert self._run(["alerts", "unsubscribe", "listener"]) == 0
        capsys.readouterr()
        self._run(["alerts", "list", "--json"])
        assert json.loads(capsys.readouterr().out)["subscribers"] == []

    def test_unsubscribing_something_that_never_subscribed_fails_loudly(
        self, isolate, capsys
    ):
        assert self._run(["alerts", "unsubscribe", "nobody"]) == 1

    def test_subscribe_typo_fails_and_mints_no_record(self, isolate, capsys):
        assert self._run(["alerts", "subscribe", "typoo"]) == 1
        assert "typoo" not in core.recorded_sessions()

    def test_list_never_reports_a_missing_index_as_a_confident_zero(
        self, isolate, capsys
    ):
        """The one surface built to make a silent stop visible must not agree
        with it. `list` reads the same index the emit path does, so a lost index
        would otherwise render as "nobody is subscribed" — while a live lease
        sits right there in the record store, hearing nothing.

        What it can honestly say is narrower than I first wrote: a machine where
        nobody ever subscribed ALSO has no index, and from here those two are
        identical. So the answer is flagged as unconfident either way, and
        `reindex` is what settles it.
        """
        self._run(["alerts", "subscribe", "listener"])
        capsys.readouterr()
        self._run(["alerts", "list", "--json"])
        assert json.loads(capsys.readouterr().out)["index_present"] is True

        fleet_alerts.subscribers_index_path().unlink()
        self._run(["alerts", "list", "--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["subscribers"] == [] and out["index_present"] is False

        # ...and the lease was never gone — reindex proves the zero was false.
        self._run(["alerts", "reindex", "--json"])
        capsys.readouterr()
        self._run(["alerts", "list", "--json"])
        assert json.loads(capsys.readouterr().out)["subscribers"] == ["listener"]

    def test_list_says_so_in_prose_too(self, isolate, capsys):
        self._run(["alerts", "subscribe", "listener"])
        fleet_alerts.subscribers_index_path().unlink()
        capsys.readouterr()
        self._run(["alerts", "list"])
        assert "reindex" in capsys.readouterr().out

    def test_reindex_is_reachable_from_the_cli(self, isolate, capsys):
        self._run(["alerts", "subscribe", "listener"])
        fleet_alerts.subscribers_index_path().unlink()
        capsys.readouterr()
        assert self._run(["alerts", "reindex", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["subscribers"] == ["listener"]


class TestBlockedRootPane:
    @pytest.fixture
    def sweep(self, monkeypatch, tmp_path):
        """The REAL entry point. The alert is throttled by a stamp the caller
        persists, so exercising the inner function alone cannot see the loop
        that shipped — the marker round-trip is where the throttle lives."""
        monkeypatch.setattr(prompt_router, "STATE_DIR", tmp_path / "prompt-router")
        monkeypatch.setattr(prompt_router, "EVENTS_FILE", tmp_path / "pr-events.jsonl")
        monkeypatch.setattr(prompt_router, "resolve_parent", lambda *a, **k: None)
        monkeypatch.setattr(
            "agentwire.channels.email.send_email",
            lambda **k: SimpleNamespace(success=True, error=None),
        )
        return lambda info=None: prompt_router.route_prompt(
            "root-1", 0, info or _prompt_info(), source="sweep"
        )

    def test_no_parent_escalation_reaches_the_listener(self, isolate, sweep):
        _subscribe("listener")
        sweep()
        msgs = _inbox_messages("listener")
        assert len(msgs) == 1
        assert msgs[0].kind == "escalation"
        assert "root-1" in msgs[0].text

    def test_throttled_by_its_own_stamp_on_the_marker(self, isolate, sweep):
        _subscribe("listener")
        sweep()
        sweep()
        assert len(_inbox_messages("listener")) == 1
        marker = prompt_router.read_marker("root-1", 0)
        assert marker["alerted_at"]

    def test_the_stamp_expires_with_its_own_ttl(self, isolate, sweep, monkeypatch):
        _subscribe("listener")
        sweep()
        marker = prompt_router.read_marker("root-1", 0)
        marker["alerted_at"] = (
            datetime.now(timezone.utc)
            - prompt_router.NO_PARENT_ESCALATE_TTL
            - timedelta(minutes=1)
        ).isoformat()
        prompt_router.write_marker("root-1", 0, **{
            k: v for k, v in marker.items() if k not in ("session", "pane")
        })
        sweep()
        assert len(_inbox_messages("listener")) == 2
