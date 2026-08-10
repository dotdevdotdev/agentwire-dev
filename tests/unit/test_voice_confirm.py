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
import re
import textwrap
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
        assert confirm.classify("confirm violet", "tango") == confirm.WRONG_NONCE
        assert confirm.classify("confirm tango", "tango") == confirm.APPROVED

    def test_no_nonce_word_has_a_second_transcriber_rendering(self):
        """The premise :func:`test_the_alphabet_has_one_spelling_per_word`
        states but does not test.

        That test checks ORTHOGRAPHY — alpha, lowercase, normalize-stable —
        which every word here passes by construction. It proves the fixture's
        own definition, not the property the docstring claims: that a
        Whisper-lineage transcriber emits ONE rendering for the word.

        Two shipped words failed that, and both livelocked rather than
        misfiring — neither variant is in the alphabet, so the outcome is
        ``no_match`` ("say confirm and then the word I gave you"), the owner
        repeats identically, and the proposal retires at
        ``MAX_CONFIRM_ATTEMPTS``:

        - ``harbor`` — en-GB ``harbour`` is an ordinary rendering.
        - ``ripcord`` — a compound; ``rip cord`` is an ordinary segmentation.

        This enumeration DOES sit on the fail-open side (an unlisted
        variant-prone word ships), and saying so is the point: the pin below
        is a regression anchor for two measured failures, not a proof of
        coverage. What bounds the class is the selection rule stated in
        :data:`confirm.NONCE_WORDS` — one morpheme, no en-US/en-GB split.
        """
        variant_prone = {
            # measured, and the reason this test exists
            "harbor", "harbour", "ripcord", "rip", "cord",
            # the same two classes, spelled out so the rule is legible
            "color", "colour", "gray", "grey", "meter", "metre",
            "airplane", "aeroplane", "donut", "doughnut",
            "backup", "sunset", "keyboard", "rainfall",
        }
        assert "harbor" in variant_prone and "ripcord" in variant_prone
        for word in confirm.NONCE_WORDS:
            assert word not in variant_prone, word

    @pytest.mark.parametrize(
        "text",
        [
            "confirm, uh, tango",
            "confirm um tango",
            "confirm, you know, tango",
            "confirm... er, tango, thanks",
        ],
    )
    def test_a_filler_between_confirm_and_the_nonce_still_approves(self, text):
        """The false-reject half, in the one place the grammar forgot it.

        The denial grammar strips ``_FILLERS`` before matching; the approval
        path required ``rest[0] == target`` with no skipping. So hesitating
        before a code word — which is exactly how people say code words —
        refused a CORRECT approval and burned an attempt.

        The tested templates above place fillers before and after the phrase,
        never BETWEEN it, so the gap was fixture-shaped rather than argued.
        """
        assert confirm.classify(text, "tango") == confirm.APPROVED, text

    def test_only_fillers_are_skipped_between_confirm_and_the_nonce(self):
        """The control that keeps the fix from being "skip anything".

        Both content words are still required, adjacent modulo disfluency.
        A real word between them is a different utterance, not a hesitation.
        """
        assert confirm.classify("confirm that tango", "tango") == confirm.NO_MATCH
        assert confirm.classify("confirm the tango step", "tango") == confirm.NO_MATCH
        # and the wrong-nonce scan uses the same skipping, or a hesitated
        # wrong code word reports "say it again" instead of "ask me the word".
        assert confirm.classify("confirm, uh, violet", "tango") == confirm.WRONG_NONCE

    def test_an_exception_never_masks_the_token_after_its_own_span(self):
        """BLOCKER: the masking loop ate one token too many.

        ``trio`` is non-empty whenever any token remains, so
        ``len(trio or pair)`` was 3 even when the 2-token exception
        ``("dont","forget")`` was what matched — the exception swallowed the
        word AFTER its own span, and that word was allowed to be a denial
        trigger. Reproduced through the real pipeline: both of these APPROVED.

        This falsified the load-bearing claim in
        :data:`confirm._DENIAL_EXCEPTIONS` — "suppresses exactly ONE token …
        cannot mask a denial signal anywhere else" — which is the entire
        argument for the entry being safe.
        """
        assert confirm.classify(
            "confirm tango, don't forget — hold on", "tango"
        ) == confirm.DENIED
        assert confirm.classify(
            "confirm tango, don't forget, cancel the other one", "tango"
        ) == confirm.DENIED
        assert confirm.carries_denial("don't forget, wait") is True

    #: Every exception, as a raw utterance a transcriber would actually emit.
    #: Hand-written because normalization is one-way — nothing derives "don't"
    #: from ``dont`` — but CHECKED against the live sets below in both
    #: directions, so an exception added without a spelling here fails this
    #: control loudly instead of escaping it.
    EXCEPTION_SPELLINGS = ("don't forget", "can't wait", "do not forget")

    def test_the_exception_spellings_cover_every_live_exception(self):
        """The control's own coverage, asserted rather than trusted.

        The first version of this test hardcoded ``("don't forget", "do not
        forget")`` while its docstring claimed "every exception" — and the
        entry this PR ADDS, ``("cant","wait")``, was not in it. No live defect
        (the cross-product denies), but the control did not cover the thing it
        shipped with, and the next exception would have escaped it silently.
        """
        spelled = {
            tuple(confirm.normalize(phrase).split())
            for phrase in self.EXCEPTION_SPELLINGS
        }
        live = confirm._DENIAL_EXCEPTIONS | confirm._DENIAL_BIGRAM_EXCEPTIONS
        assert spelled == live, live ^ spelled

    def test_a_denial_trigger_following_any_exception_still_denies(self):
        """The composition, generatively — the shape the existing generative
        test structurally cannot reach.

        ``test_an_unknown_word_after_a_denial_trigger_still_denies`` always
        places the trigger FIRST, so no arrangement it generates puts a
        trigger inside an exception's over-long mask. This one puts every
        trigger immediately after every exception, and BOTH sides are derived
        from the live tables: a new exception or a new trigger — including a
        gapped one — enters this cross-product automatically.
        """
        triggers = sorted(confirm._DENIAL_WORDS)
        triggers += [" ".join(bigram) for bigram in sorted(confirm._DENIAL_BIGRAMS)]
        for (first, second), gap_words in confirm._GAPPED_DENIAL_BIGRAMS.items():
            triggers.append(f"{first} {second}")
            triggers += [f"{first} {gap} {second}" for gap in sorted(gap_words)]
        for exception in self.EXCEPTION_SPELLINGS:
            for trigger in triggers:
                text = f"confirm tango, {exception} {trigger}"
                assert confirm.classify(text, "tango") == confirm.DENIED, text

    def test_the_exception_still_suppresses_its_own_span(self):
        """The other half: narrowing the mask must not kill the exception.

        "don't forget X" is not a retraction, and it was measured DENYING
        real approvals before the exception existed.
        """
        for text in (
            "confirm tango, don't forget the other branch",
            "confirm tango, do not forget the other branch",
            "confirm tango, don't forget",
        ):
            assert confirm.classify(text, "tango") == confirm.APPROVED, text

    @pytest.mark.parametrize(
        "text",
        [
            "never confirm tango",
            "Never confirm tango.",
            "you should never confirm tango",
            "never, uh, confirm tango",
        ],
    )
    def test_never_confirm_is_a_retraction(self, text):
        """BLOCKER: the inversion class, reached through ``never``.

        ``never`` is deliberately absent from :data:`confirm._DENIAL_WORDS`
        (recovered only as ``("never","mind")``) because it is among the
        commonest words in English. The general fallback for a missed
        retraction — "the write still needs a nonce, so the owner can simply
        not say it" — does not apply to THIS utterance: it IS the retraction
        and it CONTAINS the nonce.

        ``("never","confirm")`` is a closed ordered bigram: no genuine
        approval says those two tokens adjacent in that order.
        """
        assert confirm.classify(text, "tango") == confirm.DENIED, (
            f"{text!r} normalized to {confirm.normalize(text)!r}"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "never ever confirm tango",
            "never really confirm tango",
            "never once confirm tango",
            "never actually confirm tango",
            "never, seriously, confirm tango",
            "you should never ever confirm tango",
            "never ever, uh, confirm tango",
            "never ever ever confirm tango",
        ],
    )
    def test_an_intensified_never_confirm_is_still_a_retraction(self, text):
        """The blocker's own shape, one word wider.

        ``("never","confirm")`` as an ADJACENT pair left "never ever confirm
        tango" approving — the identical inversion, reached by the commonest
        intensifier there is. The entry was never really "adjacent tokens"
        anyway: ``_denial_tokens`` strips ``_FILLERS`` first, which is why
        "never, uh, confirm tango" denies. So it is "adjacent modulo a skip
        set", and this widens the skip set rather than inventing a mechanism.
        """
        assert confirm.classify(text, "tango") == confirm.DENIED, (
            f"{text!r} normalized to {confirm.normalize(text)!r}"
        )

    def test_the_post_approval_scan_sees_the_intensified_form_too(self):
        """``carries_denial`` was blind to it as well, which is what let it
        through AFTER the approval had already matched."""
        for text in ("never ever confirm that", "never actually confirm it"):
            assert confirm.carries_denial(text) is True, text

    def test_only_a_closed_gap_set_is_skipped_between_never_and_confirm(self):
        """The false-reject bound.

        An UNBOUNDED "never … confirm" rule would deny "I would never send
        that without checking — confirm tango", which is an approval. The gap
        is a closed set of degree adverbs, and any other word ends the run.
        """
        for text in (
            "confirm tango, I never asked you to confirm anything",
            "confirm tango, I would never send that without checking",
            "confirm tango, I never got the other one",
        ):
            assert confirm.classify(text, "tango") == confirm.APPROVED, text

    def test_the_never_gap_set_is_closed_class_and_fails_open(self):
        """The enumeration's SIDE, stated rather than assumed.

        Unlike ``_FILLERS`` (skipping one can only make a denial easier to
        match, so an unlisted filler fails closed), this set sits on the
        fail-open side: an unlisted gap word ends the run and the utterance
        approves. What bounds it is that the class is closed — degree adverbs
        and nothing else — and the file says so out loud rather than implying
        coverage.
        """
        assert confirm._GAPPED_DENIAL_BIGRAMS == {
            ("never", "confirm"): confirm._NEVER_GAP_WORDS
        }
        # No open-class word may enter the gap set: a noun or verb there turns
        # "never" back into the ordinary word it was excluded for being.
        for word in confirm._NEVER_GAP_WORDS:
            assert word.isalpha() and word.islower(), word
            assert word not in confirm._DENIAL_WORDS, word
        # And the pair itself is not ALSO a plain bigram — one rule, one place,
        # or the two spellings of it drift apart.
        assert ("never", "confirm") not in confirm._DENIAL_BIGRAMS

    def test_an_exception_mask_ends_the_gap_run_rather_than_being_skipped(self):
        """The one sentence in ``_gapped_bigram``'s docstring nothing pinned.

        It claims the run ends at a masked-out ``""``, "so an exception's
        suppression still stops this rule rather than being skipped through".
        Making the run SKIP the ``""`` instead left the whole suite green —
        a load-bearing docstring sentence with no test, which is the exact
        shape of the round-1 blocker.

        Direction matters and is stated: the skip variant is STRICTER, so this
        pins the false-reject side. What it protects is the composition rule
        the exceptions are built on — a suppressed span stays suppressed for
        every downstream rule, not just for the ones that already ran.
        """
        for text in (
            "never, don't forget, confirm tango",
            "never, do not forget, confirm tango",
        ):
            assert confirm.classify(text, "tango") == confirm.APPROVED, text

    def test_never_apart_from_confirm_is_still_ordinary_speech(self):
        """The false-reject price of the bigram above, pinned.

        Only the adjacent, ordered pair fires. ``never`` elsewhere in the
        utterance is the ordinary word it was excluded for being.
        """
        for text in (
            "confirm tango, I never got the other one",
            "confirm tango, I never said that",
            "confirm tango, that never happened",
        ):
            assert confirm.classify(text, "tango") == confirm.APPROVED, text

    def test_the_cant_wait_idiom_does_not_read_as_a_hold(self):
        """A closed idiom, suppressed on the file's own enumerate-the-safe-side
        rule: post-normalization ``cant`` is a distinct token, and "can't wait"
        has no reading meaning "hold off"."""
        assert confirm.classify(
            "confirm tango, tell them I can't wait to see it", "tango"
        ) == confirm.APPROVED
        assert confirm.carries_denial("can't wait to see it") is False

    def test_suppressing_cant_wait_does_not_suppress_a_real_hold(self):
        """Its false-accept price, bounded: the suppression covers exactly
        those two tokens, so any other retraction in the utterance still
        denies — including a second bare ``wait``."""
        for text in (
            "confirm tango, I can't wait — actually stop",
            "confirm tango, can't wait, no",
            "confirm tango, can't wait, hold on",
            "wait, confirm tango, I can't wait to see it",
        ):
            assert confirm.classify(text, "tango") == confirm.DENIED, text

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
    def test_a_wait_clause_denies_because_it_cannot_be_honoured(self, text):
        """This is CORRECT behaviour, not a tolerated false reject.

        The first framing here was "an accepted cost". That was wrong, and the
        reason is semantic rather than budgetary: **the write is ``msg send``
        and it fires immediately — the buddy has no defer mechanism at all.**
        So approving "confirm tango, wait until you hear back from the reviewer"
        would SEND NOW while the owner believes it is being held: a silent
        divergence between what they said and what happened, which is strictly
        worse than a re-propose.

        The correct home for such a clause is the INSTRUCTION, frozen at
        propose — "tell the reviewer to wait until X" — where it is content for
        the recipient rather than a condition on the send.

        The cost is also smaller than it looks: matching is on the exact token,
        so "waiting"/"waited"/"awaiting" do not fire (asserted below). Only the
        bare imperative does.

        This holds **only while recovery is cheap**, which is what
        :meth:`test_a_clean_approval_recovers_after_any_denial` guarantees.
        """
        assert confirm.classify(text, "tango") == confirm.DENIED, text

    def test_only_the_bare_imperative_wait_fires(self):
        """Bounds the cost of the rule above: inflected forms are ordinary
        speech about waiting, not instructions to hold."""
        for inflected in ("waiting", "waited", "awaiting", "waits"):
            assert confirm.carries_denial(inflected) is False, inflected
        assert confirm.carries_denial("wait") is True

    def test_the_post_approval_scan_uses_the_same_rule(self):
        """``carries_denial`` is a second entry point into the grammar, so the
        rule has to hold there too — an exception that only applied on one path
        would be a hole with a longer name."""
        for hold in ("wait for it", "wait for those", "hold on a second", "no"):
            assert confirm.carries_denial(hold) is True, hold
        for fine in ("don't forget the branch", "not urgent", "on hold"):
            assert confirm.carries_denial(fine) is False, fine

    def test_an_unknown_word_after_a_denial_trigger_still_denies(self):
        """The property, GENERATIVELY — not a list of cases.

        Cardinality never bounded the risk. What does is: for an arbitrary token
        the grammar has never seen, a denial trigger followed by it must still
        DENY. If any unknown token could suppress, there is positive-evidence
        logic on the fail-open side again.
        """
        import random
        import string

        rng = random.Random(20260806)
        for _ in range(300):
            unknown = "".join(rng.choice(string.ascii_lowercase) for _ in range(7))
            assert unknown not in confirm._DENIAL_WORDS
            for trigger in ("no", "stop", "wait", "cancel", "dont"):
                text = f"{trigger} {unknown}, confirm tango"
                assert confirm.classify(text, "tango") == confirm.DENIED, text

    def test_every_denial_entry_is_a_closed_phrase(self):
        """Item 5's audit, run over the live sets rather than trusted.

        A bigram/trigram exception suppresses a denial, so each must be a closed
        phrase with no next word to have missed. The check that catches the
        three entries removed this round: an entry whose FIRST token is an
        ordinary open-class word, matched with an ordinary open-class second
        token, is a fragment of speech rather than a phrase.
        """
        # Every bigram must be anchored: its first token is either a denial word
        # or a verb that only introduces a retraction in this position.
        anchors = confirm._DENIAL_WORDS | {
            "do", "never", "hold", "hang", "forget", "scrap", "belay"
        }
        for first, _second in confirm._DENIAL_BIGRAMS:
            assert first in anchors, f"{first!r} is an open-class anchor"
        # The removed ones stay removed — each denied a real approval.
        for gone in (("not", "that"), ("back", "off")):
            assert gone not in confirm._DENIAL_BIGRAMS, gone
        for gone_word in ("cancelled", "canceled", "not", "never", "hold", "forget"):
            assert gone_word not in confirm._DENIAL_WORDS, gone_word

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
        # Fillers ARE enumerated, and that is safe in the opposite direction:
        # skipping one can only make a denial EASIER to match, so an unlisted
        # filler fails CLOSED.
        assert confirm._FILLERS
        for filler in confirm._FILLERS:
            assert filler not in confirm._DENIAL_WORDS, filler

        # The fail-open enumeration is gone entirely, not shortened.
        assert not hasattr(confirm, "_BARE_DEICTICS")
        assert not hasattr(confirm, "_CONDITIONAL_DENIAL_EXCEPTIONS")

        # What remains is three closed phrases, spelled out so any change to
        # them shows up in this test's own diff.
        assert confirm._DENIAL_EXCEPTIONS == frozenset(
            {("dont", "forget"), ("cant", "wait")}
        )
        assert confirm._DENIAL_BIGRAM_EXCEPTIONS == frozenset(
            {("do", "not", "forget")}
        )
        # Each must CONTAIN a real denial trigger, or it suppresses nothing and
        # is dead weight pretending to be policy. Position is not fixed: the
        # trigger anchors ``dont forget`` at the head and ``cant wait`` at the
        # tail, and requiring the head would have rejected the second for the
        # wrong reason.
        for pair in confirm._DENIAL_EXCEPTIONS:
            assert any(token in confirm._DENIAL_WORDS for token in pair), pair
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

    def test_a_clean_approval_recovers_after_any_denial(self, convo, runner):
        """Recovery is a PRECONDITION for every deliberate denial in this
        grammar, and for the sentence ``denied`` speaks.

        Binding the OLDEST approval made a retraction permanent: the
        post-approval scan started at that old match, so the intervening denial
        sat inside the window forever and no later approval could ever become
        the match. Composed with the wait-clause rule it was worse than a
        re-propose — the owner's natural first recovery, saying the phrase
        cleanly, failed and kept failing until the 120s TTL, with nothing
        telling them why.

        And it FALSIFIED the spoken line: ``denied`` says "say the phrase again
        when you're ready", which against the composed behaviour instructed the
        owner to do the one thing that could not work — the ``too_many_attempts``
        shape recreated inside the fix for it. So the line and the recovery are
        asserted TOGETHER, deliberately: neither is correct without the other.
        """
        for retraction in ("no wait", "hold on", "scrap that",
                           "wait for the tests to finish"):
            proposal = convo.announced_proposal()
            convo.says(f"confirm {proposal.nonce}, {retraction}")
            assert convo.spine.confirm(proposal.token).reason == "denied", retraction

            before = len(runner.calls)
            convo.says(f"confirm {proposal.nonce}")
            verdict = convo.spine.confirm(proposal.token)
            assert verdict.approved is True, f"no recovery after {retraction!r}"
            assert len(runner.calls) == before + 1

        # The sentence that promises exactly this must still say so.
        spoken = confirm.SPOKEN["denied"].lower()
        assert "say the phrase again" in spoken

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

    def test_an_intensified_never_confirm_after_the_approval_does_not_write(
        self, convo, runner
    ):
        """The spine-level repro of the reviewer's blocker.

        The owner approves, then takes it back with "never ever confirm that"
        before the model gets round to calling confirm. The post-approval scan
        runs the same grammar, so an adjacency-only entry let the write
        EXECUTE — the take-back was spoken, in time, and did not count.
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        convo.says("never ever confirm that")
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is False
        assert verdict.reason == "denied"
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

    def test_a_denial_that_STARTS_during_the_await_still_blocks(self, runner):
        """The half of the ``unheard_between`` guarantee the ceiling excluded.

        ``ceiling`` was snapshotted BEFORE the ≤2.5s await and widened only by
        the sequences of entries that came back in ``found``. A denial the
        owner BEGINS during the await lands its ``speech_started`` above that
        snapshot and carries no transcript, so it is in neither set:
        ``after`` filters on ``complete`` and ``unheard_between`` excludes it
        for exceeding the ceiling. The write executed while a take-back was
        mid-transcription — the exact hazard the guard claims to close, closed
        only for utterances started BEFORE confirm was entered.

        The fixture has to be able to CONSTRUCT that shape, which the
        ``wait_s=0`` spine provably cannot: with no await there is no window
        for an utterance to start inside. Real threads, and the ordering is
        forced rather than raced — the denial's ``speech_started`` is emitted
        BEFORE the approval's transcript, so the await is still blocked when
        the sequence advances and the wakeup happens after it.
        """
        ring = transcript.TranscriptRing()
        spine = confirm.ConfirmSpine(ring, wait_s=2.0, runner=runner)
        convo = Conversation(ring, spine)

        proposal = convo.announced_proposal()
        approval = convo.says("", transcribe=False)  # spoken; no transcript yet
        phrase = f"confirm {confirm.spoken_nonce(proposal.nonce)}"

        denial_item = "item_denial_mid_await"

        def speak_during_the_await():
            time.sleep(0.15)                        # confirm is inside the await
            ring.speech_started(denial_item, convo._next())
            ring.transcribe(approval, phrase)       # ...and only then does it wake

        thread = threading.Thread(target=speak_during_the_await)
        thread.start()
        verdict = spine.confirm(proposal.token)
        thread.join()

        approved_entry = next(
            e for e in ring.snapshot() if e.item_id == approval
        )
        started = next(e for e in ring.snapshot() if e.item_id == denial_item)
        assert started.speech_started_seq > approved_entry.speech_started_seq, (
            "fixture must start the denial after the approval, or it proves nothing"
        )

        assert verdict.approved is False
        assert verdict.reason == "pending_transcript"
        assert runner.calls == []

        # And the false-reject half: once it lands harmless, the approval goes.
        ring.commit(denial_item, convo._next())
        ring.transcribe(denial_item, "thanks, that's the one")
        assert spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1

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


class TestTheUnheardWindowIsBounded:
    """#989, and it is TWO failures under one name — only one needs a clock.

    Re-reading ``high_seq`` after the await is the intended safety gain: a
    denial STARTED during the await blocks. Its price was that any
    ``speech_started`` with no transcript to follow sat in the window refusing
    every confirm as a WAIT outcome — no attempt burned, so
    ``too_many_attempts`` never fired and the owner heard "give me a second"
    for the whole 120s TTL and then "that one expired". A spoken LOOP, which in
    a screenless channel is the expensive failure.

    The two shapes, because a single flat wall-clock age is wrong at one end
    whichever value it takes:

    1. **Transcribed but empty** — a cough the model renders as ``""``. There is
       nothing to wait for and no clock is involved: an empty transcript
       positively carries no denial. Retired by ``Utterance.transcribed``, at
       zero false-reject cost.
    2. **Never transcribed** — a real bound, split on whether the audio buffer
       CLOSED. Committed: the utterance is over and ASR is merely overdue
       (``UNHEARD_COMMITTED_GRACE_S``). Not committed: the owner may still be
       mid-utterance, so the bound must exceed a plausible utterance
       (``UNHEARD_OPEN_UTTERANCE_S``), or the gate stops waiting for a
       retraction that is halfway spoken — the acting-twice direction.
    """

    def test_an_empty_transcript_does_not_hold_the_gate_at_all(
        self, convo, runner
    ):
        """Shape 1, and the clock never moves — that is the assertion.

        A cough transcribed as ``""`` is ``complete == False``, which is what
        put it in the window. It is ``transcribed == True``, which is what takes
        it out, immediately and with no bound to tune.
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        cough = convo.starts_speaking()
        convo.ring.commit(cough, convo._next())
        convo.ring.transcribe(cough, "")

        entry = next(e for e in convo.ring.snapshot() if e.item_id == cough)
        assert entry.complete is False, "fixture must build the empty rendering"
        assert entry.transcribed is True

        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1

    def test_a_committed_utterance_with_no_transcript_ages_out_on_the_ASR_grace(
        self, convo, clock, runner
    ):
        """Shape 2a. The buffer closed, so a missing transcript is overdue."""
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        blip = convo.starts_speaking()
        convo.ring.commit(blip, convo._next())  # ...and no transcript, ever

        # Inside the grace it still holds the gate: this is the half that must
        # NOT be given up, or a slow transcriber's denial is dropped and the
        # write goes out with a take-back mid-transcription.
        clock.advance(transcript.UNHEARD_COMMITTED_GRACE_S - 0.5)
        assert convo.spine.confirm(proposal.token).reason == "pending_transcript"
        assert runner.calls == []

        clock.advance(1.0)
        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1

    def test_an_open_utterance_gets_the_longer_bound_because_it_may_still_be_spoken(
        self, convo, clock, runner
    ):
        """Shape 2b, and the point is that it is NOT the ASR grace.

        No commit means the owner may still be talking. Ageing this out on the
        committed grace would stop waiting for a retraction they are in the
        middle of saying — so the same wall-clock age that retires a committed
        blip must leave this one holding the gate.
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        convo.starts_speaking()  # speech_started only: still mid-utterance

        clock.advance(transcript.UNHEARD_COMMITTED_GRACE_S + 1)
        assert convo.spine.confirm(proposal.token).reason == "pending_transcript"
        assert runner.calls == []

        clock.advance(transcript.UNHEARD_OPEN_UTTERANCE_S)
        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1

    def test_the_committed_grace_is_measured_FROM_THE_COMMIT(
        self, convo, clock, runner
    ):
        """``committed_at``, which the whole two-bound argument rests on.

        The split is "the audio buffer closed, so ASR is overdue" — overdue
        *from the close*, not from the speech start. Measured from
        ``received_at`` instead, a long utterance burns its entire grace before
        the transcript could possibly arrive: a 9-second sentence would age out
        1 second after committing rather than 10, which is the false-ACCEPT
        direction (a real denial dropped, the write goes out).
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        long_one = convo.starts_speaking()
        clock.advance(9.0)  # ...a long sentence
        convo.ring.commit(long_one, convo._next())

        entry = next(e for e in convo.ring.snapshot() if e.item_id == long_one)
        assert entry.committed_at > entry.received_at, (
            "fixture must separate the commit from the speech start, or this "
            "test cannot tell the two clocks apart"
        )

        # 5s past the COMMIT — inside the grace. Past the SPEECH START it is
        # 14s, well beyond it: the two answers differ, which is the point.
        clock.advance(5.0)
        assert convo.spine.confirm(proposal.token).reason == "pending_transcript"
        assert runner.calls == []

        clock.advance(transcript.UNHEARD_COMMITTED_GRACE_S)
        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1

    def test_the_bound_lands_inside_the_TTL_or_it_closes_nothing(self):
        """Both bounds have to retire the entry while the proposal is still
        alive. A bound at or past ``PROPOSAL_TTL_S`` leaves the loop exactly as
        it was and merely renames it."""
        assert (
            transcript.UNHEARD_COMMITTED_GRACE_S
            < transcript.UNHEARD_OPEN_UTTERANCE_S
            < confirm.PROPOSAL_TTL_S
        )
        # And the committed grace has to outlast an ordinary transcript, or the
        # bound itself becomes the false reject it was chosen to avoid.
        assert transcript.UNHEARD_COMMITTED_GRACE_S > confirm.APPROVAL_WAIT_S

    def test_the_module_no_longer_states_the_residual_it_fixed(self):
        """The prose half, in the direction that actually rots.

        ``confirm.py`` stated this residual AT ITS OWN USE SITE — "has no
        staleness bound", "never completes", "until the TTL expires it" — and
        those three sentences are false the moment the bound exists. A fix that
        leaves them ships a lie exactly where the next reader looks.
        """
        source = _flat(_confirm_source())
        for clause in (
            "has no staleness bound",
            "sits in this window and refuses every",
            "until the TTL expires it",
        ):
            assert clause not in source, clause
        # And it names where the bound went, so the division of labour between
        # the two modules is still legible from the caller.
        assert "The bound lives in TranscriptRing" in source

    def test_no_stale_sentence_survives_at_the_use_site(self):
        """The comment at the scan and the comment at the read must describe
        the SAME ceiling. A correct paragraph at one site does not repair a
        false sentence at the other — that is how the widened window would be
        reviewed as the old one."""
        source = _flat(_confirm_source())
        assert "high-water mark as of this confirm's entry" not in source
        assert "everything the owner had started by the time this confirm" in source


