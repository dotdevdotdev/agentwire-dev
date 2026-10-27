"""Tests for the voice layer's confirm spine (Slice 1, branch-only).

**Proved by mutation, not by the passing case.** A gate exercised only with a
valid approval proves nothing — the load-bearing assertion is always that
something FAILS TO WRITE. Every refusal below asserts on a runner that was never
called, not merely on a falsy return: "returned success=False" and "did not run
the command" are different claims, and only the second is the guarantee.

The mandated mutations (spec v2 §8) are in :class:`TestGateRefusals`, one test
each: no prior proposal, expired TTL, replay *after success*, wrong/absent
nonce, an approval whose item-commit time predates the proposal's anchor, a
Whisper-class silence hallucination, an approval followed by a denial, and one
approval offered to two outstanding proposals.

Two things this file deliberately does NOT try to prove, because asserting them
here would be fixture-shaped:

- **That a refusal is actually spoken.** A Python return value says nothing
  about whether audio happened, and is green in exactly the scenario the
  requirement exists to prevent. That assertion lives in
  ``test_voice_announcer.py``, against the data channel.
- **That the rendered body survives delivery.** That is measured against the
  real paste path in ``test_voice_body_delivery.py``.

Deliberately in-process. Shelling ``agentwire msg send`` with prose about
guarded operations through the Bash tool trips the damage-control hook (#915),
and the interesting cases here contain exactly such prose. Nothing here disables
a guard; the runner is injected rather than stubbed at the subprocess layer.
"""

import itertools

import pytest

from agentwire.voice_layer import confirm, transcript, write_tools


class FakeClock:
    """A hand-advanced clock for the TTL tests.

    Real time would make them either slow or flaky, and the TTL is the property
    under test — an assertion that depends on wall-clock luck is not one.
    """

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class RecordingRunner:
    """Captures argv instead of running it. ``calls == []`` means nothing wrote."""

    def __init__(self, result=None):
        self.calls: list[list[str]] = []
        self._result = result if result is not None else {"success": True}

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self._result


class Conversation:
    """Drives a ring + spine the way the client would, in event order.

    The sequence counter is the client's ``nextSeq()``: one logical clock over
    the data channel. Tests that hand-pick sequence numbers would be asserting
    against a model of the ordering rather than the ordering itself, which is
    how the predicate got to invert in the first place.
    """

    _ids = itertools.count()

    def __init__(self, ring, spine):
        self.ring = ring
        self.spine = spine
        self.seq = 0

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def says(self, text, *, transcribe=True, estimated=False):
        """The owner speaks: speech starts, audio commits, transcript follows.

        Three events in the order the client emits them. ``estimated`` omits
        the speech_started event, which is the degraded case the gate refuses.
        """
        item_id = f"item_{next(self._ids)}"
        if not estimated:
            self.ring.speech_started(item_id, self._next())
        self.ring.commit(item_id, self._next())
        if transcribe:
            self.ring.transcribe(item_id, text)
        return item_id

    def starts_speaking(self, text=""):
        """Begin an utterance WITHOUT finishing it — the barge-in shape."""
        item_id = f"item_{next(self._ids)}"
        self.ring.speech_started(item_id, self._next())
        return item_id

    def finishes_speaking(self, item_id, text):
        self.ring.commit(item_id, self._next())
        self.ring.transcribe(item_id, text)

    def transcribe_late(self, item_id, text):
        self.ring.transcribe(item_id, text)

    def propose(self, *, session="orchestrator", instruction="restart the portal"):
        return self.spine.propose(
            tool="send_session_message",
            session=session,
            instruction=instruction,
            argv_prefix=[
                "msg", "send", "--to", session, "--from", "buddy",
                "--kind", write_tools.WRITE_KIND,
            ],
            params={"session": session, "message": instruction},
        )

    def buddy_speaks(self, proposal):
        """The response.done of the turn in which the buddy stated the proposal."""
        self.spine.announce(proposal.id, self._next())
        return proposal

    def announced_proposal(self, **kwargs):
        return self.buddy_speaks(self.propose(**kwargs))

    def approve(self, proposal, **kwargs):
        return self.says(f"confirm {confirm.spoken_nonce(proposal.nonce)}", **kwargs)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def ring(clock):
    """ONE clock across the ring and the spine.

    The ring grew a wall-clock of its own with #989's staleness bounds, and a
    ring on ``time.monotonic`` under a spine on :class:`FakeClock` is a fixture
    that CANNOT BUILD the shape those bounds are about: advancing the clock
    expires the proposal while the never-completing entry sits at age zero
    forever. That is a green test proving the fixture's shape rather than the
    code's.
    """
    return transcript.TranscriptRing(clock=clock)


