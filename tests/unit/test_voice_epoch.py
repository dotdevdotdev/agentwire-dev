"""The logical clock's ORIGIN is server-owned (#978 blocking 1).

The confirm gate orders on a logical clock the CLIENT assigns (``nextSeq``),
because the client is the only place that sees data-channel event order. What
the client cannot supply is the clock's **origin**: ``seqCounter`` is a page
variable and starts at 0 on every page load, while the ring and the spine live
for the whole bridge run.

So a reload put the two out of step in the one direction that matters. The
pre-reload utterances sit in the ring at high sequences, complete and unspent;
the fresh page anchors its next proposal at 1. ``ring.after(anchor)`` then
returns LAST session's utterances, and two things follow, both fail-closed and
both persistently wrong in a channel with no screen:

1. they reach ``_judge`` as non-matching, so the proposal burns attempts toward
   ``too_many_attempts`` for a question the owner was never asked;
2. worse, the post-approval denial scan sees an old "no, hang on" as strictly
   AFTER the new match and retroactively denies every legitimate approval,
   until 32 fresh utterances evict it from the ring.

The fix is the second of the two directions #978 named — **move the clock's
origin server-side**. ``/mint`` is the one event that happens exactly once per
page load, so the bridge answers it with a sequence base above every sequence
it has ever seen, and the page starts counting from there. Nothing is rejected
and nothing is deleted: the false-reject half of a rejecting epoch guard is a
dropped utterance, which in this channel is a silent loop, and it would be paid
on the owner's LIVE tab.

The base is advanced by a whole :data:`~agentwire.voice_layer.server.MINT_SEQ_GAP`
rather than by one, which is what makes it an epoch rather than a bump: two
tabs minting against one bridge get non-overlapping numeric ranges, so tab A
would have to emit a million events before it could reach into tab B's.
"""

from __future__ import annotations

import pytest

from agentwire.voice_layer import client, confirm, server, transcript


class FakeMint:
    """``realtime.mint_session`` without the network."""

    def __init__(self):
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        return {"client_secret": "sk-fake", "model": "m", "voice": "v"}


@pytest.fixture
def bridge(monkeypatch):
    from agentwire.voice_layer import realtime

    monkeypatch.setattr(realtime, "mint_session", FakeMint())
    return server.BuddyBridge("buddy", "tok", runner=lambda argv: {"success": True})


class TestTheMintHandsOutTheClockOrigin:
    def test_a_fresh_bridge_still_starts_the_page_above_zero(self, bridge):
        """Even the first page gets a base: the client's ``|| 0`` fallback is
        the shape that silently reintroduced a zero origin."""
        assert bridge.mint()["seq_base"] >= server.MINT_SEQ_GAP

    def test_each_mint_opens_a_range_above_everything_seen(self, bridge):
        first = bridge.mint()["seq_base"]
        # The page runs for a while.
        bridge.utterance({"item_id": "u1", "speech_started_seq": first + 1})
        bridge.utterance({"item_id": "u1", "commit_seq": first + 2})
        second = bridge.mint()["seq_base"]
        assert second > first + 2
        # ...and by a whole gap, so a still-live first page cannot count into
        # the second page's range.
        assert second - (first + 2) >= server.MINT_SEQ_GAP

    def test_the_base_is_recorded_on_the_ring_not_just_returned(self, bridge):
        """A base the ring has not seen is a base the NEXT mint would reuse."""
        base = bridge.mint()["seq_base"]
        assert bridge.ring.high_seq >= base