class TestSingleUseIsClaimedNotJudged:
    """Single-use has to be a property of the CLAIM, not of the timing.

    ``_claim`` neither removed the proposal nor marked it in flight, so
    consumption happened after the await and the judge. Two confirms carrying
    the same token could both pass the claim and both reach the runner. It was
    not reproducible in 250 threaded trials — client dispatch is sequential per
    response and the judge window is sub-timeslice — but "not observed" is the
    argument this module refuses everywhere else: each ``response.done`` spawns
    its own async IIFE and the bridge is a ``ThreadingHTTPServer``, so nothing
    in the code guarantees the sequencing that made it safe.

    The related wart is fixed by the same marker: a confirm arriving while the
    runner is mid-dispatch used to report ``no_proposal`` — "tell me again what
    you'd like sent" — which invites a re-propose that DOUBLE-SENDS.
    """

    def test_a_second_confirm_during_dispatch_is_refused_not_told_to_re_propose(
        self, convo
    ):
        """The re-entrant construction: the second confirm arrives from inside
        the runner, i.e. strictly while the first one is dispatching."""
        nested: list[str] = []
        calls: list[list[str]] = []

        def reentrant_runner(argv):
            calls.append(list(argv))
            nested.append(convo.spine.confirm(proposal.token).reason)
            return {"success": True}

        convo.spine._runner = reentrant_runner
        proposal = convo.announced_proposal()
        convo.approve(proposal)

        assert convo.spine.confirm(proposal.token).approved is True
        assert len(calls) == 1, "the duplicate must not reach the runner"
        assert nested == ["in_flight"], nested


    def test_a_confirm_while_another_is_awaiting_never_reaches_the_runner(
        self, runner
    ):
        """The claim is held across the AWAIT too, not only across dispatch —
        which is where the two-thread window is widest (up to 2.5s)."""
        ring = transcript.TranscriptRing()
        spine = confirm.ConfirmSpine(ring, wait_s=1.0, runner=runner)
        convo = Conversation(ring, spine)
        proposal = convo.announced_proposal()

        verdicts: list[confirm.Verdict] = []
        thread = threading.Thread(
            target=lambda: verdicts.append(spine.confirm(proposal.token))
        )
        thread.start()
        time.sleep(0.15)  # the first confirm is inside its bounded await
        duplicate = spine.confirm(proposal.token)
        thread.join()

        assert duplicate.reason == "in_flight"
        assert runner.calls == []
        # The duplicate is a WAIT outcome: the owner's move is to wait, and the
        # gate must stay open for the confirm that is actually running.
        assert duplicate.to_dict()["owner_should_wait"] is True
        assert duplicate.to_dict()["confirm_terminal"] is False

    def test_the_claim_is_released_on_every_exit(self, convo, runner):
        """The other half. A marker that is not released is a proposal wedged
        for its whole TTL, reported as "still working on it" forever — a
        silent loop in a channel with no screen."""
        proposal = convo.announced_proposal()
        assert convo.spine.confirm(proposal.token).reason == "pending_transcript"
        convo.says("that is not the phrase")
        assert convo.spine.confirm(proposal.token).reason == "refused"
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1
        # And after success it is replayed — the marker must not shadow that.
        assert convo.spine.confirm(proposal.token).reason == "replayed"
        assert len(runner.calls) == 1

    def test_a_dispatch_that_raises_still_releases_the_claim(self, ring, clock):
        def exploding(argv):
            raise RuntimeError("boom")

        spine = confirm.ConfirmSpine(
            ring, wait_s=0.0, runner=exploding, clock=clock
        )
        convo = Conversation(ring, spine)
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        assert spine.confirm(proposal.token).reason == "dispatch_failed"
        # Not in_flight: the failure is reported honestly on the retry.
        assert spine.confirm(proposal.token).reason == "dispatch_failed"


