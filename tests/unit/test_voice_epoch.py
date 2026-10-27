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

from agentwire.voice_layer import server, transcript


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