class TestAReloadCannotBeApprovedByLastSessionsSpeech:
    """The two harms #978 named, driven through the real ring and spine."""

    def _spine(self, ring, runner):
        return confirm.ConfirmSpine(ring, wait_s=0.05, runner=runner)

    def test_an_old_utterance_no_longer_burns_a_new_proposals_attempts(self):
        ring = transcript.TranscriptRing()
        calls = []
        spine = self._spine(ring, lambda argv: calls.append(argv) or {"success": True})

        # --- page 1: the owner says several unrelated things -----------------
        for n, text in enumerate(["okay", "sure, go on", "that's fine"], start=1):
            ring.speech_started(f"old{n}", n)
            ring.commit(f"old{n}", n + 100)
            ring.transcribe(f"old{n}", text)

        # --- reload: the page takes its origin from the bridge ---------------
        base = ring.note_seq(ring.high_seq + server.MINT_SEQ_GAP)

        proposal = spine.propose(
            tool="fleet_session_send",
            session="orch",
            instruction="ping",
            argv_prefix=("agentwire", "msg", "send"),
        )
        spine.announce(proposal.id, base + 1)

        verdict = spine.confirm(proposal.token)
        # Nothing the owner said LAST session is visible to this proposal, so
        # the outcome is the honest "I have not heard you yet" — never a
        # refusal counted against them.
        assert verdict.reason == "pending_transcript"
        live = {p.token: p for p in spine.pending()}
        assert live[proposal.token].attempts == 0
        assert calls == []

    def test_an_old_denial_cannot_retroactively_deny_a_new_approval(self):
        ring = transcript.TranscriptRing()
        calls = []
        spine = self._spine(ring, lambda argv: calls.append(argv) or {"success": True})

        # --- page 1: the owner took something back --------------------------
        ring.speech_started("old_denial", 1)
        ring.commit("old_denial", 2)
        ring.transcribe("old_denial", "no, hang on")
        assert confirm.carries_denial("no, hang on"), "fixture must really deny"

        # --- reload ----------------------------------------------------------
        base = ring.note_seq(ring.high_seq + server.MINT_SEQ_GAP)

        proposal = spine.propose(
            tool="fleet_session_send",
            session="orch",
            instruction="ping",
            argv_prefix=("agentwire", "msg", "send"),
        )
        spine.announce(proposal.id, base + 1)
        ring.speech_started("new_ok", base + 2)
        ring.commit("new_ok", base + 3)
        ring.transcribe("new_ok", f"confirm {confirm.spoken_nonce(proposal.nonce)}")

        verdict = spine.confirm(proposal.token)
        assert verdict.approved is True, verdict.reason
        assert len(calls) == 1

    def test_the_control_that_must_fail_without_the_origin(self):
        """Kill this file's own false negative.

        The two tests above prove nothing unless the SAME shape at a zero
        origin actually breaks. This is that shape: identical, minus the
        server-owned base.
        """
        ring = transcript.TranscriptRing()
        calls = []
        spine = self._spine(ring, lambda argv: calls.append(argv) or {"success": True})

        ring.speech_started("old_denial", 50)
        ring.commit("old_denial", 51)
        ring.transcribe("old_denial", "no, hang on")

        proposal = spine.propose(
            tool="fleet_session_send",
            session="orch",
            instruction="ping",
            argv_prefix=("agentwire", "msg", "send"),
        )
        spine.announce(proposal.id, 1)          # a page that restarted at zero
        ring.speech_started("new_ok", 2)
        ring.commit("new_ok", 3)
        ring.transcribe("new_ok", f"confirm {confirm.spoken_nonce(proposal.nonce)}")

        verdict = spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "denied"
        assert calls == []


class TestThePageTakesItsOriginFromTheMint:
    """The wiring half. The node harnesses cover the factories; the origin
    lives in the page's own ``start()``, so it is asserted on the source."""

    def test_the_counter_is_seeded_from_the_mint_response(self):
        page = client.page("buddy", "tok")
        start = page.split("async function start()", 1)[1].split("function stop()", 1)[0]
        assert "seqCounter = session.seq_base" in start

    def test_the_seed_is_not_defaulted_to_zero(self):
        """``session.seq_base || 0`` reads defensive and silently restores the
        exact defect — a bridge that failed to answer would hand every page a
        zero origin again. A missing base is a broken bridge, and start()
        already throws on one."""
        page = client.page("buddy", "tok")
        assert "seq_base || 0" not in page
        assert "seq_base ||" not in page

    def test_the_seed_lands_before_anything_can_consume_a_sequence(self):
        """The declaration's 0 is only unreachable because the seed happens
        before the data channel exists — nothing emits an event, and so
        nothing calls ``nextSeq()``, until then. Reordering the seed below
        ``createDataChannel`` would put a zero-origin sequence back on the
        wire without changing a single line that mentions the origin."""
        page = client.page("buddy", "tok")
        start = page.split("async function start()", 1)[1].split("function stop()", 1)[0]
        assert start.index("seqCounter = session.seq_base") < start.index(
            "pc.createDataChannel"
        )
