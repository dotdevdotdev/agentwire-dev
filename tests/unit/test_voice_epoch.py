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

Two properties of that base are load-bearing and are tested here rather than
assumed, both found in review:

- it is **reserved atomically** (``reserve_epoch``, one lock). Read-then-write
  is two acquisitions, and two concurrent mints on the bridge's threading
  server can be handed the same base — the two-interleaved-counters case, back
  inside the fix for it.
- it is **bounded** (:data:`~agentwire.voice_layer.server.MAX_SEQ`). The number
  now crosses into the page's counter, where it is a double rather than an
  int: past 2**53 an increment stops advancing and every event shares a
  sequence, and past that it is ``Infinity``, whose anchors serialize as
  ``null``. Both wedge the buddy silently for the rest of the bridge run.
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


class TestTheEpochIsReservedAtomically:
    """Review F1. ``high_seq`` read, then ``note_seq`` written, is two lock
    acquisitions with a window between them — so two concurrent ``/mint`` on
    the ``ThreadingHTTPServer`` can be handed the SAME base. That is exactly
    the "second tab, two interleaved counters" case #978 names, reintroduced
    inside the fix for it.

    It did not reproduce under plain threading (the window is a couple of
    bytecodes), and "not observed" is the argument this module refuses
    everywhere else — ``TestSingleUseIsClaimedNotJudged`` is the same shape,
    and the #978 work leaned on that reasoning. So the window is forced open
    rather than raced for.
    """

    def test_two_concurrent_mints_never_share_a_base(self, monkeypatch):
        import threading
        import time as _time

        class SlowReadRing(transcript.TranscriptRing):
            """A ring whose ``high_seq`` READ is slow.

            This is the whole test: a reserve that holds one lock across the
            read and the write never calls this at all, so the window it opens
            cannot exist. A read-then-write reserve calls it and both threads
            come back with the same number.
            """

            @property
            def high_seq(self):
                value = transcript.TranscriptRing.high_seq.fget(self)
                _time.sleep(0.05)
                return value

        from agentwire.voice_layer import realtime

        monkeypatch.setattr(realtime, "mint_session", FakeMint())
        b = server.BuddyBridge("buddy", "tok")
        b.ring = SlowReadRing()

        bases = []
        lock = threading.Lock()

        def one():
            base = b.mint()["seq_base"]
            with lock:
                bases.append(base)

        threads = [threading.Thread(target=one) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(bases)) == 2, f"two tabs were handed the same base: {bases}"

    def test_many_concurrent_reservations_are_all_disjoint(self):
        import threading

        ring = transcript.TranscriptRing()
        got = []
        lock = threading.Lock()

        def one():
            base = ring.reserve_epoch(server.MINT_SEQ_GAP, server.MAX_SEQ)
            with lock:
                got.append(base)

        threads = [threading.Thread(target=one) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(got)) == len(got)
        # Disjoint, not merely distinct: consecutive bases are a whole gap
        # apart, which is what stops one tab counting into the next's range.
        ordered = sorted(got)
        for lower, upper in zip(ordered, ordered[1:]):
            assert upper - lower >= server.MINT_SEQ_GAP


