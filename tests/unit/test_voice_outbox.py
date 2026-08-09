"""Tests for the buddy's self-observation surface (#958) and the narrow
epistemic boundary that pairs with it (#956).

The defect these guard: the buddy had nine read tools and not one showed what
it SENT. Asked "did the code word end up in the message you sent?" it had no
instrument that could answer, scraped the recipient's terminal instead, and
confabulated. Two halves land together:

1. **The outbox** (:mod:`agentwire.voice_layer.outbox`): every executed write
   is recorded — proposal id, recipient, THE EXACT RENDERED BODY THAT RAN, the
   argv, timestamp, dispatch outcome — and per-message delivery state is
   computed from the recipient's real inbox, never stored and never guessed.
2. **The boundary in the instructions**: decline ONLY where no instrument
   exists; everywhere an instrument exists the instruction is LOOK. Both
   directions are asserted, because a blanket refusal would make the buddy
   *worse* — in a screenless channel "I don't know" is barely an upgrade on a
   confident wrong answer.

What matters most here is recorded-not-reconstructed: the body written to the
outbox must be ``argv[-1]`` of what actually executed, not a re-render. Worker
A is changing what ``render_body`` produces (#953); recording the executed
string keeps this tool correct through that change unseen.
"""

import itertools
from types import SimpleNamespace

import pytest

from agentwire import core, inbox
from agentwire.voice_layer import confirm, instructions, outbox, tools, transcript


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})
    return tmp_path


def _argv(session="orchestrator", buddy="buddy", body="<voice> hello ┃ #abc123"):
    return [
        "msg", "send", "--to", session, "--from", buddy, "--kind", "request", body,
    ]


def _proposal(id="abc123", session="orchestrator", instruction="hello"):
    return SimpleNamespace(id=id, session=session, instruction=instruction)


# =============================================================================
# Recording
# =============================================================================


class TestRecordWrite:
    def test_records_the_executed_body_verbatim(self, isolate):
        """The body in the record is argv[-1] — what RAN — not a re-render."""
        argv = _argv(body="<voice> whatever confirm.py actually built ┃ #abc123")
        outbox.record_write(_proposal(), argv, {"success": True})

        entries = outbox.read_outbox("buddy")
        assert len(entries) == 1
        entry = entries[0]
        assert entry["body"] == argv[-1]
        assert entry["argv"] == argv
        assert entry["proposal_id"] == "abc123"
        assert entry["session"] == "orchestrator"
        assert entry["buddy"] == "buddy"
        assert entry["dispatched"] is True
        assert entry["ts"] > 0

    def test_recipient_and_buddy_come_from_the_argv_not_the_proposal(self, isolate):
        """The argv is what executed; the proposal is what was intended. If the
        two ever disagree, the record must side with reality."""
        argv = _argv(session="real-target", buddy="real-buddy")
        outbox.record_write(_proposal(session="intended-target"), argv, {"success": True})
        (entry,) = outbox.read_outbox("real-buddy")
        assert entry["session"] == "real-target"

    def test_failed_dispatch_is_recorded_with_its_error(self, isolate):
        outbox.record_write(
            _proposal(), _argv(), {"success": False, "error": "boom"}
        )
        (entry,) = outbox.read_outbox("buddy")
        assert entry["dispatched"] is False
        assert "boom" in entry["error"]

    def test_recording_failure_never_raises(self, isolate, monkeypatch):
        """The record happens AFTER the write executed. An exception here would
        propagate to the dispatcher's catch-all, which tells the owner 'nothing
        happened' about a message that is already in the recipient's inbox —
        the exact over/under-claim this whole layer exists to prevent."""
        monkeypatch.setattr(
            outbox, "outbox_path", lambda buddy: (_ for _ in ()).throw(OSError("disk"))
        )
        outbox.record_write(_proposal(), _argv(), {"success": True})  # must not raise

    def test_newest_first_and_limit(self, isolate):
        for n in range(5):
            outbox.record_write(
                _proposal(id=f"id{n:04d}"), _argv(body=f"body {n} #id{n:04d}"),
                {"success": True},
            )
        entries = outbox.read_outbox("buddy", limit=2)
        assert [e["proposal_id"] for e in entries] == ["id0004", "id0003"]

    def test_corrupt_lines_are_skipped_not_fatal(self, isolate):
        outbox.record_write(_proposal(), _argv(), {"success": True})
        with open(outbox.outbox_path("buddy"), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        outbox.record_write(_proposal(id="def456"), _argv(), {"success": True})
        assert len(outbox.read_outbox("buddy")) == 2


# =============================================================================
# Delivery state — computed from the recipient's REAL inbox at read time
# =============================================================================


class TestDeliveryState:
    def _entry(self, **over):
        base = {
            "proposal_id": "abc123",
            "session": "orchestrator",
            "body": "<voice> hello ┃ #abc123",
            "kind": "request",
            "dispatched": True,
        }
        base.update(over)
        return base

    def test_pending_message_reads_as_queued(self, isolate):
        inbox.enqueue("orchestrator", "<voice> hello ┃ #abc123", kind="request", sender="buddy")
        assert outbox.delivery_state(self._entry())["state"] == "queued"

    def test_dead_lettered_message_reads_as_dead_lettered_with_reason(self, isolate):
        msgs = inbox.enqueue(
            "orchestrator", "<voice> hello ┃ #abc123", kind="request", sender="buddy"
        )
        msg = msgs[0]
        msg.dead_ts = 1
        msg.reason = "target_gone"
        dead = inbox.dead_dir("orchestrator")
        dead.mkdir(parents=True, exist_ok=True)
        inbox._write_message(dead / f"{msg.id}.json", msg)
        msg.path.unlink()
        state = outbox.delivery_state(self._entry())
        assert state["state"] == "dead_lettered"
        assert "target_gone" in state.get("detail", "")

    def test_neither_pending_nor_dead_reads_as_delivered(self, isolate):
        assert outbox.delivery_state(self._entry())["state"] == "delivered"

    def test_failed_dispatch_short_circuits(self, isolate):
        state = outbox.delivery_state(self._entry(dispatched=False))
        assert state["state"] == "dispatch_failed"

    def test_matches_by_proposal_id_when_the_body_shape_changes(self, isolate):
        """Worker A is rewriting render_body (#953). If the recorded body and
        the enqueued text ever diverge in shape, the ``#<id>`` tag still keys
        the match — the state must not silently flip to 'delivered'."""
        inbox.enqueue(
            "orchestrator", "some future body shape #abc123", kind="request", sender="buddy"
        )
        assert outbox.delivery_state(self._entry())["state"] == "queued"


# =============================================================================
# The spine records its writes — integration through the one-line call site
# =============================================================================


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Runner:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"success": True}

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.result


