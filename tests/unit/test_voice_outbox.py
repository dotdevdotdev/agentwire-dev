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
from agentwire.voice_layer import (
    confirm,
    outbox,
    transcript,
    write_tools,
)


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})
    return tmp_path


def _argv(session="orchestrator", buddy="buddy", body="hello ┃ #abc123"):
    return [
        "msg", "send", "--to", session, "--from", buddy, "--kind", "request", body,
    ]


def _proposal(id="abc123", session="orchestrator", instruction="hello",
              append_body=True, buddy="buddy"):
    return SimpleNamespace(
        id=id, session=session, instruction=instruction, append_body=append_body,
        params={"_buddy": buddy},
    )


# =============================================================================
# Recording
# =============================================================================


class TestRecordWrite:
    def test_records_the_executed_body_verbatim(self, isolate):
        """The body in the record is argv[-1] — what RAN — not a re-render."""
        argv = _argv(body="whatever confirm.py actually built ┃ #abc123")
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


# =============================================================================
# Delivery state — computed from the recipient's REAL inbox at read time
# =============================================================================


class TestDeliveryState:
    def _entry(self, **over):
        base = {
            "proposal_id": "abc123",
            "session": "orchestrator",
            "body": "hello ┃ #abc123",
            "kind": "request",
            "dispatched": True,
        }
        base.update(over)
        return base

    def test_pending_message_reads_as_queued(self, isolate):
        inbox.enqueue("orchestrator", "hello ┃ #abc123", kind=write_tools.WRITE_KIND, sender="buddy")
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


# =============================================================================
# Instructions: grounding + the NARROW boundary (#956)
# =============================================================================