@pytest.fixture
def runner():
    return RecordingRunner()


@pytest.fixture
def spine(ring, runner, clock):
    # wait_s=0 keeps refusal tests instant; the bounded await has its own tests
    # below, with real threads.
    return confirm.ConfirmSpine(ring, wait_s=0.0, runner=runner, clock=clock)


@pytest.fixture
def convo(ring, spine):
    return Conversation(ring, spine)


# =============================================================================
# The nonce grammar, in isolation
# =============================================================================


# =============================================================================
# The mandated mutations
# =============================================================================


class TestGateRefusals:

    def test_confirm_after_ttl_does_not_write(self, convo, clock, runner):
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        clock.advance(confirm.PROPOSAL_TTL_S + 1)
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "expired"
        assert runner.calls == []

    def test_a_token_replayed_after_success_does_not_write_twice(self, convo, runner):
        """Replay means post-success replay. A refused attempt keeps its token —
        that is a different property, asserted in TestTokenIsNotBurnedOnAMiss."""
        proposal = convo.announced_proposal()
        convo.approve(proposal)

        first = convo.spine.confirm(proposal.token)
        assert first.approved is True
        assert len(runner.calls) == 1

        second = convo.spine.confirm(proposal.token)
        assert second.approved is False
        assert second.reason == "replayed"
        assert len(runner.calls) == 1

    def test_a_wrong_nonce_does_not_write(self, convo, runner):
        proposal = convo.announced_proposal()
        wrong = next(w for w in confirm.NONCE_WORDS if w != proposal.nonce)
        convo.says(f"confirm {wrong}")
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        # Its own outcome, not "refused": the owner should ask what the word
        # was, not repeat a word that will never match.
        assert verdict.reason == "wrong_nonce"
        assert runner.calls == []


    def test_a_barge_in_approval_does_not_write(self, convo, runner):
        """The case ordering-on-COMMIT gets wrong, and the reason the ring
        stamps speech_started instead.

        The owner starts speaking DURING the proposal and finishes after it.
        Speech-start predates the proposal's response.done; the commit
        postdates it. Ordering on the commit approves an approval for a
        proposal the owner never heard stated — the exact hole the clock change
        exists to close.
        """
        proposal = convo.propose()
        item = convo.starts_speaking()          # owner cuts in mid-proposal
        convo.buddy_speaks(proposal)            # buddy's turn completes
        convo.finishes_speaking(               # ...and only then do they finish
            item, f"confirm {confirm.spoken_nonce(proposal.nonce)}"
        )

        entry = next(e for e in convo.ring.snapshot() if e.item_id == item)
        assert entry.speech_started_seq < proposal.anchor_seq < entry.commit_seq, (
            "fixture must actually straddle the proposal, or it proves nothing"
        )

        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert runner.calls == []

    def test_a_silence_hallucination_does_not_write(self, convo, runner):
        proposal = convo.announced_proposal()
        convo.says("Okay.")
        convo.says("Thank you.")
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "refused"
        assert runner.calls == []


    def test_a_denial_whose_transcript_has_not_landed_still_blocks(
        self, convo, runner
    ):
        """The bounded-await asymmetry, applied to the denial side.

        The owner approves, then speaks again — and that second utterance is
        still in transcription when confirm runs. ``ring.after`` filters on
        ``complete``, so it was invisible and the write went out. But the
        sequence has ALREADY advanced past it: the system knows they spoke
        again, it just cannot yet say what they said. "Cannot yet say" is
        pending_transcript, never approval.
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        pending = convo.starts_speaking()          # spoke; no transcript yet

        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "pending_transcript"
        assert runner.calls == []

        # And once it lands and turns out to be a denial, it denies.
        convo.finishes_speaking(pending, "no, don't")
        assert convo.spine.confirm(proposal.token).reason == "denied"
        assert runner.calls == []


    def test_an_approval_followed_by_a_denial_does_not_write(self, convo, runner):
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        convo.says("no wait, don't")
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "denied"
        assert runner.calls == []

    def test_one_approval_cannot_satisfy_two_outstanding_proposals(
        self, convo, runner
    ):
        """The "acting twice" failure §4 names, which an existential predicate
        over "some utterance after the proposal" does not prevent."""
        first = convo.announced_proposal(instruction="restart the portal")
        second = convo.announced_proposal(instruction="delete the branch")
        assert first.nonce != second.nonce

        convo.approve(first)

        assert convo.spine.confirm(first.token).approved is True
        verdict = convo.spine.confirm(second.token)
        assert verdict.approved is False
        assert len(runner.calls) == 1
        assert "delete the branch" not in " ".join(runner.calls[0])


    def test_the_happy_path_does_write(self, convo, runner):
        """One passing case, so the refusals above are not passing vacuously."""
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is True
        assert len(runner.calls) == 1
        assert runner.calls[0][:2] == ["msg", "send"]


# =============================================================================
# The bounded await
# =============================================================================


#: Cancel outcomes whose spoken line ASSERTS that nothing was sent. These are
#: the ones that may never be spoken about a token whose write has run — the
#: §3.6 over-claim, which is what #990 is.


# =============================================================================
# Argv freezing
# =============================================================================


class TestArgvFreezing:
    def test_confirming_one_proposal_never_runs_another(self, convo, runner):
        first = convo.announced_proposal(
            session="orchestrator", instruction="restart it"
        )
        convo.announced_proposal(session="other-session", instruction="delete it")
        convo.approve(first)

        assert convo.spine.confirm(first.token).approved is True
        argv = " ".join(runner.calls[0])
        assert "orchestrator" in argv
        assert "other-session" not in argv
        assert "delete it" not in argv
        assert "restart it" in argv


# =============================================================================
# The ring
# =============================================================================


# =============================================================================
# Attribution (spec §4b)
# =============================================================================


# =============================================================================
# The NATO alphabet and the near-miss diagnosis (#1039)
# =============================================================================


# =============================================================================
# Outcomes speak, and say different things
# =============================================================================


class TestOutcomesAreDistinctAndSpoken:
    """The return-value half of §3.4. The half that matters — that it reaches
    the ear — is asserted on the data channel in test_voice_announcer.py."""

    def _outcomes(self, convo, clock):
        cases = {}
        cases["no_proposal"] = convo.spine.confirm("never-minted")

        unannounced = convo.propose()
        cases["not_announced"] = convo.spine.confirm(unannounced.token)

        expired = convo.announced_proposal()
        convo.approve(expired)
        clock.advance(confirm.PROPOSAL_TTL_S + 1)
        cases["expired"] = convo.spine.confirm(expired.token)

        replayed = convo.announced_proposal()
        convo.approve(replayed)
        convo.spine.confirm(replayed.token)
        cases["replayed"] = convo.spine.confirm(replayed.token)

        rejected = convo.announced_proposal()
        convo.says("Yeah.")
        cases["refused"] = convo.spine.confirm(rejected.token)

        # The code word rendered one letter off — the transcriber's doing, not
        # the owner's (#1039). Any one-char mutation is within the bound and,
        # with pairwise alphabet distances >= 3, never closer to another word.
        near = convo.announced_proposal()
        convo.says(f"confirm {near.nonce}z")
        cases["near_miss"] = convo.spine.confirm(near.token)

        denied = convo.announced_proposal()
        convo.says(f"no, confirm {confirm.spoken_nonce(denied.nonce)}")
        cases["denied"] = convo.spine.confirm(denied.token)

        timing = convo.announced_proposal()
        cases["pending_transcript"] = convo.spine.confirm(timing.token)
        return cases


    def _hard_to_reach_outcomes(self, convo, clock) -> set:
        """The outcomes the ordinary scenario table does not produce."""
        seen = set()

        # quoted_frame: the announcement frame echoed back with the RIGHT word.
        quoted = convo.announced_proposal()
        convo.says(f"to approve say confirm {quoted.nonce}")
        seen.add(convo.spine.confirm(quoted.token).reason)

        # too_many_attempts: the attempt that hits the cap must SAY it retired.
        capped = convo.announced_proposal()
        for _ in range(confirm.MAX_CONFIRM_ATTEMPTS - 1):
            convo.says("that is not the phrase")
            assert convo.spine.confirm(capped.token).reason == "refused"
        convo.says("still not the phrase")
        seen.add(convo.spine.confirm(capped.token).reason)

        # wrong_nonce
        wrong = convo.announced_proposal()
        other = next(w for w in confirm.NONCE_WORDS if w != wrong.nonce)
        convo.says(f"confirm {other}")
        seen.add(convo.spine.confirm(wrong.token).reason)

        # dispatch_failed
        failing = RecordingRunner({"success": False, "error": "target gone"})
        spine = confirm.ConfirmSpine(
            convo.ring, wait_s=0.0, runner=failing, clock=clock
        )
        sub = Conversation(convo.ring, spine)
        sub.seq = convo.seq + 500
        proposal = sub.announced_proposal()
        sub.approve(proposal)
        seen.add(spine.confirm(proposal.token).reason)

        # in_flight: a duplicate confirm arriving while the first is dispatching.
        # Last, and on its own sequence range, so its ring entries cannot sit
        # newer than another case's approval.
        nested = []
        busy_spine = confirm.ConfirmSpine(
            convo.ring,
            wait_s=0.0,
            runner=lambda argv: (
                nested.append(busy_spine.confirm(busy.token).reason),
                {"success": True},
            )[1],
            clock=clock,
        )
        busy_convo = Conversation(convo.ring, busy_spine)
        busy_convo.seq = sub.seq + 500
        busy = busy_convo.announced_proposal()
        busy_convo.approve(busy)
        busy_spine.confirm(busy.token)
        seen |= set(nested)

        # cancel_in_flight: a CANCEL arriving while a confirm is dispatching
        # (#990). Same re-entrant construction as in_flight, different caller,
        # and it must not report `denied` — that line asserts nothing was sent.
        cancelled = []
        race_spine = confirm.ConfirmSpine(
            convo.ring,
            wait_s=0.0,
            runner=lambda argv: (
                cancelled.append(race_spine.cancel(racing.token).reason),
                {"success": True},
            )[1],
            clock=clock,
        )
        race_convo = Conversation(convo.ring, race_spine)
        race_convo.seq = busy_convo.seq + 500
        racing = race_convo.announced_proposal()
        race_convo.approve(racing)
        race_spine.confirm(racing.token)
        seen |= set(cancelled)

        # build_failed: the argv could not be built — popped, never dispatched
        # (#1005). Distinct from dispatch_failed because the runner never ran.
        broken = convo.announced_proposal()
        convo.approve(broken)

        def _boom():
            raise RuntimeError("render blew up")

        broken.build_argv = _boom
        seen.add(convo.spine.confirm(broken.token).reason)

        # cancelled: the ordinary retraction. Split from `denied` because it
        # POPS — "say the phrase again" cannot work once it has.
        retracted = convo.announced_proposal()
        seen.add(convo.spine.cancel(retracted.token).reason)

        # nothing_to_cancel: a cancel with nothing of ours to retract. It must
        # not reuse `no_proposal`/`expired`, whose lines argue for re-proposing
        # the write the owner just took back.
        seen.add(convo.spine.cancel("no-such-token").reason)
        return seen


    def test_a_failed_dispatch_is_not_reported_as_queued(self, ring, clock):
        runner = RecordingRunner({"success": False, "error": "target gone"})
        spine = confirm.ConfirmSpine(ring, wait_s=0.0, runner=runner, clock=clock)
        convo = Conversation(ring, spine)
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "dispatch_failed"
        # Names the UNCERTAINTY as well as the next move. "nothing was sent"
        # would be a definite claim the system cannot verify — run_agentwire_cmd
        # reports success=False on a subprocess timeout, where the CLI may
        # already have enqueued — and pairing false certainty with "ask me
        # again" invites a re-propose that double-delivers.
        assert "can't tell whether it took effect" in verdict.spoken
        assert "check that session" in verdict.spoken.lower()
        assert "nothing was sent" not in verdict.spoken


# =============================================================================
# The write tool surface
# =============================================================================


# =============================================================================
# The bridge routes
# =============================================================================


# =============================================================================
# The honest limit (§3.7) — asserted, so a later edit cannot quietly narrow it
# =============================================================================


class TestTheQuotedFrameGuard:
    """The defence-in-depth guard in classify(), pinned on BOTH halves.

    This guard is the sole cover for a residual the PR documents —
    model-channel echo under failed AEC — and until this class existed,
    deleting it changed no test in the repository. A claimed protection
    nothing exercises is worse than an unclaimed one: a refactor removes it
    silently while the comment above it keeps asserting the coverage.

    Both halves, because a guard has two costs. Pinning only the refusal
    invites someone to WIDEN it later — and the guard is deliberately narrow
    (say-preceded AND approve-framed, both required), because in this channel
    a wrongly refused approval is not a safe failure: the owner says the
    right word, nothing happens, and there is no screen to explain why.
    """

    def test_the_announcement_frame_echoed_back_is_refused(self):
        assert confirm.classify(
            "to approve say confirm tango", "tango"
        ) == confirm.QUOTED_FRAME
        assert confirm.classify(
            "I'm ready to send it. To approve, say confirm tango.", "tango"
        ) == confirm.QUOTED_FRAME


#: Every ``SPOKEN`` line that would deny if it came back through the mic.
#: DERIVED from the map, never typed: a new refusal line carrying a denial
#: trigger is covered the day it is written, and a rewording that removes the
#: last one fails the control test below rather than silently emptying the
#: parametrization.


class TestTheNonceNeverLeavesTheGate:
    """Issue #953: the approving utterance is ``confirm <nonce>`` by
    construction, so putting it in the delivered body ships the nonce to the
    recipient's scrollback on EVERY approved write — and carries none of the
    verification §4b built the slot for. The slot now carries the owner's
    REQUEST utterance, captured at propose time, which a recipient genuinely
    can check the paraphrase against.
    """


    def test_a_stale_confirm_phrase_is_never_selected_as_the_request(
        self, convo, runner
    ):
        """The one path a nonce could re-enter: a prior proposal's approval or
        wrong-nonce utterance sitting newest in the ring at propose time. That
        is not a request, so selection skips it — falling back to the real
        request sentence, false-reject half covered by the fallback below."""
        convo.says("tell the orchestrator to restart the portal")
        convo.says("confirm zulu")
        proposal = convo.announced_proposal(instruction="restart the portal")
        convo.approve(proposal)
        convo.spine.confirm(proposal.token)
        body = runner.calls[0][-1]
        assert "zulu" not in body
        assert 'said: "tell the orchestrator to restart the portal"' in body