class _Convo:
    """Minimal client-ordered driver, same shape as test_voice_confirm's."""

    _ids = itertools.count()

    def __init__(self, spine, ring):
        self.spine, self.ring, self.seq = spine, ring, 0

    def _next(self):
        self.seq += 1
        return self.seq

    def announced_proposal(self):
        proposal = self.spine.propose(
            tool="send_session_message",
            session="orchestrator",
            instruction="hello there",
            argv_prefix=[
                "msg", "send", "--to", "orchestrator", "--from", "buddy",
                "--kind", "request",
            ],
            params={},
        )
        self.spine.announce(proposal.id, self._next())
        return proposal

    def approve(self, proposal):
        item_id = f"item_{next(self._ids)}"
        self.ring.speech_started(item_id, self._next())
        self.ring.commit(item_id, self._next())
        self.ring.transcribe(item_id, f"confirm {confirm.spoken_nonce(proposal.nonce)}")


class TestSpineRecords:
    def _spine(self, runner):
        ring = transcript.TranscriptRing()
        spine = confirm.ConfirmSpine(ring, wait_s=0.0, runner=runner, clock=_Clock())
        return spine, _Convo(spine, ring)

    def test_an_approved_write_lands_in_the_outbox(self, isolate):
        runner = _Runner()
        spine, convo = self._spine(runner)
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        verdict = spine.confirm(proposal.token)
        assert verdict.approved

        (entry,) = outbox.read_outbox("buddy")
        assert entry["body"] == runner.calls[0][-1]
        assert entry["proposal_id"] == proposal.id
        assert entry["dispatched"] is True

    def test_a_failed_dispatch_is_recorded_as_failed(self, isolate):
        runner = _Runner({"success": False, "error": "cli exploded"})
        spine, convo = self._spine(runner)
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        assert not spine.confirm(proposal.token).approved
        (entry,) = outbox.read_outbox("buddy")
        assert entry["dispatched"] is False

    def test_a_refused_confirm_records_nothing(self, isolate):
        """The false-record direction: an outbox entry for a write that never
        executed would let the buddy 'quote' a message that does not exist."""
        runner = _Runner()
        spine, convo = self._spine(runner)
        proposal = convo.announced_proposal()
        # No approval spoken.
        assert not spine.confirm(proposal.token).approved
        assert runner.calls == []
        assert outbox.read_outbox("buddy") == []


# =============================================================================
# The buddy_sent tool
# =============================================================================