class TestCancelTakesTheSameClaim:
    """#990 — ``cancel()`` bypassed ``_claim()``, so it could speak a FALSE
    STATEMENT rather than merely lose a race.

    #987 made confirm-vs-confirm safe by construction: the second confirm gets
    ``in_flight``, a WAIT outcome whose line is worded — deliberately, §3.6 —
    to avoid claiming nothing was sent, *because the first confirm may be inside
    the runner*. ``cancel`` was the same shape left uncovered: it popped the
    proposal and said "I heard you hold off, so I haven't sent it" while the
    runner was sending. Screenless, the owner hears an affirmative "nothing was
    sent" about a write that went out, and has no way to discover otherwise.

    **And the first fix for it re-committed the same defect, inverted** (review
    round 2). Routing cancel through the claim and calling every claimed token
    "already going out" is false for most of the claim's life: it is taken at
    the top of ``confirm``, before the ≤2.5s await and before the judge, so the
    dominant occupant of ``_in_flight`` is a confirm still DECIDING. The race is
    therefore split on the thing the sentence is about — ``_dispatching`` (in
    the runner: the claim is true) versus ``_cancelled`` (a cancel during the
    await WINS, and the confirm finds out before it dispatches).
    """

    def test_a_cancel_during_the_AWAIT_wins_rather_than_being_told_it_is_too_late(
        self, runner
    ):
        """The dominant window, and the one the first fix got wrong.

        Nothing has been sent while the confirm sits in its bounded await, so
        "it's already going out" is a false statement AND the proposal it
        refuses to drop stays pending. Real threads, because ``wait_s=0``
        provably cannot construct a window for the cancel to land inside.
        """
        ring = transcript.TranscriptRing()
        spine = confirm.ConfirmSpine(ring, wait_s=2.0, runner=runner)
        convo = Conversation(ring, spine)
        proposal = convo.announced_proposal()

        verdicts: "list[confirm.Verdict]" = []
        thread = threading.Thread(
            target=lambda: verdicts.append(spine.confirm(proposal.token))
        )
        thread.start()
        time.sleep(0.15)  # the confirm is inside its bounded await
        cancelled = spine.cancel(proposal.token)
        thread.join()

        assert cancelled.reason == "cancelled", (
            "nothing has been sent yet, so the cancel wins outright"
        )
        # ...and the confirm does not then write behind the retraction.
        assert runner.calls == []
        assert verdicts[0].approved is False
        # The proposal is really gone — the measured failure was that it stayed
        # pending after being refused a cancel.
        assert spine.pending() == []
        assert spine.confirm(proposal.token).reason == "no_proposal"
        assert runner.calls == []

    def test_a_cancel_during_the_await_that_then_approves_still_does_not_write(
        self, runner
    ):
        """The barrier itself: the judge APPROVES and the dispatch is refused.

        The previous test's confirm times out, so it would refuse anyway. Here
        the approval is already in the ring, so only the cancel barrier stands
        between the verdict and the runner.
        """
        ring = transcript.TranscriptRing()
        spine = confirm.ConfirmSpine(ring, wait_s=2.0, runner=runner)
        convo = Conversation(ring, spine)
        proposal = convo.announced_proposal()
        approval = convo.says("", transcribe=False)
        phrase = f"confirm {confirm.spoken_nonce(proposal.nonce)}"

        def cancel_then_let_the_approval_land():
            time.sleep(0.15)              # the confirm is inside the await
            spine.cancel(proposal.token)  # ...retracted before it wakes
            ring.transcribe(approval, phrase)

        thread = threading.Thread(target=cancel_then_let_the_approval_land)
        thread.start()
        verdict = spine.confirm(proposal.token)
        thread.join()

        assert verdict.reason == "cancelled"
        assert runner.calls == [], "the write must not go out behind a retraction"
        # The approving utterance is NOT spent: nothing was acted on.
        spent = [u for u in ring.snapshot() if u.spent]
        assert spent == []

    def test_a_cancel_racing_a_DISPATCHING_confirm_does_not_claim_nothing_was_sent(
        self, convo, runner
    ):
        """The one true "too late": the cancel arrives from inside the runner,
        i.e. strictly while the argv is being dispatched."""
        cancels: "list[confirm.Verdict]" = []

        def reentrant_runner(argv):
            runner(argv)
            cancels.append(convo.spine.cancel(proposal.token))
            return {"success": True}

        convo.spine._runner = reentrant_runner
        proposal = convo.announced_proposal()
        convo.approve(proposal)

        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1, "fixture must have the confirm dispatching"

        (verdict,) = cancels
        assert verdict.reason == "cancel_in_flight"
        spoken = verdict.spoken.lower()
        assert "haven't sent" not in spoken, spoken
        assert "hold off" not in spoken, spoken
        # It is not terminal: closing the handshake here would close it out
        # from under the confirm that is still running.
        payload = verdict.to_dict()
        assert payload["confirm_terminal"] is False
        assert payload["owner_should_wait"] is True

    def test_only_a_DISPATCHING_confirm_produces_the_too_late_line(self, convo):
        """The distinction stated as a property rather than inferred from the
        two tests above: a token that is merely claimed must never produce it."""
        proposal = convo.announced_proposal()
        with convo.spine._lock:
            convo.spine._in_flight.add(proposal.token)
        assert convo.spine.cancel(proposal.token).reason == "cancelled"

        second = convo.announced_proposal()
        with convo.spine._lock:
            convo.spine._dispatching.add(second.token)
        assert convo.spine.cancel(second.token).reason == "cancel_in_flight"

    def test_no_cancel_outcome_leaves_a_live_proposal(self, convo, runner):
        """The false-reject half the review measured, stated as a PROPERTY.

        The shape was "refused cancel, then refused confirm, and the proposal
        sits to TTL with no second word". Its source was refusing during the
        await, which the split removes — but the guarantee worth keeping is
        broader than that one path, so it is asserted over every reachable
        cancel outcome instead of at the site that used to break it. A cancel
        the owner spoke must never leave the buddy holding the write.

        Deliberately a sweep rather than a single scenario: the failure was
        never in one branch, it was in the branch nobody enumerated.
        """
        outcomes: "dict[str, object]" = {}

        # denied — the ordinary win.
        plain = convo.announced_proposal()
        outcomes[convo.spine.cancel(plain.token).reason] = plain

        # cancel_in_flight — refused from inside the runner.
        racing = convo.announced_proposal()

        def reentrant(argv):
            runner(argv)
            outcomes[convo.spine.cancel(racing.token).reason] = racing
            return {"success": True}

        convo.spine._runner = reentrant
        convo.approve(racing)
        convo.spine.confirm(racing.token)
        convo.spine._runner = runner

        # replayed — the write really went out.
        done = convo.announced_proposal()
        convo.approve(done)
        assert convo.spine.confirm(done.token).approved is True
        outcomes[convo.spine.cancel(done.token).reason] = done

        # nothing_to_cancel — a second cancel.
        outcomes[convo.spine.cancel(plain.token).reason] = plain

        assert set(outcomes) == {
            "cancelled", "cancel_in_flight", "replayed", "nothing_to_cancel"
        }, outcomes
        live = {p.token for p in convo.spine.pending()}
        for reason, proposal in outcomes.items():
            assert proposal.token not in live, reason

    def test_the_refused_cancel_still_tells_the_owner_what_happens_next(self):
        """A cancel that cannot be honoured is the one moment the owner most
        needs the next move named — the thing they asked for is the thing that
        is no longer available."""
        line = confirm.SPOKEN["cancel_in_flight"].lower()
        assert "don't repeat" in line
        assert "tell you how it went" in line

    def test_an_unannounced_proposal_can_still_be_cancelled(self, convo, runner):
        """The claim is shared, MINUS the announcement requirement.

        "The buddy hasn't finished saying it" is a reason to refuse a confirm —
        the owner cannot have approved what they have not heard. It is not a
        reason to refuse a RETRACTION, and refusing one would leave the
        proposal live for its whole TTL over who was talking.
        """
        proposal = convo.propose()  # never announced
        assert convo.spine.cancel(proposal.token).reason == "cancelled"
        assert convo.spine.confirm(proposal.token).reason == "no_proposal"
        assert runner.calls == []

    def test_cancelling_a_write_that_already_went_out_says_so(self, convo, runner):
        """``denied`` here asserted "I haven't sent it" about a completed
        write. The claim already knows better — it just was not consulted."""
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True
        assert convo.spine.cancel(proposal.token).reason == "replayed"
        assert len(runner.calls) == 1

    def test_a_cancel_that_wins_the_race_still_retires_the_proposal(
        self, convo, runner
    ):
        """The ordinary path is unchanged, and the claim is RELEASED — a marker
        left set would wedge the token for its whole TTL."""
        proposal = convo.announced_proposal()
        assert convo.spine.cancel(proposal.token).reason == "cancelled"
        assert convo.spine.pending() == []
        assert convo.spine._in_flight == set()
        assert convo.spine._dispatching == set()
        assert convo.spine.confirm(proposal.token).reason == "no_proposal"
        assert runner.calls == []


