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
import threading
import time

import pytest

from agentwire import core, inbox
from agentwire.voice_layer import confirm, tools, transcript, write_tools


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
def ring():
    return transcript.TranscriptRing()


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


class TestNonceGrammar:
    """The nonce has TWO failure directions and both are priced here.

    False accept (a write nobody authorized) is the obvious one. **False reject
    is the one that livelocks**: a correct approval that fails deterministically
    is reported as "say it again", so the owner repeats and fails identically,
    forever. The digit alphabet this replaced had exactly that property.
    """

    def test_the_alphabet_has_one_spelling_per_word(self):
        """No digits, no hyphens, nothing with a second rendering.

        "four seven" comes back as 47 / four seven / 4-7 / forty-seven. Pairing
        the least stable token type with an exact matcher is what produced the
        livelock.
        """
        for word in confirm.NONCE_WORDS:
            assert word.isalpha(), word
            assert word.islower(), word
            assert confirm.normalize(word) == word, word
        assert len(set(confirm.NONCE_WORDS)) == len(confirm.NONCE_WORDS)

    @pytest.mark.parametrize(
        "template",
        [
            "confirm {n}",
            "Confirm {n}.",
            "confirm {n} please",
            "yeah, confirm {n}",
            "okay — confirm {n}!",
            "  CONFIRM   {N}  ",
            "um, confirm {n} thanks",
        ],
    )
    def test_a_correct_approval_is_never_rejected(self, template):
        """The false-reject half. Every one of these is a CORRECT approval, and
        rejecting any of them produces a livelock, not a near-miss."""
        for nonce in confirm.NONCE_WORDS:
            text = template.format(n=nonce, N=nonce.upper())
            assert confirm.classify(text, nonce) == confirm.APPROVED, text

    def test_every_minted_nonce_round_trips_through_its_spoken_form(self):
        for _ in range(200):
            nonce = confirm.mint_nonce()
            phrase = f"confirm {confirm.spoken_nonce(nonce)}"
            assert confirm.classify(phrase, nonce) == confirm.APPROVED

    def test_digit_spellings_normalize_if_one_ever_reaches_the_alphabet(self):
        """Nothing mints digits, but the normalizer must not be the reason a
        future alphabet change livelocks."""
        assert confirm.normalize("confirm seven") == "confirm 7"
        assert confirm.normalize("Confirm 7.") == "confirm 7"

    def test_a_wrong_nonce_is_its_own_outcome(self):
        """"repeat it" and "ask what the code was" are different advice."""
        assert confirm.classify("confirm harbor", "tango") == confirm.WRONG_NONCE
        assert confirm.classify("confirm tango", "tango") == confirm.APPROVED

    def test_an_absent_nonce_is_no_match(self):
        for text in ("confirm", "confirm it", "confirm that one"):
            assert confirm.classify(text, "tango") == confirm.NO_MATCH, text

    @pytest.mark.parametrize(
        "text", ["Okay.", "Yeah.", "Thank you.", "Yes.", "Mm-hmm.", "Sure.", "Got it."]
    )
    def test_whisper_class_silence_hallucinations_never_approve(self, text):
        """The failure the earlier denylist was quietly enumerating.

        ``gpt-4o-mini-transcribe`` is Whisper-lineage and emits confident short
        affirmatives on near-silence. A denylist of them is a list of the
        failures you have thought of; a nonce is not in that prior.
        """
        assert confirm.classify(text, "tango") == confirm.NO_MATCH

    def test_an_approval_shaped_utterance_meant_for_someone_else_never_approves(self):
        for text in ("yeah, that's right, anyway", "yes go ahead and do that", "do it"):
            assert confirm.classify(text, "tango") == confirm.NO_MATCH, text

    def test_containment_is_safe_because_the_nonce_carries_the_entropy(self):
        """Whole-utterance strictness was a constraint of the "yes" grammar,
        which carried no entropy. It is obsolete here, and it cost the two most
        natural phrasings."""
        assert confirm.classify("confirm tango please", "tango") == confirm.APPROVED
        assert confirm.classify("yeah, confirm tango", "tango") == confirm.APPROVED

    @pytest.mark.parametrize(
        "text",
        [
            "confirm tango, it is not urgent",
            "confirm tango, the worker is on hold",
            "confirm tango, I never got the other one",
            "confirm tango, do not forget the other branch",
            "confirm tango, don't forget the other branch",
            "confirm tango, the deploy is on hold until Monday",
        ],
    )
    def test_ordinary_speech_is_not_read_as_a_retraction(self, text):
        """The FALSE-REJECT half of the denial grammar, priced.

        An earlier version matched not/never/hold/forget and turned all of
        these into "You said no, so I haven't sent it." The owner did not say
        no. In a hands-free channel a false reject is never free: it costs a
        whole proposal and there is no screen to explain why.
        """
        assert confirm.classify(text, "tango") == confirm.APPROVED, text

    @pytest.mark.parametrize(
        "text",
        [
            # THE INVERSION. Verified end to end through the real gate before
            # the fix: nonce juniper, owner says "don't confirm juniper",
            # verdict APPROVED, write went out. An explicit spoken refusal
            # authorizing the write is the exact opposite of this slice's job.
            "don't confirm tango",
            "Don't confirm tango.",
            "do not confirm tango",
            "don’t confirm tango",          # curly apostrophe, what a transcriber emits
            "never mind, confirm tango",
            "hold on, confirm tango",
            "hang on — confirm tango",
            "forget it, confirm tango",
            "don't send it, confirm tango",
            "confirm tango, actually don't",
        ],
    )
    def test_contracted_and_split_refusals_are_caught(self, text):
        """Driven through the REAL pipeline, which is the whole point.

        These were all APPROVED, and the root cause was normalization rather
        than the word list: ``_PUNCT_RE`` replaced punctuation with a SPACE, so
        "don't" normalized to "don t" and the ``dont`` alternative could never
        fire. ``donot`` and ``nevermind`` were dead the same way, since speech
        transcribes as "do not" and "never mind".

        **A reachability test over the grammar PASSES**, because every
        alternative matches when fed to itself. What fails is that
        normalization never PRODUCES those tokens. Testing a table's entries
        against themselves proves the table, not the path into it — so this
        starts from a raw utterance, exactly as a transcriber would emit it.
        """
        assert confirm.classify(text, "tango") == confirm.DENIED, (
            f"{text!r} normalized to {confirm.normalize(text)!r}"
        )

    def test_normalization_elides_apostrophes_rather_than_spacing_them(self):
        """The one line the inversion turned on, asserted directly."""
        assert confirm.normalize("don't") == "dont"
        assert confirm.normalize("don’t") == "dont"
        assert "don t" not in confirm.normalize("don't send it")

    @pytest.mark.parametrize(
        "text",
        [
            "confirm tango, no wait",
            "no, confirm tango",
            "confirm tango — actually stop",
            "cancel that, confirm tango",
            "confirm tango, nevermind",
        ],
    )
    def test_real_retractions_are_still_caught(self, text):
        """The false-ACCEPT half. A missed denial is recoverable — the write
        still needs a nonce — but it should not be missed."""
        assert confirm.classify(text, "tango") == confirm.DENIED, text

    @pytest.mark.parametrize(
        "text",
        [
            # The class, not a list of the ones we happened to think of.
            "wait for it, confirm tango",
            "wait for those, confirm tango",
            "wait for these, confirm tango",
            "wait for mine, confirm tango",
            "wait for both, confirm tango",
            "wait for everything, confirm tango",
            "wait for a second, confirm tango",
            "confirm tango — wait for it",
            "hold on a second, confirm tango",
            "hang on a minute, confirm tango",
            "wait a moment, confirm tango",
            "wait up, confirm tango",
        ],
    )
    def test_every_hold_denies_because_there_is_no_conditional_exception(self, text):
        """A conditional ``("wait", "for")`` exception was tried and removed.

        It failed BOTH ways: "wait for those/these/mine/both/everything"
        APPROVED (holds — the write went out), while "wait for that build"
        DENIED (a real condition). The comment claimed a determiner/noun test
        and the code was a hold-word denylist, three lines below a comment
        saying denylists were the thing being avoided.

        And inverting it does not rescue it: *"wait for a second"* (a hold) and
        *"wait for a build"* (a condition) are structurally identical, so no
        structural test separates them, and the only remaining instrument would
        be a list of time-unit nouns whose incompleteness FAILS OPEN.
        """
        assert confirm.classify(text, "tango") == confirm.DENIED, text

    @pytest.mark.parametrize(
        "text",
        [
            "confirm tango, wait for the tests to finish",
            "confirm tango, wait until Monday",
            "confirm tango, wait for that build",
        ],
    )
    def test_a_real_condition_also_denies_and_that_cost_is_accepted(self, text):
        """The price of the above, asserted so nobody "fixes" it by accident.

        These are genuine approvals-with-a-condition and they now deny. The
        owner re-proposes; nothing is lost but a turn. That is the recoverable
        direction, and it is the whole reason the exception was dropped rather
        than widened.
        """
        assert confirm.classify(text, "tango") == confirm.DENIED, text

    def test_the_post_approval_scan_uses_the_same_rule(self):
        """``carries_denial`` is a second entry point into the grammar, so the
        rule has to hold there too — an exception that only applied on one path
        would be a hole with a longer name."""
        for hold in ("wait for it", "wait for those", "hold on a second", "no"):
            assert confirm.carries_denial(hold) is True, hold
        for fine in ("don't forget the branch", "not urgent", "on hold"):
            assert confirm.carries_denial(fine) is False, fine

    def test_no_enumeration_sits_on_the_side_where_being_wrong_writes(self):
        """The property, asserted instead of the cardinality.

        Counting exceptions does not bound the risk: the old design had 3
        conditional exceptions and the danger lived in a 17-entry hold-word
        list the count never covered. What bounds the risk is that **every
        surviving exception is a CLOSED phrase, not an open class** — so its
        incompleteness cannot fail open, because there is no next word to have
        missed.

        The rule: when a set must be enumerated, enumerate the side whose
        incompleteness is safe.
        """
        # The fail-open enumeration is gone entirely, not shortened.
        assert not hasattr(confirm, "_BARE_DEICTICS")
        assert not hasattr(confirm, "_CONDITIONAL_DENIAL_EXCEPTIONS")

        # What remains is two closed phrases, spelled out so any change to them
        # shows up in this test's own diff.
        assert confirm._DENIAL_EXCEPTIONS == frozenset({("dont", "forget")})
        assert confirm._DENIAL_BIGRAM_EXCEPTIONS == frozenset(
            {("do", "not", "forget")}
        )
        # Each must be anchored on a real denial trigger, or it suppresses
        # nothing and is dead weight pretending to be policy.
        for first, _ in confirm._DENIAL_EXCEPTIONS:
            assert first in confirm._DENIAL_WORDS, first
        for first, second, _third in confirm._DENIAL_BIGRAM_EXCEPTIONS:
            assert (first, second) in confirm._DENIAL_BIGRAMS

    def test_bigram_order_is_what_separates_the_two_measured_cases(self):
        """"hold on" denies; "on hold" does not.

        Bare-word matching cannot express that distinction, which is why
        dropping the bare words was right and dropping the retractions with them
        was not. The ordered bigram is the precise instrument for both.
        """
        assert confirm.classify("hold on, confirm tango", "tango") == confirm.DENIED
        assert (
            confirm.classify("confirm tango, the worker is on hold", "tango")
            == confirm.APPROVED
        )

    def test_a_self_correction_reads_as_denied_not_as_a_typo(self):
        """"say it again" and "stop" are opposite advice."""
        assert confirm.classify("confirm tango, no wait", "tango") == confirm.DENIED
        assert confirm.classify("no, confirm tango", "tango") == confirm.DENIED
        assert confirm.classify("cancel that, confirm tango", "tango") == confirm.DENIED