class TestBuddySentTool:
    def test_answers_what_did_i_send_in_one_call(self, isolate):
        """The triggering question. The rendered body comes back verbatim,
        with delivery state, keyed by proposal id — no terminal scraping."""
        argv = _argv(body="<voice> ship it ┃ said: \"confirm tango\" ┃ #abc123")
        outbox.record_write(_proposal(), argv, {"success": True})
        inbox.enqueue("orchestrator", argv[-1], kind="request", sender="buddy")

        result = tools.dispatch("buddy_sent", {}, buddy="buddy")
        assert result["success"] is True
        (sent,) = result["sent"]
        assert sent["body"] == argv[-1]
        assert sent["proposal_id"] == "abc123"
        assert sent["delivery"]["state"] == "queued"

    def test_empty_outbox_is_a_real_answer_not_an_error(self, isolate):
        result = tools.dispatch("buddy_sent", {}, buddy="buddy")
        assert result["success"] is True
        assert result["sent"] == []

    def test_filters_by_proposal_id(self, isolate):
        outbox.record_write(_proposal(id="aaa111"), _argv(body="one #aaa111"), {"success": True})
        outbox.record_write(_proposal(id="bbb222"), _argv(body="two #bbb222"), {"success": True})
        result = tools.dispatch("buddy_sent", {"proposal_id": "aaa111"}, buddy="buddy")
        assert [s["proposal_id"] for s in result["sent"]] == ["aaa111"]

    def test_limit_is_clamped_not_trusted(self, isolate):
        for n in range(3):
            outbox.record_write(_proposal(id=f"id{n:04d}"), _argv(), {"success": True})
        result = tools.dispatch("buddy_sent", {"limit": 99999}, buddy="buddy")
        assert result["success"] is True
        result = tools.dispatch("buddy_sent", {"limit": 1}, buddy="buddy")
        assert len(result["sent"]) == 1

    def test_is_in_the_realtime_tool_defs(self):
        assert "buddy_sent" in {t["name"] for t in tools.realtime_tool_defs()}


# =============================================================================
# Instructions: grounding + the NARROW boundary (#956)
# =============================================================================


class TestGrounding:
    def test_states_what_a_message_is_once_it_leaves(self):
        """The ecosystem model: file inbox, empty-box injection (WHY 'queued
        not sent' is true), the leading voice marker, defer and dead-letter."""
        flowed = " ".join(instructions.build_instructions().split())
        assert "file inbox" in flowed
        assert "input box is empty" in flowed
        assert "<voice>" in flowed
        assert "dead-letter" in flowed

    def test_points_at_buddy_sent_for_questions_about_its_own_writes(self):
        flowed = " ".join(instructions.build_instructions().split())
        assert "buddy_sent" in flowed
        assert "quote" in flowed


class TestNarrowBoundary:
    """#956, and it must be NARROW. Decline bites ONLY where no instrument
    exists; everywhere else the instruction is LOOK. If both directions came
    out as refusals the buddy would be worse than before the change."""

    def test_the_decline_is_scoped_to_unobservable_internals(self):
        flowed = " ".join(instructions.build_instructions().split())
        assert "BOUNDARY." in instructions.build_instructions()
        assert "implemented" in flowed
        assert "invent" in flowed or "never describe" in flowed

    def test_the_observable_direction_says_look_not_decline(self):
        """The false-reject half: an observable question must be routed to an
        instrument, in the same breath as the decline rule."""
        text = instructions.build_instructions()
        boundary = text[text.index("BOUNDARY.") :]
        first_para = boundary.split("\n\n")[0]
        # "look it up" specifically — a bare "look" also matches "looked
        # wrong" in the anomaly clause, which let a gutted observable
        # direction pass (caught by mutation testing).
        assert "look it up" in first_para
        assert "buddy_sent" in first_para
        assert "decline" not in first_para.lower()

    def test_never_reassures_past_an_owner_reported_anomaly(self):
        flowed = " ".join(instructions.build_instructions().split()).lower()
        assert "anomal" in flowed or "looked wrong" in flowed
        assert "explain it away" in flowed

    def test_no_blanket_refusal_language(self):
        """The boundary must not tell the buddy to decline questions about its
        own actions — those are exactly the ones it now CAN answer."""
        text = instructions.build_instructions()
        boundary = text[text.index("BOUNDARY.") :].split("\n\n")[0].lower()
        assert "what you sent" not in boundary or "look" in boundary
        # The decline verbs must be bound to internals/implementation, never
        # bare: every sentence containing a decline verb also names internals.
        for sentence in boundary.replace("\n", " ").split(". "):
            if "do not describe" in sentence or "don't know" in sentence:
                assert (
                    "implement" in sentence
                    or "internals" in sentence
                    or "observe" in sentence
                ), sentence