#: Cancel outcomes whose spoken line ASSERTS that nothing was sent. These are
#: the ones that may never be spoken about a token whose write has run — the
#: §3.6 over-claim, which is what #990 is.
ASSERTS_NOTHING_SENT = frozenset({"cancelled", "nothing_to_cancel"})


class _ObservableLock:
    """A lock that lets an observer run at each RELEASE boundary.

    The point is to make "these two operations happen under ONE hold" a
    testable claim rather than a comment. Every moment the spine's lock is free
    is a moment another caller can observe it, so firing the observer at each
    release enumerates exactly the states the outside world can ever see —
    without needing to know which release is the interesting one, which is what
    makes the sweep survive a refactor that moves the boundary.

    Single-threaded on purpose: the hook runs while the lock is genuinely free,
    so there is no race to lose and no sleep to tune. A re-entrancy guard keeps
    the observer's own lock traffic from re-triggering it.
    """

    def __init__(self, inner, observer):
        self._inner = inner
        self._observer = observer
        self._firing = False
        self.releases = 0

    def acquire(self, *args, **kwargs):
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        self._inner.release()
        if self._firing:
            return
        self.releases += 1
        self._firing = True
        try:
            self._observer(self.releases)
        finally:
            self._firing = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class TestTheCancelBarrierIsONELockHold:
    """The invariant the whole #990 redesign rests on, and it had no pin.

    ``_confirm_claimed`` reads ``_cancelled`` and marks ``_dispatching`` in a
    single hold, and the module comment says why: reading and then marking
    leaves a window in which a cancel lands after the check and is told —
    truthfully by then, but wrongly at the moment it spoke — that the write was
    going out. Split into two holds, a cancel in the gap speaks *"I heard you
    hold off, so I haven't sent it"* while the runner runs. That is #990 in its
    ORIGINAL form, restored by a refactor the suite could not see.

    A stated invariant with no test is the defect this run has found repeatedly.
    So it is asserted the only way that survives the boundary moving: sweep
    EVERY point at which the spine's lock is observable, and require that none
    of them produces a cancel claiming nothing was sent while something was.
    """

    def _run(self, fire_at: int, *, dispatch_succeeds: bool = True):
        """One confirm, with a cancel fired at the *fire_at*-th lock release.

        **Parametrised over the dispatch OUTCOME, and that is not decoration.**
        The first version built a bare ``RecordingRunner()``, so every sweep ran
        a SUCCEEDING dispatch and half the state space simply did not exist
        while it swept: nothing ever entered ``_failed``, so the window in which
        a token is both "dispatch came back failed" and "dispatching" was
        unreachable — and the sweep reported exhaustive coverage of it. A sweep
        that is exhaustive over lock releases but not over outcomes is
        exhaustive in ONE AXIS, which is the shape of a green test that proves
        the fixture.
        """
        ring = transcript.TranscriptRing()
        runner = RecordingRunner(
            {"success": True} if dispatch_succeeds
            else {"success": False, "error": "target gone"}
        )
        spine = confirm.ConfirmSpine(ring, wait_s=0.0, runner=runner)
        convo = Conversation(ring, spine)
        proposal = convo.announced_proposal()
        convo.approve(proposal)

        seen: dict = {}

        def observer(index: int) -> None:
            if index != fire_at or "cancel" in seen:
                return
            # The spine's own record of the write, read at the moment the
            # cancel speaks. This is what makes "the outcome is already
            # recorded" a question the assertion can ask, rather than one
            # inferred from which release index we happen to be at.
            seen["recorded"] = (
                proposal.token in spine._succeeded
                or proposal.token in spine._failed
            )
            seen["cancel"] = spine.cancel(proposal.token).reason

        lock = _ObservableLock(threading.Lock(), observer)
        spine._lock = lock
        seen["confirm"] = spine.confirm(proposal.token)
        seen["writes"] = len(runner.calls)
        seen["releases"] = lock.releases
        return seen

    @pytest.mark.parametrize("dispatch_succeeds", [True, False])
    def test_no_observable_moment_lets_a_cancel_misreport_the_write(
        self, dispatch_succeeds
    ):
        """The pin. At every lock boundary, the two stories must agree."""
        probe = self._run(fire_at=0, dispatch_succeeds=dispatch_succeeds)
        total = probe["releases"]
        assert total >= 3, "the confirm must take the lock several times"

        stories = set()
        for index in range(1, total + 1):
            seen = self._run(fire_at=index, dispatch_succeeds=dispatch_succeeds)
            reason = seen.get("cancel")
            if reason is None:
                continue
            stories.add((reason, seen["writes"] > 0))

            # ONCE THE OUTCOME IS RECORDED, THE CANCEL MUST REPORT IT. The
            # third and fourth windows are both this rule violated from
            # different sides: a token sits in `_succeeded`/`_failed` while
            # still marked claimed (third) or still marked dispatching
            # (fourth), and cancel answered from the marker instead of from the
            # record. `cancel_in_flight` is the sharpest case — "it's already
            # going out ... we can undo it from there" is two definite claims
            # about a dispatch the system has already recorded it CANNOT
            # characterise (see `dispatch_failed`'s own line).
            if seen.get("recorded"):
                assert reason in ("replayed", "dispatch_failed"), (
                    f"release {index}: the outcome was already recorded and "
                    f"cancel said {reason!r}"
                )

            if seen["writes"]:
                # Something was sent, so no cancel may CLAIM OTHERWISE. Stated
                # as the property rather than as an allowed outcome: which
                # outcome is correct here depends on how far the confirm got
                # (`cancel_in_flight` mid-runner, `replayed` once recorded),
                # and pinning one of them would fail on a correct answer — as
                # this assertion did, on its first run, before it was rewritten
                # to say what it actually means.
                assert reason not in ASSERTS_NOTHING_SENT, (
                    f"release {index}: cancel said {reason!r}, which claims "
                    f"nothing was sent, while the runner had run"
                )
            else:
                # Nothing was sent, so the confirm must not have written and
                # the cancel must not have been told it was too late.
                assert seen["confirm"].approved is False, index
                assert reason != "cancel_in_flight", (
                    f"release {index}: cancel said it was too late with nothing sent"
                )

        # The sweep must actually have exercised BOTH sides of the barrier, or
        # it passes by never reaching the interesting states.
        assert len(stories) >= 2, stories
        assert any(sent for _, sent in stories), "no run reached the runner"
        assert any(not sent for _, sent in stories), "no run stopped at the barrier"

    def test_the_nothing_sent_set_really_says_nothing_was_sent(self):
        """The control for the set the sweep is written in terms of.

        A membership list is only as good as its membership: if ``cancelled``
        ever stops claiming "I haven't sent anything", the sweep above goes on
        passing while guarding nothing. Derived from the lines, not asserted
        about them.
        """
        for reason in ASSERTS_NOTHING_SENT:
            line = confirm.SPOKEN[reason].lower()
            assert "haven't sent" in line, reason
        # And the complement: the outcomes that may be spoken over a real write
        # must NOT carry the claim.
        for reason in ("cancel_in_flight", "replayed", "dispatch_failed"):
            assert reason not in ASSERTS_NOTHING_SENT
            assert "haven't sent" not in confirm.SPOKEN[reason].lower(), reason

    def test_a_cancel_after_a_recorded_write_never_says_nothing_was_sent(
        self, convo, runner
    ):
        """The third window the sweep found, kept as a named scenario.

        A confirm records its result and only THEN unwinds the claim, so
        between ``_succeeded.add`` and ``_in_flight.discard`` a token is both
        written and claimed. Testing ``_in_flight`` first answered a cancel
        there with "I haven't sent anything" about a write that had gone out —
        the same over-claim, in a window nobody had enumerated.
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1

        # Reconstruct the window: the write is recorded, the claim not yet
        # released.
        with convo.spine._lock:
            convo.spine._in_flight.add(proposal.token)
        try:
            verdict = convo.spine.cancel(proposal.token)
        finally:
            with convo.spine._lock:
                convo.spine._in_flight.discard(proposal.token)

        assert verdict.reason not in ASSERTS_NOTHING_SENT, verdict.reason
        assert verdict.reason == "replayed"

    @pytest.mark.parametrize(
        "recorded,marker,expected",
        [
            # The THIRD window: outcome recorded, claim not yet released.
            ("_succeeded", "_in_flight", "replayed"),
            ("_failed", "_in_flight", "dispatch_failed"),
            # The FOURTH: outcome recorded, the DISPATCHING marker not yet
            # cleared. `_dispatching` was tested above both terminal facts, so
            # a cancel here was told "it's already going out ... we can undo it
            # from there" about a dispatch already recorded as failed — two
            # definite claims about the one outcome `dispatch_failed` exists to
            # say cannot be characterised.
            ("_succeeded", "_dispatching", "replayed"),
            ("_failed", "_dispatching", "dispatch_failed"),
        ],
    )
    def test_a_recorded_outcome_outranks_every_in_progress_marker(
        self, convo, runner, recorded, marker, expected
    ):
        """Both halves of the rule, on both markers, as a table.

        The rule this branch introduced — *only facts about the WRITE can
        contradict a spoken claim about sending* — was applied against
        ``_in_flight`` and not against ``_dispatching``, and its ``_failed``
        half was never exercised at all: deleting that check left the whole
        suite green. A rule stated once and enforced against one of the two
        markers is the shape both of those defects share, so it is asserted as
        the cross-product rather than at the site that happened to break.
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True

        with convo.spine._lock:
            convo.spine._succeeded.discard(proposal.token)
            getattr(convo.spine, recorded).add(proposal.token)
            getattr(convo.spine, marker).add(proposal.token)
        try:
            verdict = convo.spine.cancel(proposal.token)
        finally:
            with convo.spine._lock:
                getattr(convo.spine, marker).discard(proposal.token)

        assert verdict.reason == expected, (recorded, marker, verdict.reason)
        assert verdict.reason not in ASSERTS_NOTHING_SENT

    def test_the_barrier_read_and_mark_are_SYNTACTICALLY_inside_one_hold(self):
        """The blind spot a boundary sweep has BY CONSTRUCTION.

        The sweep fires at lock RELEASES, so it can only see states that a
        release creates. Move the ``_cancelled`` read outside every lock and
        there is no release between the read and the mark to fire at — the
        sweep stays green while a cancel landing there is told "I haven't sent
        anything" with the runner already called. That is the same bug, in the
        one form the instrument cannot reach.

        So this one is read off the SOURCE. A structural assertion is the right
        tool exactly when the property is "these statements are in this scope",
        which no amount of runtime observation can establish.

        **This pin checks SCOPE. It does not check ORDER.** Marking
        ``_dispatching`` before reading ``_cancelled``, both inside the one
        hold, passes here — and returns without entering the ``try``/``finally``
        that releases the marker, so the marker leaks and every later cancel is
        told *"Too late to stop that one — it's already going out"* forever,
        about a write that never happened. The current order is correct and
        nothing on this branch does that; the point is that whoever moves these
        two statements is not protected against it by this test.
        """
        import ast
        import inspect

        source = inspect.getsource(confirm.ConfirmSpine._confirm_claimed)
        tree = ast.parse(textwrap.dedent(source))

        def holds_the_lock(node) -> bool:
            return isinstance(node, ast.With) and any(
                isinstance(item.context_expr, ast.Attribute)
                and item.context_expr.attr == "_lock"
                for item in node.items
            )

        def mentions(node, name: str) -> bool:
            return any(
                isinstance(child, ast.Attribute) and child.attr == name
                for child in ast.walk(node)
            )

        barriers = [
            node
            for node in ast.walk(tree)
            if holds_the_lock(node)
            and mentions(node, "_cancelled")
            and mentions(node, "_dispatching")
        ]
        assert len(barriers) == 1, (
            "the _cancelled read and the _dispatching mark must sit in exactly "
            f"one `with self._lock` block; found {len(barriers)}"
        )

        # ...and the two halves of the decision happen nowhere else. Two holds,
        # a mark placed after the block, or an unlocked read all fail here.
        #
        # Scoped to the READ and the MARK, not to every mention: the `finally`
        # that DISCARDS `_dispatching` is a legitimate second touch, and a
        # blanket "this name appears once" would forbid releasing the marker at
        # all. What must be atomic is deciding, not unwinding.
        barrier = barriers[0]
        span = range(barrier.lineno, (barrier.end_lineno or barrier.lineno) + 1)

        def is_mark(node) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_dispatching"
            )

        stray = [
            child.lineno
            for child in ast.walk(tree)
            if (
                (isinstance(child, ast.Attribute) and child.attr == "_cancelled")
                or is_mark(child)
            )
            and child.lineno not in span
        ]
        assert stray == [], (
            f"the _cancelled read or the _dispatching mark sits outside the "
            f"barrier hold, at lines {stray} (relative to _confirm_claimed)"
        )

    def test_the_two_flags_are_never_both_observable(self, convo):
        """The invariant stated directly as well as behaviourally.

        ``_cancelled`` and ``_dispatching`` are set by the two sides of one
        decision. If a token can ever be seen carrying both, the barrier was
        not atomic — a cancel recorded a retraction for a confirm that had
        already been cleared to dispatch.
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        both: list = []

        def observing_runner(argv):
            with convo.spine._lock:
                both.append(
                    convo.spine._cancelled & convo.spine._dispatching
                )
            convo.spine.cancel(proposal.token)
            with convo.spine._lock:
                both.append(
                    convo.spine._cancelled & convo.spine._dispatching
                )
            return {"success": True}

        convo.spine._runner = observing_runner
        assert convo.spine.confirm(proposal.token).approved is True
        assert both == [set(), set()], both


class TestACancelIsNeverAnsweredWithConfirmShapedAdvice:
    """The shared claim's lines are written for a CONFIRM, and two of them
    argue for the very write the owner just retracted.

    ``no_proposal`` says *"Tell me again what you'd like sent"* and ``expired``
    says *"Ask me again and I'll set it up fresh"*. Answering a retraction with
    either is the system pressing for the thing that was cancelled — in a
    screenless channel, with nothing on screen to reveal that it is answering
    the wrong question. Sharing the claim is right; sharing its vocabulary is
    not.
    """

    #: The confirm lines that would invite a re-propose, as substrings.
    RE_PROPOSE_CUES = ("tell me again", "ask me again", "set it up fresh")

    def test_a_second_cancel_does_not_ask_for_the_write_again(self, convo):
        proposal = convo.announced_proposal()
        assert convo.spine.cancel(proposal.token).reason == "cancelled"
        again = convo.spine.cancel(proposal.token)
        assert again.reason == "nothing_to_cancel"
        spoken = again.spoken.lower()
        for cue in self.RE_PROPOSE_CUES:
            assert cue not in spoken, cue

    def test_a_cancel_after_the_TTL_does_not_ask_for_the_write_again(
        self, convo, clock
    ):
        proposal = convo.announced_proposal()
        clock.advance(confirm.PROPOSAL_TTL_S + 1)
        verdict = convo.spine.cancel(proposal.token)
        assert verdict.reason == "nothing_to_cancel"
        spoken = verdict.spoken.lower()
        for cue in self.RE_PROPOSE_CUES:
            assert cue not in spoken, cue

    def test_the_confirm_path_keeps_those_lines(self, convo, clock):
        """The must-fail control. The cues are only wrong for a CANCEL — if a
        well-meaning edit strips them from the confirm lines too, the tests
        above would pass while the taxonomy quietly lost its next-move advice.
        """
        proposal = convo.announced_proposal()
        clock.advance(confirm.PROPOSAL_TTL_S + 1)
        assert "ask me again" in convo.spine.confirm(proposal.token).spoken.lower()
        assert "tell me again" in confirm.SPOKEN["no_proposal"].lower()

    def test_the_HAPPY_PATH_cancel_does_not_name_a_move_that_cannot_work(
        self, convo, runner
    ):
        """The same defect on the outcome nobody looked at: the cancel that
        SUCCEEDS.

        ``denied`` says "Say the phrase again when you're ready" — true of an
        in-band denial, which leaves the proposal live, and false of a cancel,
        which pops it. Following that advice lands on ``no_proposal`` — *"Tell
        me again what you'd like sent"* — one turn later, which is the exact
        line ``_cancel_refusal`` exists to keep off this path. So the advice
        was routed around at one door and re-entered through another.
        """
        proposal = convo.announced_proposal()
        verdict = convo.spine.cancel(proposal.token)
        assert verdict.reason == "cancelled"
        assert "say the phrase" not in verdict.spoken.lower()

        # The move it names must be one that WORKS. Following the retired
        # advice is what this asserts about: saying the phrase now cannot.
        convo.approve(proposal)
        after = convo.spine.confirm(proposal.token)
        assert after.approved is False
        assert after.reason == "no_proposal"
        assert runner.calls == []

    def test_the_in_band_denial_KEEPS_that_advice(self, convo, runner):
        """The must-fail control, and the reason this is two outcomes rather
        than one reworded line: on the in-band path the proposal is still live,
        the phrase still works, and telling the owner so is correct."""
        proposal = convo.announced_proposal()
        # A confirm word is required to reach the judge's DENIED branch at all
        # — `classify` returns NO_MATCH before consulting the denial grammar
        # otherwise, which is `refused`, not `denied`.
        convo.says("no, don't confirm that")
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.reason == "denied"
        assert "say the phrase again" in verdict.spoken.lower()
        # ...and it is true: the proposal survived, so the advice can be taken.
        assert proposal.token in {p.token for p in convo.spine.pending()}
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1

    def test_no_cancel_outcome_argues_for_the_write(self):
        """Swept over the whole cancel vocabulary rather than the two lines
        that were wrong, because the failure was a line reached from a path
        nobody enumerated."""
        for reason in ("cancelled", "nothing_to_cancel", "cancel_in_flight"):
            spoken = confirm.SPOKEN[reason].lower()
            for cue in self.RE_PROPOSE_CUES + ("say the phrase", "say confirm"):
                assert cue not in spoken, (reason, cue)

    def test_the_honest_refusals_are_passed_through_unchanged(self, convo, runner):
        """``replayed`` and ``dispatch_failed`` are TRUE of a cancel and invite
        no re-propose, so translating them would lose information."""
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True
        assert convo.spine.cancel(proposal.token).reason == "replayed"


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
        result = write_tools.WRITE_TOOL_FNS["send_session_message"](
            {
                "confirm_token": proposal.token,
                "session": "victim-session",
                "message": "something else",
            },
            convo.spine,
        )
        assert result["success"] is True
        assert "victim-session" not in " ".join(runner.calls[0])

    def test_nothing_completes_at_confirm_the_whole_argv_is_frozen(
        self, convo, runner
    ):
        """The precise shape of guarantee (a), enforced rather than described.

        Everything is frozen at PROPOSE: the command, ``--to``, ``--from``,
        ``--kind``, the instruction, the proposal id, the nonce, and — since
        #953 — the body's ``said:`` slot too, which carries the request
        utterance captured at propose. Confirm adds NOTHING to the argv:
        ``build_argv`` takes no parameters, so the approving utterance has no
        path back into the delivered body.

        If this test ever has to be relaxed, §3.7's honest limit must be
        NARROWED to match, not qualified.
        """
        convo.says("please tell the orchestrator to restart the portal")
        proposal = convo.announced_proposal(
            session="orchestrator", instruction="restart the portal"
        )
        frozen = proposal.build_argv()
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True
        # What ran is byte-identical to what was buildable before approval.
        assert runner.calls[0] == frozen
        assert frozen[:-1] == list(proposal.argv_prefix)

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

    def test_the_body_carries_the_id_and_never_the_nonce(self, convo, runner):
        proposal = convo.announced_proposal(instruction="restart the portal")
        convo.approve(proposal)
        convo.spine.confirm(proposal.token)
        body = runner.calls[0][-1]
        assert body.startswith("restart the portal")
        assert proposal.nonce not in body  # #953: the nonce stays in the gate
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
        # `.estimated` is what `_judge` filters on, so it is what this asserts.
        # The old `.ordered` property asserted here was a second, subtly
        # different spelling of the same question that the gate never consulted.
        assert entry.estimated is True
        assert entry.speech_started_seq == 0
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
        # No marker: since #985 attribution is the `voice` kind slot, and the
        # instruction leads the body.
        assert body.startswith("restart the portal")
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
        # The distinguisher is the KIND SLOT, in the same on-screen position
        # the `<voice>` marker used to occupy (#985).
        assert buddy.startswith("[MSG from buddy · voice] restart the portal")
        assert "said:" in buddy and "┃" in buddy
        assert "· voice]" not in human
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
        """Not the buddy's paraphrase — the recipient can see a mis-paraphrase.

        Since #953 the verbatim utterance in the body is the REQUEST, not the
        approval: the approval is a nonce and never leaves the gate."""
        request = "okay, get the portal restarted for me"
        convo.says(request)
        proposal = convo.announced_proposal(instruction="restart the portal")
        spoken = f"okay, confirm {confirm.spoken_nonce(proposal.nonce)}"
        convo.says(spoken)
        convo.spine.confirm(proposal.token)
        body = runner.calls[0][-1]
        assert request in body
        assert spoken not in body

    def test_the_body_tells_the_recipient_how_to_reply(self):
        """#962: in the live test the recipient answered in its own terminal
        and the reply never reached the buddy. The body itself must make the
        reply path obvious — a request whose reply channel is implicit gets an
        on-screen answer the owner never hears."""
        body = confirm.render_body(
            "restart the portal", "confirm tango", "a1b2c3", reply_to="buddy"
        )
        assert confirm.reply_nudge("buddy") in body
        assert 'msg send --to buddy' in body
        # The id stays last: the nudge slots in before it, never after.
        assert body.endswith("#a1b2c3")

    def test_the_nudge_is_dropped_whole_when_the_budget_cannot_fit_it(self):
        """Both halves: a nudge that fits ships verbatim; one that does not is
        dropped ENTIRELY, never truncated into half a command — and it is never
        the proposal id that pays for it."""
        body = confirm.render_body(
            "x" * confirm.MAX_RENDERED_INSTRUCTION_CHARS,
            "y" * confirm.MAX_UTTERANCE_CHARS,
            "a1b2c3",
            reply_to="buddy",
        )
        assert len(body) <= confirm.MAX_BODY_CHARS
        assert "reply:" not in body, "an over-budget nudge must vanish, not clip"
        assert body.endswith("#a1b2c3")

    def test_the_frozen_argv_carries_the_nudge_named_after_the_sender(
        self, convo, runner
    ):
        """End to end through Proposal.build_argv: the reply target is read
        from the frozen --from, so the nudge names whoever actually sent it."""
        proposal = convo.announced_proposal(instruction="check the server")
        convo.approve(proposal)
        convo.spine.confirm(proposal.token)
        body = runner.calls[0][-1]
        assert confirm.reply_nudge("buddy") in body

    def test_the_write_rides_the_shared_kind_enum(self):
        """§4a landed in Slice 1b (#985). The full ruling — active, escalatable,
        not an interrupt — is pinned in test_voice_kind.py; this asserts only
        that the buddy's write is the kind that carries it."""
        assert write_tools.WRITE_KIND == "voice"
        assert "voice" in inbox.KINDS
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

        # cancelled: the ordinary retraction. Split from `denied` because it
        # POPS — "say the phrase again" cannot work once it has.
        retracted = convo.announced_proposal()
        seen.add(convo.spine.cancel(retracted.token).reason)

        # nothing_to_cancel: a cancel with nothing of ours to retract. It must
        # not reuse `no_proposal`/`expired`, whose lines argue for re-proposing
        # the write the owner just took back.
        seen.add(convo.spine.cancel("no-such-token").reason)
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

    def test_approved_payload_carries_the_frozen_acted_session(self, convo, runner):
        """#967's missing key: the client used to GUESS which session a
        confirmed write acted on by remembering the last proposal — wrong the
        moment two proposals interleave. The spine knows exactly, from the
        proposal frozen at propose time, so the approved payload says so."""
        proposal = convo.announced_proposal(session="orchestrator")
        convo.approve(proposal)
        payload = convo.spine.confirm(proposal.token).to_dict()
        assert payload["success"] is True
        assert payload["acted_session"] == "orchestrator"

    def test_acted_session_rides_the_success_say_branch_too(self):
        payload = confirm.Verdict(
            approved=True,
            reason="approved",
            success_say="Lit it.",
            acted_session="watchtower",
        ).to_dict()
        assert payload["reason"] == "done"
        assert payload["acted_session"] == "watchtower"

    def test_a_sessionless_write_omits_acted_session(self):
        """Absent, not empty: an empty string retiring reminders keyed to a
        session named "" is nonsense, and the client keys on truthiness."""
        payload = confirm.Verdict(approved=True, reason="approved").to_dict()
        assert "acted_session" not in payload

    def test_no_refusal_carries_acted_session(self, convo, clock):
        """The priced false-accept, at the payload layer: a cancel or refusal
        retiring a reminder means the re-raise silently never happens. No
        non-approved payload may carry the key — including cancel."""
        outcomes = dict(self._outcomes(convo, clock))
        outcomes["cancelled"] = convo.spine.cancel(
            convo.announced_proposal(session="orchestrator").token
        )
        for label, verdict in outcomes.items():
            payload = verdict.to_dict()
            assert payload["success"] is False, label
            assert "acted_session" not in payload, label

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
        assert "can't tell whether it took effect" in verdict.spoken
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
        result = write_tools.WRITE_TOOL_FNS["propose_session_message"](
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
                write_tools.WRITE_TOOL_FNS["propose_session_message"](
                    {"session": bad, "message": "hello", "_buddy": "buddy"}, convo.spine
                )
        assert runner.calls == []

    def test_a_cold_fleet_refuses_instead_of_queueing_into_the_void(
        self, convo, runner, monkeypatch
    ):
        """Spec §5, and the refusal is words the buddy can say."""
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"something-else"})
        with pytest.raises(tools.ToolError, match="Nothing is listening"):
            write_tools.WRITE_TOOL_FNS["propose_session_message"](
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
        result = write_tools.WRITE_TOOL_FNS["propose_session_message"](
            {"session": "orchestrator", "message": "hello", "_buddy": "buddy"},
            convo.spine,
        )
        assert result["success"] is True

    def test_an_absurdly_long_instruction_is_refused(self, convo, live):
        with pytest.raises(tools.ToolError):
            write_tools.WRITE_TOOL_FNS["propose_session_message"](
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
        proposed = write_tools.WRITE_TOOL_FNS["propose_session_message"](
            {"session": "orchestrator", "message": "restart the portal", "_buddy": "buddy"},
            convo.spine,
        )
        proposal = next(
            p for p in convo.spine.pending() if p.id == proposed["proposal_id"]
        )
        convo.buddy_speaks(proposal)
        convo.approve(proposal)
        write_tools.WRITE_TOOL_FNS["send_session_message"](
            {"confirm_token": proposed["confirm_token"]}, convo.spine
        )
        argv = runner.calls[0]
        assert argv[:2] == ["msg", "send"]
        assert argv[2:8] == [
            "--to", "orchestrator", "--from", "buddy", "--kind", "voice",
        ]
        assert len(argv) == 9  # prefix + exactly one body

    def test_cancel_never_writes(self, convo, runner, live):
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        result = write_tools.WRITE_TOOL_FNS["cancel_session_message"](
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
        result = write_tools.WRITE_TOOL_FNS["propose_session_message"](
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


def _confirm_source() -> str:
    """``confirm.py`` with comment markers stripped, for prose assertions.

    A sentence written across several ``#`` lines is one sentence, and a
    comment beside the code is exactly where the stale claim lived. Stripping
    the markers lets an assertion match either home.
    """
    from pathlib import Path

    lines = [
        line.lstrip().removeprefix("#:").removeprefix("#").strip()
        for line in Path(confirm.__file__).read_text(encoding="utf-8").splitlines()
    ]
    return "\n".join(lines)


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

    def test_the_ordinary_approval_still_approves(self):
        assert confirm.classify("confirm tango", "tango") == confirm.APPROVED

    def test_say_preceded_without_the_approve_frame_still_approves(self):
        """Half the guard's condition is not the guard. An owner parroting
        the advice line says exactly this, and refusing it would loop them
        against advice that coaches those very words."""
        assert confirm.classify("say confirm tango", "tango") == confirm.APPROVED

    def test_approve_framed_without_say_preceding_still_approves(self):
        """The other half alone is not the guard either — "approve" in an
        utterance is ordinary speech, not the announcement frame."""
        assert confirm.classify(
            "I approve, confirm tango", "tango"
        ) == confirm.APPROVED

    def test_a_denial_outranks_the_quoted_frame(self):
        assert confirm.classify(
            "no — to approve say confirm tango", "tango"
        ) == confirm.DENIED

    def test_a_quoted_frame_for_another_nonce_is_not_this_outcome(self):
        """The frame quoting a DIFFERENT word is a different problem — the
        owner needs this proposal's code, so wrong_nonce's advice is right."""
        assert confirm.classify(
            "to approve say confirm violet", "tango"
        ) == confirm.WRONG_NONCE

    def test_a_hesitated_frame_is_still_the_frame(self):
        """The frame lookback skips fillers for the same reason the approval
        path does — the TTS echo is chunked by VAD and a disfluency can land
        between "say" and "confirm". Narrow as ever: both conditions still
        required."""
        assert confirm.classify(
            "to approve, say, uh, confirm tango", "tango"
        ) == confirm.QUOTED_FRAME
        assert confirm.classify("say, uh, confirm tango", "tango") == confirm.APPROVED

    def test_the_spine_speaks_the_accurate_reason_not_wrong_nonce(self, convo, runner):
        """The nit that mattered: this used to classify wrong_nonce, and the
        spoken line — the owner's ENTIRE diagnostic in a screenless channel —
        told them their code word was wrong when it was right, sending them
        to fix the one thing that was not broken. The string itself is
        pinned: it must affirm the word was right and coach the bare
        phrasing."""
        proposal = convo.announced_proposal()
        convo.says(f"to approve say confirm {proposal.nonce}")
        verdict = convo.spine.confirm(proposal.token)
        assert runner.calls == []
        assert verdict.approved is False
        assert verdict.reason == "quoted_frame"
        assert verdict.spoken == (
            "That sounded like my own announcement coming back, so I haven't "
            "sent anything. The word was right — just say confirm and the "
            "word, on its own."
        )

    def test_a_bare_approval_after_the_echo_still_approves(self, convo, runner):
        """The recovery the spoken advice coaches must actually work: echo
        lands, owner says the bare phrase, the write goes."""
        proposal = convo.announced_proposal()
        convo.says(f"to approve say confirm {proposal.nonce}")
        convo.says(f"confirm {proposal.nonce}")
        verdict = convo.spine.confirm(proposal.token)
        assert verdict.approved is True
        assert len(runner.calls) == 1


class TestTheFallbackEchoCannotApprove:
    """Issue #950 defect 4: the confirm-gate bypass.

    ``speechSynthesis`` output is NOT on the WebRTC audio path, so the
    browser's echo cancellation does not suppress it — the fallback's own
    audio re-enters the microphone and lands in the USER transcript inside
    the valid approval window. Nothing in the gate distinguishes an echoed
    utterance from a spoken one, so whatever the fallback channel utters is
    a string the gate may be fed verbatim.

    The property under test: **no fragment of what the fallback channel
    speaks can approve the proposal it announces.** Fragments, not just the
    whole text — the echo is lossy and adversarially timed (VAD chunks on
    pauses, the model's overlapping voice masks parts of it, the cascade
    garbles others), so the guard must hold for every piece individually.
    Testing only the full echo would pass by luck: the full directive
    happens to contain "do not", which the denial grammar catches, while
    the one chunk that matters — "to approve, say confirm <nonce>" —
    approves cleanly on its own.
    """

    def _mint(self, ring_cls=transcript.TranscriptRing):
        ring = ring_cls()
        runner = RecordingRunner()
        spine = confirm.ConfirmSpine(ring, wait_s=0.0, runner=runner)
        convo = Conversation(ring, spine)
        result = write_tools.WRITE_TOOL_FNS["propose_session_message"](
            {"session": "orchestrator", "message": "restart the portal",
             "_buddy": "buddy"},
            spine,
        )
        # Anchor: the proposal was spoken (by whichever voice), so the echo
        # lands squarely inside the valid approval window.
        spine.announce(result["proposal_id"], convo._next())
        return result, convo, runner

    @staticmethod
    def _fallback_speech(result):
        # Exactly what the client's fallback channel utters: the announcer
        # speaks the dedicated fallback text when the payload carries one,
        # else the say text — mirroring `speak(item.fallbackText || item.text)`.
        return result.get("fallback_say") or result["say"]

    def _chunks(self, result):
        spoken = self._fallback_speech(result)
        pieces = [c.strip() for c in re.split(r"[.;:—,]", spoken) if c.strip()]
        return [spoken, *pieces]

    def test_no_fragment_of_the_fallback_output_approves(self, monkeypatch):
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})
        probe, _, _ = self._mint()
        for index in range(len(self._chunks(probe))):
            result, convo, runner = self._mint()
            echoed = self._chunks(result)[index]
            convo.says(echoed)
            verdict = write_tools.WRITE_TOOL_FNS["send_session_message"](
                {"confirm_token": result["confirm_token"]}, convo.spine
            )
            assert runner.calls == [], (
                f"echoed fragment {echoed!r} WROTE with no human speaking"
            )
            assert verdict["success"] is False, echoed

    def test_the_fallback_channel_never_carries_the_nonce(self, monkeypatch):
        """The structural fix: the nonce must not be reachable from the
        un-echo-cancelled channel at all. A guard downstream is defence in
        depth; this is the property that makes the echo harmless."""
        monkeypatch.setattr(inbox, "live_sessions", lambda: {"orchestrator"})
        result, _, _ = self._mint()
        nonce = result["confirm_phrase"].split()[1]
        assert nonce not in confirm.normalize(self._fallback_speech(result))


#: Every ``SPOKEN`` line that would deny if it came back through the mic.
#: DERIVED from the map, never typed: a new refusal line carrying a denial
#: trigger is covered the day it is written, and a rewording that removes the
#: last one fails the control test below rather than silently emptying the
#: parametrization.
SPOKEN_DENYING_LINES = [
    line for line in confirm.SPOKEN.values() if confirm.carries_denial(line)
]


def _is_run_of(utterance: str, line: str) -> bool:
    """Is *utterance* a contiguous token run of *line*? Independently spelled.

    Deliberately NOT ``confirm._contains_run``: this is the fixture's own check
    that a clipped-barge-in case is really the shape it claims to be, and using
    the function under test to validate the input makes the two fail together.
    """
    haystack = confirm.normalize(line).split()
    needle = confirm.normalize(utterance).split()
    joined = " ".join(haystack)
    return f" {' '.join(needle)} " in f" {joined} "


class TestTheBuddysOwnVoiceCannotDeny:
    """#992 — the echo's OTHER direction, and the one nothing covered.

    Approval by echo is closed structurally: the fallback channel never carries
    a nonce (#953), which is what the class above proves. ``carries_denial`` has
    no such gate, and several ``SPOKEN`` lines contain denial triggers — "I
    heard you hold off…", "Hang on — I'm already working on that one", "I don't
    have anything pending…". Echoed inside the approval→confirm window, one of
    those retroactively DENIES the owner's own approval and reports a take-back
    they never spoke, invisibly.

    **The rule chosen is CONTENT, not timing, and that is the decision.** The
    obvious rule — "utterances transcribed while the fallback voice is speaking
    are not denials" — is unusable: barge-in over the robot voice is the normal
    way to retract in this channel, so it drops genuine take-backs and the
    write goes out. That is the acting-twice direction, strictly worse than the
    wrongful refusal it fixes, which costs one re-spoken approval. A content
    rule cannot fire on words the buddy never said, so it has no such half.
    """

    def test_the_map_really_does_contain_denial_carrying_lines(self):
        """The must-fail control for the whole class: if this list is ever
        empty, the parametrized test below passes vacuously."""
        assert SPOKEN_DENYING_LINES, (
            "SPOKEN has no denying line — the echo tests here prove nothing"
        )
        assert confirm.carries_denial(confirm.SPOKEN["denied"])

    @pytest.mark.parametrize("echo", SPOKEN_DENYING_LINES)
    def test_an_echoed_refusal_line_does_not_veto_the_approval(
        self, echo, convo, runner
    ):
        """The whole line, echoed after the approval, must not deny."""
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        convo.says(echo)  # the browser voice, back through the mic

        assert confirm.carries_denial(echo), echo
        assert convo.spine.confirm(proposal.token).approved is True, echo
        assert len(runner.calls) == 1

    def test_a_real_take_back_over_the_robot_voice_still_denies(
        self, convo, runner
    ):
        """The false-reject half, and the reason the timing rule was refused.

        Every one of these is something an owner actually says while the buddy
        is mid-sentence. A rule keyed on "was the fallback speaking" drops all
        of them; the content rule keeps every one, because none is six
        contiguous tokens of a line the buddy said.
        """
        for take_back in (
            "no",
            "hold off",
            "hang on",
            "no wait, don't send it",
            "hold off — I heard you but hold off",
            "stop",
        ):
            proposal = convo.announced_proposal()
            convo.approve(proposal)
            convo.says(take_back)
            verdict = convo.spine.confirm(proposal.token)
            assert verdict.reason == "denied", take_back
        assert runner.calls == []

    def test_a_barge_in_captured_WITH_the_echo_still_denies(self, convo, runner):
        """The whole utterance must be the echo, not merely contain one.

        The realistic capture of a barge-in is the tail of the buddy's line and
        the owner's words in one transcript. That is not a contiguous run of any
        line, so it denies — the enumeration fails CLOSED.
        """
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        convo.says("hang on im already working on that one no stop")
        assert convo.spine.confirm(proposal.token).reason == "denied"
        assert runner.calls == []

    #: Real barge-ins whose transcript is a CLIPPED prefix of a `SPOKEN` line —
    #: the shape the token floor exists to keep on the denying side. Each is 3
    #: to 5 tokens, which is exactly the range the module's own docstring says
    #: would be suppressed at a lower floor.
    CLIPPED_BARGE_INS = (
        "hang on im",            # 3 tokens
        "hang on i havent",      # 4
        "i heard you hold off",  # 5
    )

    @pytest.mark.parametrize("clipped", CLIPPED_BARGE_INS)
    def test_a_clipped_barge_in_below_the_floor_still_denies(
        self, clipped, convo, runner
    ):
        """The floor is the entire false-reject budget of this rule, and only
        the two-token case was pinned.

        At a floor of 3, "hang on im" — a real retraction the transcriber
        clipped — is read as the buddy's own voice, the denial is dropped and
        THE WRITE GOES OUT. That is the acting-twice direction. Each of these
        IS a contiguous run of a `SPOKEN` line, so nothing but the floor keeps
        it denying.
        """
        assert confirm.carries_denial(clipped), clipped
        assert any(
            _is_run_of(clipped, line) for line in confirm.SPOKEN.values()
        ), f"{clipped!r} must be a real run of a SPOKEN line or it proves nothing"

        proposal = convo.announced_proposal()
        convo.approve(proposal)
        convo.says(clipped)
        assert convo.spine.confirm(proposal.token).reason == "denied", clipped
        assert runner.calls == []

    def test_the_floor_exceeds_every_clipped_barge_in_we_priced(self):
        """Stated as arithmetic, so lowering the constant fails here too rather
        than only in the scenario above."""
        longest = max(
            len(confirm.normalize(c).split()) for c in self.CLIPPED_BARGE_INS
        )
        assert confirm._ECHO_MIN_TOKENS > longest

    def test_an_utterance_that_CONTAINS_a_whole_line_is_not_an_echo(self):
        """"Whole-utterance" is named load-bearing in the module and the wiki,
        and containment has a direction.

        Reversed — "does the utterance contain a line" — an owner who talks
        over the tail of the buddy's sentence and is captured with all of it is
        classified as an echo, and their retraction is dropped.
        """
        line = confirm.SPOKEN["denied"]
        assert confirm.is_buddy_echo(line) is True, "the line itself is an echo"
        assert confirm.is_buddy_echo(line + " no, stop, don't send it") is False

    def test_a_NON_CONTIGUOUS_subset_of_a_line_is_not_an_echo(self):
        """"Contiguous" is the other named property, and a subset test passes
        every assertion above while accepting word salad.

        These are the line's own words, in the line's own order, with words
        dropped — which is what a human speaking loosely on the same subject
        looks like, and is not the machine's sentence.
        """
        tokens = confirm.normalize(confirm.SPOKEN["denied"]).split()
        gappy = " ".join(tokens[::2])
        assert len(gappy.split()) >= confirm._ECHO_MIN_TOKENS, "must clear the floor"
        assert set(gappy.split()) <= set(tokens), "must be a subset of the line"
        assert confirm.is_buddy_echo(gappy) is False

    def test_a_short_garbled_echo_fails_closed(self, convo, runner):
        """Below the token floor it is treated as a denial: a wrongful refusal
        costs one re-spoken approval, and nothing about being incomplete here
        can make a write happen."""
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        convo.says("hold off")  # three words of the `denied` line
        assert convo.spine.confirm(proposal.token).reason == "denied"
        assert runner.calls == []

    def test_the_echo_rule_never_makes_something_approvable(self):
        """Suppression is only ever subtractive. No ``SPOKEN`` line carries a
        nonce, so skipping one can never turn an echo into an approval."""
        for line in confirm.SPOKEN.values():
            for nonce in confirm.NONCE_WORDS:
                assert confirm.classify(line, nonce) != confirm.APPROVED, line

    def test_the_rule_is_applied_at_both_scan_sites(self, convo, runner):
        """The judge's OWN loop, not the post-approval scan.

        ``classify`` returns ``NO_MATCH`` before it ever consults the denial
        grammar unless the utterance contains a confirm word — so most echoed
        lines exercise only the scan, and a test built on one of those passes
        with the loop guard deleted. The ``no_proposal`` line is the one that
        reaches the loop's ``DENIED`` branch: it says "nothing to **confirm**"
        and it says "**don't**". Echoed after the approval it is the newest
        entry, so newest-first reaches it FIRST and returns ``denied`` before
        the scan below is ever run.
        """
        echo = confirm.SPOKEN["no_proposal"]
        assert confirm.classify(echo, "tango") == confirm.DENIED, (
            "this echo must reach the judge loop's DENIED branch, or the test "
            "proves only what the post-approval scan does"
        )
        proposal = convo.announced_proposal()
        convo.approve(proposal)
        convo.says(echo)
        assert convo.ring.snapshot()[-1].text == echo, "echo must be newest"
        assert convo.spine.confirm(proposal.token).approved is True
        assert len(runner.calls) == 1


class TestHonestLimit:
    #: Every clause of §3.7. Each is a separate claim and each can be lost
    #: independently by a well-meaning edit, so each is asserted separately.
    CLAUSES = (
        "mis-transcription",
        "against an approval the conversational model invented",
        "does **not** cover every mis-transcription",
        "narrows but does not eliminate",
        "A spoken retraction is caught only when it uses a word or phrase the "
        "grammar knows",
        # The fourth caveat. confirm.py's own docstring claims "the wiki and the
        # docstring both carry" this paragraph — which was false for the whole
        # time the wiki carried three clauses and the module carried four (#981).
        # Listing it here is what makes that sentence true rather than aspirational.
        "evidence of what was **heard**, not proof of what was **said**",
        "as trustworthy as the local browser page",
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


class TestTheNonceNeverLeavesTheGate:
    """Issue #953: the approving utterance is ``confirm <nonce>`` by
    construction, so putting it in the delivered body ships the nonce to the
    recipient's scrollback on EVERY approved write — and carries none of the
    verification §4b built the slot for. The slot now carries the owner's
    REQUEST utterance, captured at propose time, which a recipient genuinely
    can check the paraphrase against.
    """

    def test_the_delivered_body_never_contains_the_nonce(self, convo, runner):
        convo.says("tell the orchestrator to restart the portal")
        proposal = convo.announced_proposal(instruction="restart the portal")
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True
        body = runner.calls[0][-1]
        assert proposal.nonce not in body
        assert "confirm" not in body

    def test_the_slot_carries_the_request_utterance_verbatim(self, convo, runner):
        """§4b's actual intent: content a recipient can check the paraphrase
        against — the transcription model's words, not the buddy's."""
        spoken = "hey, tell the orchestrator to restart the portal"
        convo.says(spoken)
        proposal = convo.announced_proposal(instruction="restart the portal")
        convo.approve(proposal)
        convo.spine.confirm(proposal.token)
        assert f'said: "{spoken}"' in runner.calls[0][-1]

    def test_with_no_request_utterance_the_slot_is_gone_not_empty(self):
        """A slot whose expected content is empty must not survive (#953
        acceptance). Marker, instruction and id still deliver."""
        body = confirm.render_body("restart the portal", "", "a1b2c3")
        assert "said:" not in body
        assert body.startswith("restart the portal")
        assert body.endswith("#a1b2c3")

    def test_a_stale_confirm_phrase_is_never_selected_as_the_request(
        self, convo, runner
    ):
        """The one path a nonce could re-enter: a prior proposal's approval or
        wrong-nonce utterance sitting newest in the ring at propose time. That
        is not a request, so selection skips it — falling back to the real
        request sentence, false-reject half covered by the fallback below."""
        convo.says("tell the orchestrator to restart the portal")
        convo.says("confirm walrus")
        proposal = convo.announced_proposal(instruction="restart the portal")
        convo.approve(proposal)
        convo.spine.confirm(proposal.token)
        body = runner.calls[0][-1]
        assert "walrus" not in body
        assert 'said: "tell the orchestrator to restart the portal"' in body

    def test_an_empty_ring_at_propose_still_delivers(self, convo, runner):
        """The false-reject half: a missing request utterance must not block
        or garble the write — the slot is simply absent."""
        proposal = convo.announced_proposal(instruction="restart the portal")
        convo.approve(proposal)
        assert convo.spine.confirm(proposal.token).approved is True
        body = runner.calls[0][-1]
        assert "said:" not in body
        assert proposal.nonce not in body