# =============================================================================
# The mandated mutations
# =============================================================================


class TestGateRefusals:
    def test_confirm_with_no_prior_proposal_does_not_write(self, spine, runner):
        verdict = spine.confirm("a-token-nobody-minted")
        assert verdict.approved is False
        assert verdict.reason == "no_proposal"
        assert runner.calls == []

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

    def test_an_absent_nonce_does_not_write(self, convo, runner):
        proposal = convo.announced_proposal()
        convo.says("yes, go ahead and send it")
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "refused"
        assert runner.calls == []

    def test_an_approval_that_began_before_the_proposal_was_spoken_does_not_write(
        self, convo, runner
    ):
        """The predicate that used to invert, twice.

        Ordering is the client's real event sequence, not a synthetic
        timestamp. A receipt-time design stamps the utterance when
        transcription finishes — after the proposal — and approves.
        """
        proposal = convo.propose()
        convo.approve(proposal)  # spoken and finished first...
        convo.buddy_speaks(proposal)  # ...then the buddy finishes stating it

        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "pending_transcript"
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

    def test_a_later_unrelated_remark_does_not_retroactively_deny(
        self, convo, runner
    ):
        """The post-approval scan is bounded to the approval-to-confirm window.

        An unbounded ring-tail scan lets an utterance from any later moment —
        including one arriving during a retry's bounded await — retroactively
        deny an approval, and report "You said no" about something said in a
        different context.
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        convo.says("and it is not urgent by the way")
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is True, verdict.reason
        assert len(runner.calls) == 1

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

    def test_a_denial_that_lands_as_harmless_lets_the_approval_through(
        self, convo, runner
    ):
        """The other half: waiting must not become a livelock."""
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        pending = convo.starts_speaking()
        assert convo.spine.confirm(proposal.token).reason == "pending_transcript"

        convo.finishes_speaking(pending, "thanks, that's the one")
        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1

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

    def test_an_unannounced_proposal_cannot_be_confirmed(self, convo, runner):
        """Barge-in: the owner cannot approve what they have not heard.

        Belt and braces with the nonce, which they also could not have heard.
        Two independent barriers, and this is the one that does not depend on
        the nonce being unguessable.
        """
        proposal = convo.propose()
        convo.says(f"confirm {confirm.spoken_nonce(proposal.nonce)}")
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "not_announced"
        assert runner.calls == []

    def test_the_happy_path_does_write(self, convo, runner):
        """One passing case, so the refusals above are not passing vacuously."""
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is True
        assert len(runner.calls) == 1
        assert runner.calls[0][:2] == ["msg", "send"]


class TestTokenIsNotBurnedOnAMiss:
    """§3.3's trap: "wait" must be true advice.

    If a timing miss consumed the token, ``pending_transcript``'s spoken reason
    ("give me a second, don't repeat it yet") would be a lie — waiting would
    accomplish nothing, and the owner would have been told to do the one thing
    that cannot work.
    """

    def test_a_confirm_before_its_transcript_does_not_consume_the_token(
        self, convo, runner
    ):
        proposal = convo.announced_proposal()
        item = convo.says("", transcribe=False)  # audio committed, no text yet

        first = convo.spine.confirm(proposal.token)
        assert first.reason == "pending_transcript"
        assert runner.calls == []

        convo.transcribe_late(item, f"confirm {confirm.spoken_nonce(proposal.nonce)}")
        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1

    def test_a_timing_miss_does_not_count_against_the_attempt_budget(
        self, convo, runner
    ):
        proposal = convo.announced_proposal()
        for _ in range(confirm.MAX_CONFIRM_ATTEMPTS + 3):
            assert convo.spine.confirm(proposal.token).reason == "pending_transcript"
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True

    def test_repeated_rejections_do_eventually_discard_the_proposal(
        self, convo, runner
    ):
        proposal = convo.announced_proposal()
        for _ in range(confirm.MAX_CONFIRM_ATTEMPTS):
            convo.says("nope, that's not it")
            assert convo.spine.confirm(proposal.token).approved is False
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).reason == "no_proposal"
        assert runner.calls == []


# =============================================================================
# The bounded await
# =============================================================================


class TestBoundedAwait:
    def test_confirm_waits_for_a_transcript_that_has_not_landed_yet(self, runner):
        """The real race: the model calls confirm before transcription finishes.

        Real threads on purpose — the point is a cross-thread wakeup, which a
        fake clock cannot exercise.
        """
        ring = transcript.TranscriptRing()
        spine = confirm.ConfirmSpine(ring, wait_s=2.0, runner=runner)
        convo = Conversation(ring, spine)

        proposal = convo.announced_proposal()
        item = convo.says("", transcribe=False)
        phrase = f"confirm {confirm.spoken_nonce(proposal.nonce)}"

        def transcribe_late():
            time.sleep(0.15)
            ring.transcribe(item, phrase)

        thread = threading.Thread(target=transcribe_late)
        thread.start()
        verdict = spine.confirm(proposal.token)
        thread.join()

        assert verdict.approved is True
        assert len(runner.calls) == 1

    def test_the_await_returns_promptly_when_the_transcript_is_already_there(
        self, convo, runner
    ):
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        started = time.monotonic()
        assert convo.spine.confirm(proposal.token).approved is True
        assert time.monotonic() - started < 0.5

    def test_waiting_longer_cannot_turn_a_non_approval_into_one(self, runner):
        ring = transcript.TranscriptRing()
        spine = confirm.ConfirmSpine(ring, wait_s=0.3, runner=runner)
        convo = Conversation(ring, spine)
        proposal = convo.announced_proposal()
        convo.says("Okay.")
        assert convo.spine.confirm(proposal.token).approved is False
        assert runner.calls == []

    def test_a_timeout_is_distinguishable_from_a_rejection(self, convo, runner):
        """The two demand OPPOSITE behaviour, so they must never collapse."""
        timing = convo.announced_proposal()
        timing_verdict = convo.spine.confirm(timing.token)

        rejected = convo.announced_proposal()
        convo.says("Yeah.")
        rejected_verdict = convo.spine.confirm(rejected.token)

        assert timing_verdict.reason == "pending_transcript"
        assert rejected_verdict.reason == "refused"
        assert timing_verdict.spoken != rejected_verdict.spoken
        assert timing_verdict.to_dict()["owner_should_wait"] is True
        assert rejected_verdict.to_dict()["owner_should_wait"] is False


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

    def test_mutating_the_stored_params_does_not_change_what_runs(
        self, convo, runner
    ):
        proposal = convo.announced_proposal(
            session="orchestrator", instruction="restart it"
        )
        proposal.params["session"] = "victim-session"
        proposal.params["message"] = "something else entirely"
        convo.approve(proposal)

        convo.spine.confirm(proposal.token)
        argv = " ".join(runner.calls[0])
        assert "victim-session" not in argv
        assert "something else entirely" not in argv
        assert "orchestrator" in argv

    def test_confirm_ignores_every_argument_except_the_token(self, convo, runner):
        proposal = convo.announced_proposal(session="orchestrator")
        convo.approve(proposal)
        result = write_tools.send_session_message(
            {
                "confirm_token": proposal.token,
                "session": "victim-session",
                "message": "something else",
            },
            convo.spine,
        )
        assert result["success"] is True
        assert "victim-session" not in " ".join(runner.calls[0])

    def test_exactly_one_field_completes_at_confirm_and_it_is_the_utterance(
        self, convo, runner
    ):
        """The precise shape of guarantee (a), enforced rather than described.

        Frozen at PROPOSE: the command, ``--to``, ``--from``, ``--kind``, the
        instruction, the proposal id and the nonce. Completed at CONFIRM:
        exactly one thing — the verbatim utterance inside the body — and it is
        read from the transcript ring, whose only writer is
        ``BuddyBridge.utterance`` (``POST /utterance``). No tool writes it.

        If this test ever has to be relaxed, §3.7's honest limit must be
        NARROWED to match, not qualified.
        """
        proposal = convo.announced_proposal(
            session="orchestrator", instruction="restart the portal"
        )
        # What the argv would be for two DIFFERENT authorizing utterances.
        first = proposal.build_argv("confirm tango")
        second = proposal.build_argv("something else entirely")

        # Everything but the body element is byte-identical.
        assert first[:-1] == second[:-1] == list(proposal.argv_prefix)
        # And the body differs only in the quoted `said:` clause.
        assert first[-1].split("┃ said:")[0] == second[-1].split("┃ said:")[0]
        assert first[-1].rsplit("┃", 1)[1] == second[-1].rsplit("┃", 1)[1]

    def test_no_tool_can_write_into_the_transcript_ring(self, convo, runner, monkeypatch):
        """The other half of the claim: the conversational model's only
        confirm-time input is a token, and nothing it can call reaches the ring.
        """
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})
        before = [(u.item_id, u.text) for u in convo.ring.snapshot()]
        for name, args in (
            ("propose_session_message", {"session": "orchestrator", "message": "hi"}),
            ("send_session_message", {"confirm_token": "nope"}),
            ("cancel_session_message", {"confirm_token": "nope"}),
            ("fleet_sessions", {}),
        ):
            tools.dispatch(name, args, "buddy", convo.spine)
        assert [(u.item_id, u.text) for u in convo.ring.snapshot()] == before

    def test_the_only_confirm_time_addition_is_machine_derived(self, convo, runner):
        proposal = convo.announced_proposal(instruction="restart the portal")
        convo.approve(proposal)
        convo.spine.confirm(proposal.token)
        body = runner.calls[0][-1]
        assert body.startswith(f"{confirm.VOICE_MARKER} restart the portal")
        assert proposal.nonce in body
        assert proposal.id in body


# =============================================================================
# The ring
# =============================================================================


class TestTranscriptRing:
    def test_ordering_is_speech_start_not_transcript_arrival(self, ring):
        """The inversion, isolated.

        The owner speaks (start seq 1), the buddy's proposal completes (seq 2),
        and only THEN does transcription finish. A receipt-time design sees the
        transcript arrive after the proposal and approves.
        """
        ring.speech_started("spoken_first", 1)
        ring.commit("spoken_first", 2)
        anchor = 3
        ring.transcribe("spoken_first", "confirm tango")

        assert ring.after(anchor) == []
        assert [u.text for u in ring.after(0)] == ["confirm tango"]

    def test_ordering_is_speech_start_and_not_the_commit(self, ring):
        """Barge-in, isolated: the utterance straddles the proposal.

        speech_started(1) < anchor(2) < commit(3). Ordering on the commit would
        return this entry — that is the hole. Ordering on speech-start does not.
        """
        ring.speech_started("straddler", 1)
        anchor = 2
        ring.commit("straddler", 3)
        ring.transcribe("straddler", "confirm tango")

        assert ring.after(anchor) == [], "ordered on commit — the barge-in hole"
        assert len(ring.after(0)) == 1

    def test_a_repeated_event_keeps_the_first_sequence(self, ring):
        first = ring.speech_started("item_a", 3)
        again = ring.speech_started("item_a", 9)
        assert again.speech_started_seq == first.speech_started_seq == 3

    def test_an_untranscribed_utterance_is_never_returned(self, ring):
        ring.speech_started("item_a", 1)
        ring.commit("item_a", 2)
        assert ring.after(0) == []
        ring.transcribe("item_a", "confirm tango")
        assert len(ring.after(0)) == 1

    def test_a_transcript_with_no_speech_start_is_flagged_estimated(self, ring):
        entry = ring.transcribe("orphan", "confirm tango")
        assert entry.estimated is True
        assert entry.ordered is False
        assert ring.after(0) == []

    def test_an_estimated_entry_never_approves(self, convo, runner):
        """Unknown ordering must fail closed, and as a WAIT rather than a
        rejection — the owner's correct move is still to hold on."""
        proposal = convo.announced_proposal()
        convo.approve(proposal, estimated=True)
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "pending_transcript"
        assert runner.calls == []

    def test_a_spent_entry_cannot_be_reused(self, ring):
        ring.speech_started("item_a", 1)
        ring.transcribe("item_a", "confirm tango")
        assert len(ring.after(0)) == 1
        ring.spend("item_a")
        assert ring.after(0) == []
        assert len(ring.after(0, include_spent=True)) == 1

    def test_the_ring_is_bounded(self):
        small = transcript.TranscriptRing(capacity=3)
        for index in range(6):
            small.speech_started(f"i{index}", index + 1)
            small.transcribe(f"i{index}", f"utterance {index}")
        assert len(small.snapshot()) == 3

    def test_an_equal_sequence_does_not_count_as_after(self, ring):
        ring.speech_started("item_a", 5)
        ring.transcribe("item_a", "confirm tango")
        assert ring.after(5) == []
        assert len(ring.after(4)) == 1

    def test_concurrent_writes_do_not_corrupt_the_ring(self, ring):
        """The bridge is a ThreadingHTTPServer; the ring is shared state."""

        def writer(base):
            for index in range(50):
                item = f"t{base}_{index}"
                ring.speech_started(item, base * 100 + index + 1)
                ring.commit(item, base * 100 + index + 2)
                ring.transcribe(item, f"utterance {base} {index}")

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entries = ring.snapshot()
        assert len(entries) == transcript.DEFAULT_CAPACITY
        assert len({e.item_id for e in entries}) == len(entries)