class TestASequenceIsBoundedAtBothEnds:
    """Review F7, a coupling this work newly created.

    ``high_seq`` used to be read-only state the bridge kept for its own
    ordering. It now flows BACK into the client's clock through ``seq_base``,
    so a client-supplied sequence is no longer just a number the ring
    compares — it becomes the origin of every later page's counter, on the
    other side of a JSON boundary where a Python int is NOT an int.

    One malformed local POST of ``10**18`` permanently raises it. Every later
    mint then hands out a base above 2**53, where ``++seqCounter`` is a no-op
    in IEEE-754: every event shares one sequence, ``after(anchor)`` is
    strictly-after, and the buddy answers ``pending_transcript`` forever.
    Bigger still parses to ``Infinity`` in the page, which serializes anchors
    as ``null`` — ``not_announced`` forever. Both silent, and both survive a
    reload, because ``high_seq`` is bridge-lifetime.

    Loopback- and token-gated, so this is robustness rather than a remote
    attack — but #986 hardened this same bridge against this same class.
    """

    def test_an_absurd_speech_sequence_is_refused(self, bridge):
        before = bridge.ring.high_seq
        result = bridge.utterance({"item_id": "u1", "speech_started_seq": 10**18})
        assert result["success"] is False
        assert bridge.ring.high_seq == before, "a refused seq must not raise the clock"

    def test_an_absurd_commit_sequence_is_refused(self, bridge):
        before = bridge.ring.high_seq
        assert bridge.utterance({"item_id": "u1", "commit_seq": 10**18})[
            "success"
        ] is False
        assert bridge.ring.high_seq == before

    def test_an_absurd_transcript_sequence_is_refused(self, bridge):
        before = bridge.ring.high_seq
        assert bridge.utterance(
            {"item_id": "u1", "transcript": "hi", "speech_started_seq": 10**18}
        )["success"] is False
        assert bridge.ring.high_seq == before

    def test_an_absurd_anchor_sequence_is_refused(self, bridge):
        before = bridge.ring.high_seq
        assert bridge.anchor({"proposal_id": "abc", "seq": 10**18})["success"] is False
        assert bridge.ring.high_seq == before

    def test_an_ordinary_sequence_is_untouched(self, bridge):
        """The false-reject half, and it is the expensive one: a refused
        utterance never enters the ring, so the owner's approval is invisible
        and the buddy waits forever. The ceiling has to sit far above anything
        a real session reaches."""
        base = bridge.mint()["seq_base"]
        assert bridge.utterance({"item_id": "u1", "speech_started_seq": base + 1})[
            "success"
        ] is True
        assert bridge.ring.high_seq >= base + 1
        assert server.MAX_SEQ > server.MINT_SEQ_GAP * 1000

    def test_the_ceiling_stays_inside_what_the_page_can_count(self):
        """The bound exists because the number crosses into JS. Everything the
        bridge can ever hand out must stay a SAFE integer there, or the page's
        ``++seqCounter`` silently stops advancing."""
        assert server.MAX_SEQ < 2**53

    def test_an_exhausted_sequence_space_refuses_rather_than_poisons(self, bridge):
        """The other end. Handing out a base past the ceiling is the exact
        wedge this guards, so exhaustion must be an error the page throws on,
        never a number it counts from."""
        bridge.ring.note_seq(server.MAX_SEQ - 1)
        result = bridge.mint()
        assert result["success"] is False
        assert "seq_base" not in result

    def test_exhaustion_is_checked_before_the_api_key_is_spent(self, bridge, monkeypatch):
        from agentwire.voice_layer import realtime

        minter = FakeMint()
        monkeypatch.setattr(realtime, "mint_session", minter)
        bridge.ring.note_seq(server.MAX_SEQ - 1)
        bridge.mint()
        assert minter.calls == 0


class TestThePageRefusesAnUnusableOrigin:
    def test_the_guard_requires_a_safe_integer_not_merely_a_number(self):
        """``typeof x === "number"`` passes for ``Infinity``, and a page that
        counts from Infinity serializes every anchor as ``null``."""
        page = client.page("buddy", "tok")
        assert "Number.isSafeInteger(session.seq_base)" in page
        assert 'typeof session.seq_base !== "number"' not in page


class TestAReservationIsAWholeBlock:
    """Review N2. ``reserve_epoch`` returns a BASE the page counts UP from, so
    what has to fit under the ceiling is ``base + gap`` — testing the base
    alone let the final reservation land exactly on it. That page mints
    successfully and then has ZERO usable sequences: every forward is refused
    for exceeding :data:`~agentwire.voice_layer.server.MAX_SEQ`, silently, for
    the rest of the run.

    Unreachable in practice — roughly 35 million mints on one bridge process —
    and that is not the argument. ``reserve_epoch``'s own first sentence claims
    a block of ``gap`` sequences, so the code has to reserve one.
    """

    def test_a_base_landing_exactly_on_the_ceiling_is_refused(self):
        ring = transcript.TranscriptRing()
        ring.note_seq(server.MAX_SEQ - server.MINT_SEQ_GAP)
        assert ring.reserve_epoch(server.MINT_SEQ_GAP, server.MAX_SEQ) == 0

    def test_the_last_usable_reservation_keeps_its_whole_block(self):
        """The false-reject half: the guard must not refuse a reservation that
        genuinely fits, or it retires the sequence space a whole epoch early."""
        ring = transcript.TranscriptRing()
        ring.note_seq(server.MAX_SEQ - 2 * server.MINT_SEQ_GAP)
        base = ring.reserve_epoch(server.MINT_SEQ_GAP, server.MAX_SEQ)
        assert base == server.MAX_SEQ - server.MINT_SEQ_GAP
        # ...and every sequence that page can count to is still acceptable.
        assert base + server.MINT_SEQ_GAP <= server.MAX_SEQ

    def test_the_page_it_hands_that_base_to_can_actually_use_it(self, bridge):
        bridge.ring.note_seq(server.MAX_SEQ - 2 * server.MINT_SEQ_GAP)
        base = bridge.mint()["seq_base"]
        assert bridge.utterance(
            {"item_id": "u1", "speech_started_seq": base + server.MINT_SEQ_GAP}
        )["success"] is True