# =============================================================================
# Attribution (spec §4b)
# =============================================================================


class TestAttribution:
    def test_the_body_carries_instruction_verbatim_and_id_on_one_line(self):
        body = confirm.render_body("restart the portal", "confirm tango", "a1b2c3")
        assert "\n" not in body and "\r" not in body
        # The marker goes FIRST — with --kind request the kind slot
        # distinguishes nothing, so this is Slice 1's whole attribution.
        assert body.startswith(f"{confirm.VOICE_MARKER} restart the portal")
        assert 'said: "confirm tango"' in body
        assert body.endswith("#a1b2c3")

    def test_a_buddy_line_is_distinguishable_from_a_human_typed_one(self):
        buddy = inbox.Message(
            id="1700000000000000000-abc123",
            sender="buddy",
            to="orchestrator",
            kind=write_tools.WRITE_KIND,
            text=confirm.render_body("restart the portal", "confirm tango", "a1b2c3"),
            ts=1700000000000,
        ).render()
        human = inbox.Message(
            id="1700000000000000000-def456",
            sender="agentwire",
            to="orchestrator",
            kind="request",
            text="restart the portal",
            ts=1700000000000,
        ).render()
        assert buddy.startswith(f"[MSG from buddy · request] {confirm.VOICE_MARKER} ")
        assert "said:" in buddy and "┃" in buddy
        assert confirm.VOICE_MARKER not in human
        assert "said:" not in human

    def test_the_body_is_one_line_even_when_the_inputs_are_not(self):
        body = confirm.render_body(
            "restart\nthe\rportal", "confirm\ntango  please", "a1b2c3"
        )
        assert "\n" not in body and "\r" not in body
        assert "  " not in body

    def test_the_body_is_capped(self):
        body = confirm.render_body("go " * 200, "confirm tango " * 40, "a1b2c3")
        assert len(body) <= confirm.MAX_BODY_CHARS

    def test_the_verbatim_utterance_is_the_transcription_models_words(
        self, convo, runner
    ):
        """Not the buddy's paraphrase — the recipient can see a mis-paraphrase."""
        proposal = convo.announced_proposal(instruction="restart the portal")
        spoken = f"okay, confirm {confirm.spoken_nonce(proposal.nonce)}"
        convo.says(spoken)
        convo.spine.confirm(proposal.token)
        assert spoken in runner.calls[0][-1]

    def test_this_diff_does_not_touch_the_shared_kind_enum(self):
        """§4a is deferred to Slice 1b — deliberately, see write_tools' docstring.

        ``request`` is already in ESCALATE_KINDS, so the dead-letter-emails-the-
        owner property the ``voice`` kind was argued for is already achieved.
        """
        assert write_tools.WRITE_KIND == "request"
        assert "voice" not in inbox.KINDS
        assert write_tools.WRITE_KIND in inbox.ESCALATE_KINDS
        assert inbox.is_passive(write_tools.WRITE_KIND) is False


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

        denied = convo.announced_proposal()
        convo.says(f"no, confirm {confirm.spoken_nonce(denied.nonce)}")
        cases["denied"] = convo.spine.confirm(denied.token)

        timing = convo.announced_proposal()
        cases["pending_transcript"] = convo.spine.confirm(timing.token)
        return cases

    def test_each_outcome_reports_its_own_reason(self, convo, clock):
        for expected, verdict in self._outcomes(convo, clock).items():
            assert verdict.approved is False, expected
            assert verdict.reason == expected

    def test_every_outcome_has_something_specific_to_say(self, convo, clock):
        spoken = set()
        for label, verdict in self._outcomes(convo, clock).items():
            line = verdict.spoken
            assert line.strip(), f"{label} refused silently"
            assert len(line) > 25, label
            spoken.add(line)
        assert len(spoken) == 7, "outcomes must not share a spoken line"

    def test_the_wait_outcomes_are_flagged_as_such(self, convo, clock):
        for label, verdict in self._outcomes(convo, clock).items():
            expected = label in confirm.WAIT_OUTCOMES
            assert verdict.to_dict()["owner_should_wait"] is expected, label

    def test_the_spoken_map_and_the_taxonomy_agree_both_ways(self, convo, clock):
        """A one-directional guard is how a dead line ships.

        "Every outcome has a line" catches a mute refusal. It does NOT catch a
        LINE WITHOUT AN OUTCOME — and ``too_many_attempts`` shipped exactly
        that: a carefully written sentence with no producer, while the attempt
        that really retired a proposal said "say the phrase again" at the moment
        that stopped being possible.
        """
        assert set(confirm.SPOKEN) == confirm.REASONS
        for reason, line in confirm.SPOKEN.items():
            assert line.strip(), reason
        assert confirm.WAIT_OUTCOMES <= confirm.REASONS

        # And every reason is genuinely REACHABLE, not merely declared.
        observed = {v.reason for v in self._outcomes(convo, clock).values()}
        observed |= self._hard_to_reach_outcomes(convo, clock)
        assert observed == confirm.REASONS, confirm.REASONS - observed

    def _hard_to_reach_outcomes(self, convo, clock) -> set:
        """The three outcomes the ordinary scenario table does not produce."""
        seen = set()

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
        return seen

    def test_the_attempt_that_retires_the_proposal_says_so(self, convo, runner):
        """It must not say "say the phrase again" as it destroys the proposal."""
        proposal = convo.announced_proposal()
        reasons = []
        for _ in range(confirm.MAX_CONFIRM_ATTEMPTS):
            convo.says("that is not the phrase")
            reasons.append(convo.spine.confirm(proposal.token).reason)
        assert reasons[-1] == "too_many_attempts"
        assert reasons[:-1] == ["refused"] * (confirm.MAX_CONFIRM_ATTEMPTS - 1)
        # It names the owner's NEXT MOVE, not just the failure — the proposal
        # is gone, so "say the phrase again" would be the one useless answer.
        line = confirm.Verdict(approved=False, reason="too_many_attempts").spoken
        assert "ask me again" in line.lower()
        assert "say confirm" not in line.lower()
        assert runner.calls == []

    def test_every_outcome_names_the_owners_next_move(self):
        """The taxonomy rule, asserted across the whole map rather than per case.

        Reporting a failure without naming what to do next leaves the owner to
        infer it, from a channel with no screen. Each line must either tell them
        to act, or explicitly tell them to stand down.
        """
        act = (
            "ask me again", "asking me again", "say confirm", "tell me again",
            "don't repeat", "hang on", "ask me what", "check that session",
        )
        stand_down = (
            "not sending", "haven't sent", "haven't sent anything",
            "already passed that one on", "not doing it again",
        )
        for reason, line in confirm.SPOKEN.items():
            lowered = line.lower()
            assert any(cue in lowered for cue in act + stand_down), (
                f"{reason}: {line!r} leaves the owner nothing to do"
            )

    def test_every_refusal_is_flagged_must_speak(self, convo, clock):
        for label, verdict in self._outcomes(convo, clock).items():
            payload = verdict.to_dict()
            assert payload["success"] is False, label
            assert payload["must_speak"] is True, label
            assert payload["say"].strip(), label

    def test_success_says_queued_and_never_sent(self, convo, runner):
        """§3.6. ``msg send`` queues; delivery is at the next safe boundary and
        can defer. Claiming "sent" is worse than silence, because it is a claim.
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        payload = convo.spine.confirm(proposal.token).to_dict()
        assert payload["success"] is True
        assert payload["queued"] is True
        assert payload["sent"] is False
        assert "queued" in payload["say"].lower()
        assert "sent" not in payload["say"].lower()
        assert payload["must_speak"] is True

    def test_a_failed_dispatch_never_becomes_replayed_on_retry(self, ring, clock):
        """BLOCKING 1: the write never happened, so nothing may say it did.

        The proposal is retired and the ring entry spent before the argv runs.
        If the token also landed in ``_succeeded``, a retry — which is exactly
        what a model does after being told the handoff failed — got
        ``replayed``: "I already sent that one." Over-claiming the SEND, on the
        one path where the system already KNOWS it failed, to an owner who is
        not watching a screen.

        ``replayed`` must mean it really went out.
        """
        runner = RecordingRunner({"success": False, "error": "target gone"})
        spine = confirm.ConfirmSpine(ring, wait_s=0.0, runner=runner, clock=clock)
        convo = Conversation(ring, spine)
        proposal = convo.announced_proposal()
        convo.approve(proposal)

        first = spine.confirm(proposal.token)
        assert first.reason == "dispatch_failed"

        retry = spine.confirm(proposal.token)
        assert retry.reason == "dispatch_failed", "must not claim it was sent"
        assert retry.reason != "replayed"
        assert "failed" in retry.spoken.lower()
        assert "already sent" not in retry.spoken.lower()
        # And it is not silently re-attempted: a failed dispatch may have
        # partially written, so re-running risks a duplicate delivery.
        assert len(runner.calls) == 1

    def test_a_dispatch_that_raises_is_also_not_reported_as_sent(self, ring, clock):
        def explode(_argv):
            raise RuntimeError("boom")

        spine = confirm.ConfirmSpine(ring, wait_s=0.0, runner=explode, clock=clock)
        convo = Conversation(ring, spine)
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        assert spine.confirm(proposal.token).reason == "dispatch_failed"
        assert spine.confirm(proposal.token).reason == "dispatch_failed"

    def test_replayed_still_means_it_really_went_out(self, convo, runner):
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True
        assert convo.spine.confirm(proposal.token).reason == "replayed"
        assert len(runner.calls) == 1

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
        assert "can't tell whether it went out" in verdict.spoken
        assert "check that session" in verdict.spoken.lower()
        assert "nothing was sent" not in verdict.spoken


# =============================================================================
# The write tool surface
# =============================================================================


class TestWriteToolSurface:
    @pytest.fixture
    def live(self, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})

    def test_propose_writes_nothing_and_returns_a_spoken_phrase(
        self, convo, runner, live
    ):
        result = write_tools.propose_session_message(
            {"session": "orchestrator", "message": "restart the portal", "_buddy": "buddy"},
            convo.spine,
        )
        assert result["success"] is True
        assert result["needs_spoken_approval"] is True
        assert result["confirm_phrase"].startswith("confirm ")
        assert result["anchor_proposal_id"] == result["proposal_id"]
        assert result["must_speak"] is True
        assert runner.calls == []

    def test_a_garbled_session_name_fails_closed(self, convo, runner, live):
        for bad in ("--help", "../etc/passwd", "", None):
            with pytest.raises(tools.ToolError):
                write_tools.propose_session_message(
                    {"session": bad, "message": "hello", "_buddy": "buddy"}, convo.spine
                )
        assert runner.calls == []

    def test_a_cold_fleet_refuses_instead_of_queueing_into_the_void(
        self, convo, runner, monkeypatch
    ):
        """Spec §5, and the refusal is words the buddy can say."""
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"something-else"})
        with pytest.raises(tools.ToolError, match="Nothing is listening"):
            write_tools.propose_session_message(
                {"session": "orchestrator", "message": "hello", "_buddy": "buddy"},
                convo.spine,
            )
        assert runner.calls == []

    def test_the_buddy_has_no_tool_that_starts_a_session(self):
        """Spec §5, structurally.

        The pull toward "let it bootstrap ONE orchestrator when nothing is live"
        is real and is not built. Asserted as an absence, because that boundary
        dies quietly — by a plausible tool being added — not loudly.

        Scoped to WRITE tools: ``fleet_worktrees`` reads, and a read of the
        topology is not a step toward creating one.
        """
        names = {t.name for t in tools.write_tools()}
        forbidden = ("spawn", "worktree", "create", "start", "new", "orchestrator")
        for name in names:
            assert not any(word in name for word in forbidden), name
        # And the one write there is goes through msg send, not a session verb.
        assert names == {
            "propose_session_message",
            "send_session_message",
            "cancel_session_message",
        }

    def test_tmux_unreachable_is_an_outage_not_a_gone_recipient(
        self, convo, monkeypatch
    ):
        monkeypatch.setattr(inbox, "live_sessions", lambda: None)
        result = write_tools.propose_session_message(
            {"session": "orchestrator", "message": "hello", "_buddy": "buddy"},
            convo.spine,
        )
        assert result["success"] is True

    def test_an_absurdly_long_instruction_is_refused(self, convo, live):
        with pytest.raises(tools.ToolError):
            write_tools.propose_session_message(
                {
                    "session": "orchestrator",
                    "message": "x" * (write_tools.MAX_INSTRUCTION_CHARS + 1),
                    "_buddy": "buddy",
                },
                convo.spine,
            )

    def test_the_frozen_argv_is_a_msg_send_handoff_not_a_direct_action(
        self, convo, runner, live
    ):
        """Q2 settled as handoff: the only write is a message to a real session."""
        proposed = write_tools.propose_session_message(
            {"session": "orchestrator", "message": "restart the portal", "_buddy": "buddy"},
            convo.spine,
        )
        proposal = next(
            p for p in convo.spine.pending() if p.id == proposed["proposal_id"]
        )
        convo.buddy_speaks(proposal)
        convo.approve(proposal)
        write_tools.send_session_message(
            {"confirm_token": proposed["confirm_token"]}, convo.spine
        )
        argv = runner.calls[0]
        assert argv[:2] == ["msg", "send"]
        assert argv[2:8] == [
            "--to", "orchestrator", "--from", "buddy", "--kind", "request",
        ]
        assert len(argv) == 9  # prefix + exactly one body

    def test_cancel_never_writes(self, convo, runner, live):
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        result = write_tools.cancel_session_message(
            {"confirm_token": proposal.token}, convo.spine
        )
        assert result["success"] is False
        assert convo.spine.confirm(proposal.token).reason == "no_proposal"
        assert runner.calls == []

    def test_the_scripted_text_matches_the_word_alphabet(self, convo, runner, live):
        """Scripted instructions are the MECHANISM, so their content is not
        cosmetic — a wrong script is the mechanism working as designed with the
        wrong text.

        This carried "say the two digits separately" long after the alphabet
        became words, and survived precisely because it lives in a prompt string
        no test exercised. Now one does.
        """
        result = write_tools.propose_session_message(
            {"session": "orchestrator", "message": "restart it", "_buddy": "buddy"},
            convo.spine,
        )
        scripted = result["say"].lower()
        for stale in ("digit", "number", "two words", "spell it out", "separately"):
            assert stale not in scripted.replace("do not spell it out", ""), stale
        assert result["confirm_phrase"] == f"confirm {result['confirm_phrase'].split()[1]}"
        assert result["confirm_phrase"].split()[1] in confirm.NONCE_WORDS
        assert result["confirm_phrase"] in result["say"]

    def test_the_persona_has_no_digit_era_phrasing(self):
        from agentwire.voice_layer import instructions

        text = instructions.build_instructions().lower()
        assert "digits" not in text
        assert "confirm four seven" not in text
        assert "confirm tango" in text, "the example must show the real alphabet"

    def test_two_live_proposals_never_share_a_nonce(self, convo):
        """The two-proposal closure holds ONLY under uniqueness, and a
        spoken-friendly alphabet has a small collision space."""
        count = len(confirm.NONCE_WORDS)
        nonces = [convo.propose().nonce for _ in range(count)]
        assert len(set(nonces)) == count

    def test_exhausting_the_alphabet_fails_loudly_rather_than_colliding(self, convo):
        """Reusing a nonce would silently reopen "one approval, two proposals",
        so the alphabet running out must be an error, not a duplicate."""
        for _ in range(len(confirm.NONCE_WORDS)):
            convo.propose()
        with pytest.raises(RuntimeError, match="no free nonce"):
            convo.propose()


class TestDispatch:
    def test_a_write_tool_without_a_gate_is_refused_not_degraded(self):
        result = tools.dispatch(
            "propose_session_message",
            {"session": "orchestrator", "message": "hello"},
            "buddy",
        )
        assert result["success"] is False
        assert result["reason"] == "no_confirm_gate"
        assert "Nothing was sent" in result["error"]
        assert result["must_speak"] is True

    def test_write_tools_are_in_the_realtime_surface(self):
        names = {entry["name"] for entry in tools.realtime_tool_defs()}
        assert {"propose_session_message", "send_session_message",
                "cancel_session_message", "fleet_sessions"} <= names

    def test_confirm_takes_exactly_one_parameter(self):
        entry = next(
            e for e in tools.realtime_tool_defs() if e["name"] == "send_session_message"
        )
        assert list(entry["parameters"]["properties"]) == ["confirm_token"]
        assert entry["parameters"]["additionalProperties"] is False

    def test_every_tool_refusal_speaks(self, convo, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"something-else"})
        for name, args in (
            ("rm_rf_everything", {}),
            ("propose_session_message", {"session": "orchestrator", "message": "hi"}),
            ("propose_session_message", {"session": "--help", "message": "hi"}),
            ("send_session_message", {"confirm_token": ""}),
        ):
            result = tools.dispatch(name, args, "buddy", convo.spine)
            assert result["success"] is False, name
            assert result["must_speak"] is True, name
            assert result["say"].strip(), name

    def test_an_unexpected_tool_failure_still_speaks(self, convo, monkeypatch):
        def explode(_args):
            raise RuntimeError("boom")

        monkeypatch.setitem(
            tools.TOOLS_BY_NAME,
            "fleet_sessions",
            tools.ReadOnlyTool(name="fleet_sessions", description="", run=explode),
        )
        result = tools.dispatch("fleet_sessions", {}, "buddy", convo.spine)
        assert result["success"] is False
        assert result["must_speak"] is True
        assert result["say"].strip()


# =============================================================================
# The bridge routes
# =============================================================================


class TestBridgeRoutes:
    @pytest.fixture
    def bridge(self, tmp_path, monkeypatch):
        from agentwire.voice_layer import server

        monkeypatch.setattr(core, "CONFIG_DIR", tmp_path)
        runner = RecordingRunner()
        bridge = server.BuddyBridge("buddy", "token", runner=runner)
        bridge.runner = runner
        return bridge

    def test_a_speech_start_then_a_transcript_makes_a_usable_utterance(self, bridge):
        started = bridge.utterance({"item_id": "i1", "speech_started_seq": 1})
        assert started["recorded"] == "speech_started"
        assert bridge.utterance({"item_id": "i1", "commit_seq": 2})["success"] is True
        result = bridge.utterance({"item_id": "i1", "transcript": "confirm tango"})
        assert result["recorded"] == "transcript"
        assert result["estimated"] is False
        assert result["speech_started_seq"] == 1

    def test_a_commit_only_utterance_is_never_orderable(self, bridge):
        """Ordering on the commit is the barge-in hole; an entry with only a
        commit has no intent time and must not gate."""
        bridge.utterance({"item_id": "i2", "commit_seq": 4})
        result = bridge.utterance({"item_id": "i2", "transcript": "confirm tango"})
        assert result["estimated"] is True
        assert result["speech_started_seq"] == 0

    def test_an_event_without_any_sequence_is_rejected(self, bridge):
        assert bridge.utterance({"item_id": "i1"})["success"] is False

    def test_a_transcript_with_no_commit_is_flagged_estimated(self, bridge):
        assert bridge.utterance({"item_id": "i9", "transcript": "hi"})["estimated"] is True

    def test_malformed_payloads_are_rejected(self, bridge):
        assert bridge.utterance({})["success"] is False
        assert bridge.utterance({"item_id": "i1", "transcript": 5})["success"] is False
        assert bridge.anchor({})["success"] is False
        assert bridge.anchor({"proposal_id": "abc", "seq": 0})["success"] is False

    def test_the_anchor_route_makes_a_proposal_confirmable(self, bridge, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})
        proposed = bridge.tool_call(
            {
                "name": "propose_session_message",
                "arguments": {"session": "orchestrator", "message": "restart it"},
            }
        )
        assert proposed["success"] is True

        before = bridge.tool_call(
            {
                "name": "send_session_message",
                "arguments": {"confirm_token": proposed["confirm_token"]},
            }
        )
        assert before["reason"] == "not_announced"

        assert bridge.anchor(
            {"proposal_id": proposed["proposal_id"], "seq": 5}
        )["anchored"] is True

        after = bridge.tool_call(
            {
                "name": "send_session_message",
                "arguments": {"confirm_token": proposed["confirm_token"]},
            }
        )
        assert after["reason"] == "pending_transcript"
        assert bridge.runner.calls == []

    def test_the_bridge_wires_the_gate_into_tool_dispatch(self, bridge, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})
        proposed = bridge.tool_call(
            {
                "name": "propose_session_message",
                "arguments": {"session": "orchestrator", "message": "restart it"},
            }
        )
        bridge.anchor({"proposal_id": proposed["proposal_id"], "seq": 1})
        bridge.utterance({"item_id": "u1", "speech_started_seq": 2})
        bridge.utterance({"item_id": "u1", "commit_seq": 3})
        bridge.utterance(
            {"item_id": "u1", "transcript": proposed["confirm_phrase"]}
        )
        confirmed = bridge.tool_call(
            {
                "name": "send_session_message",
                "arguments": {"confirm_token": proposed["confirm_token"]},
            }
        )
        assert confirmed["success"] is True
        assert confirmed["queued"] is True
        assert len(bridge.runner.calls) == 1


# =============================================================================
# The honest limit (§3.7) — asserted, so a later edit cannot quietly narrow it
# =============================================================================


def _flat(text: str) -> str:
    """Whitespace-normalized, with markdown blockquote markers stripped.

    So an assertion survives a re-wrap of the prose, and matches the same
    sentence whether it is rendered as a docstring or as a wiki blockquote.
    """
    lines = [line.lstrip().removeprefix("> ").removeprefix(">") for line in text.splitlines()]
    return " ".join(" ".join(lines).split())


class TestHonestLimit:
    #: Every clause of §3.7. Each is a separate claim and each can be lost
    #: independently by a well-meaning edit, so each is asserted separately.
    CLAUSES = (
        "mis-transcription",
        "against an approval the conversational model invented",
        "does **not** cover every mis-transcription",
        "narrows but does not eliminate",
        "**not** a security boundary against an adversary",
    )

    def test_the_confirm_module_states_the_full_widened_guarantee(self):
        doc = _flat(confirm.__doc__ or "")
        for clause in self.CLAUSES:
            assert clause in doc, clause

    def test_the_wiki_page_states_it_too(self):
        from pathlib import Path

        page = Path(__file__).resolve().parents[2] / "docs" / "wiki" / "voice-layer.md"
        text = _flat(page.read_text(encoding="utf-8"))
        for clause in self.CLAUSES:
            assert clause in text, clause

    def test_nothing_rounds_the_guarantee_up(self):
        """The defect class this repo hit twice, caught mechanically.

        A prohibition ("do not paraphrase this as X") legitimately contains X,
        so each occurrence is checked in context: it must sit next to a
        negation. An unqualified assertion of X fails.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        targets = [
            root / "agentwire" / "voice_layer" / "confirm.py",
            root / "agentwire" / "voice_layer" / "write_tools.py",
            root / "agentwire" / "voice_layer" / "transcript.py",
            root / "docs" / "wiki" / "voice-layer.md",
        ]
        overclaims = (
            "confirm gate protects writes",
            "secures the write path",
            "two models must fail the same way",
            "the approval surface is speech itself",
        )
        negations = ("not ", "never", "do not", "wrong", "false", "refuse", "was ")
        for path in targets:
            flat = _flat(path.read_text(encoding="utf-8").lower())
            for claim in overclaims:
                start = 0
                while (index := flat.find(claim, start)) != -1:
                    context = flat[max(0, index - 160):index]
                    assert any(word in context for word in negations), (
                        f"{path.name} asserts '{claim}' without qualification"
                    )
                    start = index + len(claim)
