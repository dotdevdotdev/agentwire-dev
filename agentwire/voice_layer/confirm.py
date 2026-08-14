"""The confirm spine — propose/confirm below the model (spike).

    This defends against **mis-transcription and against an approval the
    conversational model invented**, which is the stated threat. It does **not**
    cover every mis-transcription — a transcriber hallucination or an
    approval-shaped utterance meant for someone else is a real residual risk
    that the nonce narrows but does not eliminate. **A spoken retraction is caught
    only when it uses a word or phrase the grammar knows** — "let's not", "on
    second thought" and "I changed my mind" are not caught, and no word list
    reaches them. **A passed gate means the message was queued, not delivered,
    and not acted on.** The ``said:`` clause
    is evidence of what was **heard**, not proof of what was **said** — it is
    exactly as trustworthy as the local browser page, which holds the bridge
    token and can POST to ``/utterance``. It is **not** a security boundary
    against an adversary.

That paragraph is the guarantee, in full. **Widen it if you learn more; never
narrow it.** Anyone holding the microphone can approve anything the buddy
proposes. Do not paraphrase any of this as "the confirm gate protects writes" —
a guarantee that gets rounded up in the retelling is how an operator-facing
claim starts lying.

The "queued, not delivered" clause lives *here*, in the paragraph the wiki and
the docstring both carry, rather than only next to the spoken wording. This is
what a future reader quotes when they ask what the gate guarantees, and without
that clause they conclude "gate passed, so the write happened". It did not:
``msg send`` queues, and delivery is at the recipient's next safe boundary.

The ``said:`` clause caveat is there because §4b's entire purpose is that the
verbatim REQUEST utterance (captured at propose — never the approving one,
which is a nonce and stays inside the gate, #953) is evidence a recipient can
CHECK the paraphrase against, and a
recipient reading ``said:`` will treat it as what the human said. Anything
resident in the bridge's browser page holds the per-run bearer token and can
POST arbitrary text to ``/utterance``, so the evidence property is weaker than
a reader would otherwise assume.

The retraction clause is a stated residual rather than a to-do. Chasing "let's
not" / "on second thought" / "I changed my mind" is how this becomes the
unbounded denylist the filler list already taught us to reject — the phrasings
are open-ended and the list would never be done. What bounds the damage instead
is that a retraction the grammar misses does NOT approve anything by itself: the
write still needs the nonce, so the owner can simply not say it. The residual is
"you said something meaning stop AND then said the nonce anyway", which is a
narrower and much stranger thing to do than the clause's plain reading suggests.

Rationale, deliberately kept OUT of the quotable sentence above — stacking
mitigations into an honest limit is how it gets rounded back up: the residual is
small, because that field reaches only the attribution clause. ``--to``,
``--from``, ``--kind`` and the instruction are all frozen at propose, so the
worst available consequence is falsified *evidence*, never a redirected write.

The split, and why it is two halves
-----------------------------------

**(a) Proposal binding, below the model.** :meth:`ConfirmSpine.confirm` accepts
one argument: a token minted on a PRIOR turn by :meth:`ConfirmSpine.propose`.
The argv is frozen at propose time (:class:`Proposal`) and TTL-bounded. Nothing
between propose and confirm changes *what* runs, only *whether* it runs.

**Single-use means consumed on SUCCESS, not on attempt**, and that is
load-bearing rather than a detail. If a refused attempt burned the token, the
``pending_transcript`` refusal below would tell the owner to wait when waiting
cannot work — the spoken reason becomes a lie, and the owner is told to do the
one thing that cannot help. Refused attempts are rate-limited
(:data:`MAX_CONFIRM_ATTEMPTS`) instead.

**(b) The approval judgment, also below the model.** DocumentScribe leaves this
100% in the model: their anti-filler rule is a paragraph asking the model to be
strict (``voice/instructions.ts`` lines 42-46), their own comment says "there's
no code-level pattern match on 'yes'", and the stated fallback is "they tap the
card" — a click surface a voice-only user cannot reach (#748). The part we were
told to copy most carefully is the part that never worked hands-free.

Why the approval is a NONCE and not an approval grammar
--------------------------------------------------------

The first design here gated on "an utterance matching an approval grammar and
missing a filler denylist", and claimed that made two models fail the same way.
**That claim was false, and the mechanism was weaker than it looked.** The two
checks are not independent: both models consume the same audio. Three breaks,
none of which need the conversational model to fail at all:

- **Transcriber hallucination.** ``gpt-4o-mini-transcribe`` is Whisper-lineage,
  and confident short outputs on near-silence — "Okay.", "Yeah.", "Thank you.",
  "Yes." — are that family's best-documented failure. Three of those four were
  in the original filler denylist, which is the tell: **the denylist was
  enumerating a hallucination prior.** An unbounded denylist is not a
  mechanism, it is a list of the failures you have thought of so far — the same
  objection (b) raises against DocumentScribe's paragraph, one level down.
- **An approval-shaped utterance meant for someone else** — "yeah, that's
  right, anyway" to a person in the room. ``semantic_vad`` commits it.
- **One approval, two proposals.** The old condition was existential ("there is
  an utterance that postdates the proposal"), so one "yes" satisfied both P1 and
  P2. §4 names that exact failure: acting twice.

So the approval is a **spoken nonce**. The buddy speaks it in the proposal
("say **confirm tango** to approve"), and the grammar is ``confirm <nonce>``.
That **narrows** the first two — a nonce is not in a transcriber's prior, and
nobody says "confirm tango" incidentally — and **closes** the third, because the
nonce binds the utterance to one proposal (given uniqueness among live ones,
which :meth:`ConfirmSpine.propose` enforces). It also makes the filler denylist
redundant, which is the right shape. Cost: two words instead of one, still
hands-free, so T5 holds.

"Narrows", not "kills": a transcriber can still hallucinate, and a nonce word
can still appear in speech meant for someone else. Claiming otherwise would
contradict the honest limit above two paragraphs later.

**The false-REJECT half is priced too, and it is the half that bites.** A nonce
the transcriber renders inconsistently makes a CORRECT approval fail every
time, and the taxonomy then tells the owner to say it again — so they repeat and
fail identically. That is a livelock, and it is worse than the false-accept the
strictness was buying. Hence :data:`NONCE_WORDS` (one TRANSCRIBER RENDERING
each — which is a stronger claim than one spelling, and the difference cost two
of the original twenty words) and containment rather than whole-utterance
matching, with disfluencies skipped between the confirm word and the nonce.
See :func:`classify`.

The nonce carries a second property worth naming: **the owner cannot say a
nonce they have not heard**, which independently covers most of the barge-in
hazard that :attr:`Proposal.anchor_seq` exists for. The anchor is kept anyway —
two independent barriers, not one — but that is why the anchor is defence in
depth rather than the only thing standing between a barge-in and a write.

Bounded await, and three outcomes
---------------------------------

Fail-closed is right; fail-closed *immediately* is not. The conversational model
starts generating as soon as VAD commits the turn, while transcription is a
separate pass over the same buffer — so for a short utterance the confirm
plausibly beats its own transcript a large fraction of the time. Refusing
instantly would make every confirm cost two utterances, and worse: the first
approval then sits stale in the ring, so if the owner says "no, wait" and the
model retries, the gate finds the original approval and **writes after the owner
said no**. Fail-closed plus retry manufactures the window.

Hence: a bounded await on the ring's condition variable
(:data:`APPROVAL_WAIT_S`), and **three outcomes, not two** — ``approved`` /
``refused`` (a transcript arrived and did not match) / ``pending_transcript``
(the await timed out). The last two demand OPPOSITE behaviour from the owner
("say it again" vs "wait"), so collapsing them trains the owner to repeat into a
system that needed them to hold still.

The residual stale-approval window is closed from the other side too: a matched
utterance is SPENT (:meth:`TranscriptRing.spend`), and any denial committed
after the approval refuses the write.

Every refusal must SPEAK — and this module cannot achieve that alone
---------------------------------------------------------------------

Silence is the one unacceptable failure mode: the owner is not looking at a
screen, so a refusal they cannot hear is indistinguishable from not having been
heard, and they simply repeat themselves.

**Returning a reason does not achieve this.** A ``function_call_output`` is
context; the model then says whatever it says. Refusing to leave the *judgment*
in the model and then leaving the *announcement* in it is the same defect one
level up. So this module's job ends at producing a distinct, actionable
:attr:`Verdict.spoken` per outcome — and ``client.py`` owns the mechanism that
makes it reach the ear (cancel the in-flight response, scripted
``response.create``, verify against the following ``response.done``,
``speechSynthesis`` fallback). Neither half is sufficient alone.

No damage-control backstop on the sending — but the acting IS guarded
----------------------------------------------------------------------

Empirically confirmed (see the probe in ``tools/voice_dc_probe.py`` and the PR
body): the Bash-tool path is hooked and over-blocks on prose (#915), and the
bridge's ``subprocess.run`` path is not hooked at all. ``msg_send``'s MCP tool
is not in the matcher list either, so **no programmatic path to ``msg send`` has
damage-control coverage**.

The precise statement, and it has to be this precise because every shorter
version rounds up:

    **The sending is unguarded. The acting-on-it inherits the recipient's
    ordinary guards — which are guards on the OPERATION, not on WHO ASKED.**

"The acting-on-it is guarded" overstates twice. On coverage: the recipient's
hooks cover ``Bash``/``Edit``/``Write``/``Read``/``Grep``/``Glob`` and two MCP
tools, so a recipient acting through any other ``mcp__agentwire__*`` tool
(``session_send``, ``pane_spawn``, ``msg_send``, ``worktree_*``) is not guarded
at all. And on kind, which matters more: **damage control cannot tell "the human
asked" from "the buddy asked" from "a mis-transcription asked."** It is not a
guard on the buddy's authority in any sense — the recipient is exactly as
guarded as it was before, and the buddy has added a new way to ask it things.

What actually constrains the buddy is the frozen argv and this gate. That is
worth stating plainly rather than borrowing reassurance from the recipient's
hooks.
"""

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import outbox, relay
from .transcript import TranscriptRing, Utterance

#: How long a minted proposal stays confirmable, from the moment the buddy
#: finished speaking it. Long enough to answer, short enough that an approval
#: for something said minutes ago cannot land on it.
PROPOSAL_TTL_S = 120.0

#: How long ``confirm`` blocks waiting for the transcription model to catch up
#: (§3.3). Long enough to absorb the ordinary transcript lag for a short
#: utterance, short enough that the conversation does not read as dead — tool
#: dispatch is sequential in the client, so this stalls the turn while it waits.
APPROVAL_WAIT_S = 2.5

#: Refused confirms tolerated per proposal before it is discarded. Refusals do
#: NOT consume the token (see the module docstring), so something has to bound
#: a model that keeps guessing.
MAX_CONFIRM_ATTEMPTS = 5

#: The nonce alphabet: short, phonetically distinct WORDS with one spelling each.
#:
#: **Digits were tried and they livelock.** "four seven" comes back from the
#: transcriber as ``47``, ``four seven``, ``4-7`` or ``forty-seven`` — the least
#: stable token type there is. Pairing that with an exact matcher makes a
#: CORRECT approval fail deterministically, and under the taxonomy it fails as
#: "that wasn't the phrase, say it again", so the owner repeats and fails
#: identically. That is a livelock, and it is a worse outcome than the
#: false-accept the strictness was buying: the gate exists to be usable
#: hands-free.
#:
#: These are one-word, unambiguously spelled, and mutually distinct under
#: ordinary mis-hearing. Chosen for how they SOUND, not for how they look.
#:
#: **"One spelling each" is a claim about the TRANSCRIBER, not about the
#: orthography**, and two of the original twenty failed it in the same way the
#: digits did — not by mis-firing, but by livelocking:
#:
#: - ``harbor`` — a Whisper-lineage model emits en-GB ``harbour`` freely.
#: - ``ripcord`` — a compound, and ``rip cord`` is an ordinary segmentation.
#:
#: Neither variant is in this tuple, so the outcome is not even
#: ``wrong_nonce``: it is ``no_match``, whose spoken advice is "say confirm and
#: then the word I gave you". The owner repeats the identical utterance, fails
#: identically, and the proposal retires at :data:`MAX_CONFIRM_ATTEMPTS`. That
#: is the digit failure exactly, reached through spelling rather than digits.
#:
#: They are REMOVED rather than aliased. A variant-folding map beside
#: :data:`_NUMBER_WORDS` would fail only in the safe direction, but it can only
#: fold token-for-token — ``rip cord`` is two tokens and needs a compound-merge
#: pass — and folding a spelling for a word we no longer mint is machinery with
#: nothing to do. The selection rule replaces both: **one morpheme, no
#: en-US/en-GB split.**
#:
#: **The base vocabulary is the NATO phonetic alphabet (#1039)** — designed for
#: exactly this job and familiar to the owner — filtered by the admission test
#: above, MEASURED against the real transcriber (``gpt-4o-mini-transcribe``)
#: rather than assumed. ``tools/nonce_stt_probe.py`` synthesizes each candidate
#: inside the real phrase ("confirm <word>") at three macOS voices (en-US,
#: en-GB, en-AU) and admits a word only when every transcription across three
#: runs renders it as exactly its canonical token. Measured 2026-08-13; the
#: failures are the tuple's negative space and each is the harbor/harbour or
#: digit failure reached through a different door:
#:
#: - ``alfa``/``alpha`` — BOTH spellings failed: rendered ``alpha`` or ``elva``.
#: - ``juliett`` — the ICAO spelling never comes back; the transcriber emits
#:   ``juliet``, which is the spelling admitted instead.
#: - ``x-ray`` — segments as ``x ray``, both spellings, every run.
#: - ``mike`` — rendered ``mic`` and ``my``. A homophone of a word this channel
#:   is ABOUT is the worst candidate the alphabet contained.
#: - ``papa`` — rendered ``popup``/``pop up``; one run also glued the phrase
#:   into ``confirme papa``.
#: - ``sierra`` — one run heard "confirms the error", i.e. the PHRASE itself
#:   dissolved, not just the word.
#: - ``foxtrot`` — rendered ``vostrot`` in one run of nine samples; unstable.
#: - ``quebec`` — rendered ``québec`` (diacritic) in one run; ``normalize``
#:   strips no diacritics, so that is a second spelling.
#: - ``whiskey`` — measured stable across nine samples, but excluded by the
#:   standing selection rule: ``whisky`` is a live en-GB/spirits split, the
#:   harbor/harbour shape with a thinner measurement than the rule demands.
#:
#: Re-run the probe before adding a word; a word failing ANY run stays out.
NONCE_WORDS = (
    "bravo", "charlie", "delta", "echo", "golf", "hotel", "india", "juliet",
    "kilo", "lima", "november", "oscar", "romeo", "tango", "uniform",
    "victor", "yankee", "zulu",
)

#: Digit spellings, kept for normalization only. Nothing MINTS a digit nonce;
#: this exists so that if one ever reaches the alphabet, "7" and "seven" match
#: rather than livelocking — the false-reject half, priced.
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "zero": "0",
    "oh": "0",
}

#: Denial grammar (§3.1). Two tiers, and the split is the fix for a defect that
#: INVERTED the whole gate: "don't confirm juniper" APPROVED the write.
#:
#: **The root cause was normalization, not the word list.** ``_PUNCT_RE``
#: replaced punctuation with a SPACE, so ``normalize("don't")`` produced
#: ``"don t"`` and the ``dont`` alternative could never fire on real transcriber
#: output. ``donot`` and ``nevermind`` were dead the same way — speech
#: transcribes as "do not" and "never mind". Three carefully written entries
#: with no reachable path.
#:
#: **The test lesson, which is the transferable part:** a reachability test over
#: this table PASSES, because every alternative matches when fed to itself. What
#: failed is that normalization never PRODUCED those tokens. Testing a table's
#: entries against themselves proves the table, not the path into it — so the
#: tests for this drive the REAL pipeline (raw utterance → normalize → classify)
#: and never the matcher in isolation.
#:
#: Single words that always signal retraction in reply to "say confirm <word>".
#: Deliberately excludes ``not``/``never``/``hold``/``forget``, which are among
#: the commonest words in English and turned "confirm tango, it is not urgent"
#: into "You said no". Those are recovered as ORDERED BIGRAMS below.
_DENIAL_WORDS = frozenset(
    {"no", "nope", "dont", "stop", "cancel", "wait", "nevermind", "abort",
     "scratch", "undo"}
)

#: Disfluencies that may be interleaved anywhere inside a retraction phrase.
#:
#: "hold, uh, on" and "do, um, not" are how people ACTUALLY speak at the exact
#: moment this grammar has to work — a filler mid-retraction is the sound of
#: someone changing their mind. Matching adjacent tokens only let both APPROVE
#: the write: the same inversion as the apostrophe defect, arriving through
#: disfluency instead of punctuation.
#:
#: Enumerating these is safe under the closed-phrase rule for a reason worth
#: stating: skipping a filler can only ever make a denial EASIER to match, never
#: harder. An unlisted filler fails CLOSED — the phrase simply does not match and
#: the utterance denies on its own or is refused as not-the-nonce. This is an
#: enumeration on the safe side.
_FILLERS = frozenset({"uh", "um", "er", "erm", "ah", "hmm", "like", "you", "know"})


#: Ordered pairs. Order is the whole point: **"hold on" denies, "on hold" does
#: not** — which is the precise instrument for "confirm tango, the worker is on
#: hold", a measured false positive from the previous round. Bare-word matching
#: cannot express that distinction, which is why dropping the bare words was
#: right and dropping the retractions with them was not.
#: **Every entry is audited against the closed-phrase test**, and three failed
#: it — each measured DENYING a real approval:
#:
#: - ``("not", "that")`` — "it is not that urgent" DENIED while "it is not
#:   urgent" approved. It flipped on one added word, regressing the exact
#:   false-positive class the bare-word tightening fixed. "not that" is not a
#:   closed phrase, it is a fragment of open-ended speech.
#: - ``("back", "off")`` — "back off the throttle after" DENIED. An
#:   instruction, not a retraction.
#: - bare ``cancelled``/``canceled``, removed from the word list above — "the
#:   other task cancelled" DENIED. Ordinary past tense ABOUT SOMETHING ELSE.
#:
#: ``("scrap", "that")`` and ``("hold", "off")`` are added: closed retraction
#: phrases one word from entries already here, so they were plain misses and
#: cost nothing.
_DENIAL_BIGRAMS = frozenset(
    {("do", "not"), ("never", "mind"), ("hold", "on"), ("hang", "on"),
     ("hold", "off"), ("forget", "it"), ("forget", "that"),
     ("scrap", "that"), ("belay", "that")}
)

#: Words tolerated BETWEEN the two halves of a gapped bigram.
#:
#: **"Adjacent" was already a fiction and that is what made this a blocker.**
#: ``_denial_tokens`` strips ``_FILLERS`` before matching, so every entry in
#: this grammar has always been "adjacent modulo a skip set" — which is why
#: "never, uh, confirm tango" denied while "never ever confirm tango"
#: APPROVED. Shipping the pair as strictly adjacent stated a rule the matcher
#: did not implement, and the gap it left was the commonest intensifier in the
#: language. Also measured: really / once / actually / seriously, and
#: ``carries_denial("never ever confirm that")`` was False, so the
#: post-approval scan was blind to it too and the write EXECUTED.
#:
#: Closed class on purpose: degree adverbs, nothing else. An open gap ("any
#: word between never and confirm") would deny "I would never send that
#: without checking — confirm tango", which is an approval.
#:
#: **This enumeration sits on the FAIL-OPEN side, and unlike ``_FILLERS`` it
#: cannot be moved off it.** An unlisted gap word ends the run and the
#: utterance approves — "never absolutely confirm tango" would approve if
#: ``absolutely`` were missing. What bounds it is that the class is closed and
#: small; what does not bound it is anything structural. That is the honest
#: shape of this entry, stated rather than implied.
_NEVER_GAP_WORDS = frozenset(
    {"ever", "once", "again", "really", "actually", "seriously", "truly",
     "honestly", "literally", "absolutely", "definitely", "certainly",
     "just", "simply", "please"}
)

#: Ordered pairs matched with a bounded, closed gap between them.
#:
#: ``("never", "confirm")`` closes the one place the "a missed retraction is
#: safe" fallback does not hold. ``never`` is kept OUT of the word list above
#: for good reason — it is among the commonest words in English — and the
#: general argument for tolerating that is "the write still needs a nonce, so
#: the owner can simply not say it". **That argument fails on this exact
#: utterance**: "never confirm tango" IS the retraction and it CONTAINS the
#: nonce, so the fallback the exclusion leans on is the very thing being
#: spoken. Measured before the fix: APPROVED, along with "you should never
#: confirm tango".
#:
#: Safe by the closed-phrase rule rather than by intuition: no genuine approval
#: places those two tokens in that order separated only by a degree adverb.
#: ``never`` anywhere else stays the ordinary word it was excluded for being —
#: "confirm tango, I never got the other one" still approves.
#:
#: ONE rule, one place: the pair is deliberately NOT also in
#: :data:`_DENIAL_BIGRAMS`. Two spellings of one rule drift apart, and the
#: zero-gap case is just this rule with an empty run.
#:
#: **The second half is ``confirm`` alone, not :data:`_CONFIRM_WORDS`, and that
#: is deliberate** — a reader will otherwise assume the pair tracks that tuple.
#: "never confirmed tango" approves, and should: the past tense is a STATEMENT
#: about what happened ("I never confirmed tango, did I?"), not an imperative
#: retraction. Only the bare imperative retracts, which is the same
#: exact-token reasoning that keeps ``waiting``/``waited`` out of the ``wait``
#: rule. Adding ``confirmed`` here would buy no retraction anyone speaks and
#: would deny ordinary speech about a past approval.
_GAPPED_DENIAL_BIGRAMS = {("never", "confirm"): _NEVER_GAP_WORDS}

#: Pairs that SUPPRESS a single-word denial.
#:
#: **Exceptions carry a HIGHER bar than denial words, and the asymmetry is the
#: opposite of the one that governs the word list.** For denial WORDS, prefer
#: tight: a missed denial is recoverable, because the write still needs a nonce
#: and the owner can simply not say it. That reasoning does NOT transfer here.
#: An exception SUPPRESSES a denial, so a wrong one means **the owner said no
#: and the write went** — not recoverable by declining to speak, because they
#: already spoke and it did not count. Same failure as the normalization
#: inversion, through a narrower door.
#:
#: So: for the word list, prefer tight; for exceptions, **prefer few**.
#:
#: Unconditional, and safe for a STRUCTURAL reason rather than a semantic one.
#:
#: The intuition is "don't forget X has no reading meaning cancel" — arguable,
#: and it survived eleven adversarial phrasings. But the checkable reason is
#: better: **an exception suppresses exactly the tokens of its own span** —
#: here the ``dont`` and the ``forget`` — and cannot mask a denial signal
#: anywhere else, because the word loop continues past it and the bigram loop
#: has already run. So its incompleteness has nothing to be incomplete ABOUT.
#:
#: **That sentence was false in the code for one round, and it is the whole
#: safety argument.** The masking loop computed ``len(trio or pair)`` — and
#: ``trio`` is non-empty whenever any token remains — so a matched TWO-token
#: exception masked THREE tokens and ate the word after its own span.
#: Measured: "confirm tango, don't forget — hold on" and "confirm tango, don't
#: forget, cancel the other one" both APPROVED, and ``carries_denial("don't
#: forget, wait")`` was False, so the post-approval scan was blind to it too.
#: The span is now taken from the rule that actually matched. An exception's
#: mask is only ever as safe as its length.
#:
#: That is the form a future exception should be argued in. Its one known miss,
#: "don't forget, on second thought skip it", contains no grammar word at all
#: and is the stated §3.7 residual, not a gap in this entry.
#:
#: ``("cant", "wait")`` is the second entry and clears the same bar. It is a
#: CLOSED idiom — "can't wait" has no reading meaning "hold off" — and it is a
#: measured false reject: "confirm tango, tell them I can't wait to see it"
#: DENIED on the bare ``wait``. Post-normalization ``cant`` is a distinct
#: token, so the pair is expressible without touching ``wait`` itself, and the
#: mask covers those two tokens only: any other retraction in the utterance
#: still denies, including a second bare ``wait``.
#:
#: Its price, stated rather than assumed: a hesitated hold spelled "can't —
#: wait!" normalizes to the same two tokens and is suppressed. That is a real
#: false accept, and it is accepted for the same reason the ``("hold","on")``
#: ordering is: the idiom is common in ordinary speech and the hold spelling is
#: rare, and a lone ``wait`` anywhere else in the utterance still denies.
#:
#: Note the anchor sits at the TAIL here (``wait``), not the head. An exception
#: must CONTAIN a denial trigger or it suppresses nothing; requiring it first
#: would be a rule about spelling rather than about what is being suppressed.
_DENIAL_EXCEPTIONS = frozenset({("dont", "forget"), ("cant", "wait")})

#: Trigrams that suppress a BIGRAM denial. "do not forget the other branch" is
#: the uncontracted twin of the ``("dont", "forget")`` exception above, and it
#: has to be listed separately because normalization does not merge the two
#: forms — the same reachability trap that made this grammar dead once already.
#:
#: This one is safe to enumerate for the reason the block below explains: it is
#: a CLOSED phrase, not an open class. "don't forget X" has no reading in which
#: a person means "cancel", so there is no next word to have missed.
_DENIAL_BIGRAM_EXCEPTIONS = frozenset({("do", "not", "forget")})

#: **There is deliberately NO conditional exception, and the reason is the
#: general rule this file has now learned twice.**
#:
#: A ``("wait", "for")`` exception was tried, guarded by "suppress only when a
#: real object follows". Two things killed it:
#:
#: 1. **The comment described a grammatical rule and the code was a denylist.**
#:    It tested membership in a closed list of hold-words, which is the exact
#:    shape the comment claimed to avoid. Measured, it failed BOTH ways: "wait
#:    for those / these / mine / both / everything" APPROVED (holds, so the
#:    write went out), while "wait for that build" DENIED (a real condition).
#: 2. **Inverting it does not work either**, and this is the part that settles
#:    it. The obvious repair is "default deny; suppress only on determiner +
#:    noun". But *"wait for a second"* (a hold) and *"wait for a build"* (a
#:    condition) are **structurally identical** — determiner + noun in both. No
#:    structural test separates them. Preventing the hold would need a list of
#:    time-unit nouns, and that list's incompleteness FAILS OPEN.
#:
#: The rule, which sharpens the filler-denylist lesson this file already carries:
#:
#:     **When a set must be enumerated, enumerate the side whose incompleteness
#:     is safe.** An incomplete list of words-meaning-HOLD fails open — an
#:     unlisted hold word approves a retraction. An incomplete list of
#:     structures-meaning-CONDITION fails closed — an unrecognized phrase denies
#:     an approval, costing a re-propose and nothing else.
#:
#: The problem was never enumeration as such. It was that this enumeration sat
#: on the side where being wrong WRITES.
#:
#: So there is no CONDITIONAL exception, and ``wait`` denies wherever the closed
#: ``("cant", "wait")`` idiom above does not mask it — and that is **correct
#: behaviour, not a tolerated false reject.**
#:
#: (This entry used to say "``wait`` denies unconditionally"; #987 added that
#: idiom, and the absolute survived here for a round after the code stopped
#: honouring it.
#: The wiki's copy was corrected first and this one was not, which is the same
#: drift with its polarity reversed: a claim pinned on one surface only is
#: proved on one surface only.)
#:
#: The reason is semantic rather than budgetary: the
#: write is ``msg send`` and it fires IMMEDIATELY. The buddy has no defer
#: mechanism at all. So approving "confirm tango, wait until you hear back from
#: the reviewer" would SEND NOW while the owner believes it is being held — a
#: silent divergence between what they said and what happened, which is strictly
#: worse than a re-propose. A "wait" clause attached to an approval is
#: **semantically unhonorable**, and the correct home for it is the INSTRUCTION,
#: frozen at propose ("tell the reviewer to wait until X"), where it is content
#: for the recipient rather than a condition on the send.
#:
#: The cost is also smaller than it looks: matching is on the exact token, so
#: ``waiting``/``waited``/``awaiting`` never fire. Only the bare imperative does.
#:
#: This holds **only while recovery is cheap** — see the newest-first binding in
#: :meth:`ConfirmSpine._judge`. When recovery was broken, this rule composed with
#: it into a dead proposal, and the ``denied`` line promising "say the phrase
#: again" became false.
#:
#: Do not reintroduce a conditional exception without a test that separates
#: "wait for a second" from "wait for a build" — and if you find one, it is a
#: genuine discovery, not a list.


def _denial_tokens(tokens: "list[str]") -> bool:
    """Does this normalized token sequence contain a retraction?

    Fillers are removed before matching rather than tolerated at each site: a
    retraction split by a disfluency ("hold, uh, on") is the same retraction,
    and handling that per-rule is how one of the rules ends up forgetting.
    """
    words = [t for t in tokens if t not in _FILLERS]

    # Exceptions are evaluated FIRST and mask the span they cover. Checking
    # them per-rule at match time let a later bigram fire before the exception
    # for an earlier one was consulted — "dont forget that branch" denied via
    # ("forget","that"), so the exception never protected the phrase it exists
    # for. Masking makes the exception win over its whole clause regardless of
    # what else matches downstream.
    masked = list(words)
    for index in range(len(masked)):
        pair = tuple(masked[index:index + 2])
        trio = tuple(masked[index:index + 3])
        # The span comes from the rule that MATCHED, never from whichever slice
        # happened to be longer. `len(trio or pair)` was 3 whenever a third
        # token existed, so a two-token exception masked the word after its own
        # span — and that word was allowed to be a denial trigger. See
        # _DENIAL_EXCEPTIONS: the "suppresses exactly its own span" claim is the
        # entire argument for these entries being safe, so the length is not an
        # implementation detail of the loop.
        if trio in _DENIAL_BIGRAM_EXCEPTIONS:
            span = 3
        elif pair in _DENIAL_EXCEPTIONS:
            span = 2
        else:
            continue
        for offset in range(index, index + span):
            masked[offset] = ""

    for index in range(len(masked) - 1):
        if tuple(masked[index:index + 2]) in _DENIAL_BIGRAMS:
            return True
    if _gapped_bigram(masked):
        return True
    return any(token in _DENIAL_WORDS for token in masked)


def _gapped_bigram(masked: "list[str]") -> bool:
    """Does *masked* contain a :data:`_GAPPED_DENIAL_BIGRAMS` pair?

    The run of tolerated words between the two halves is closed and ends at the
    first word outside it — including a masked-out ``""``, so an exception's
    suppression still stops this rule rather than being skipped through.
    """
    for (first, second), gap_words in _GAPPED_DENIAL_BIGRAMS.items():
        for index, token in enumerate(masked):
            if token != first:
                continue
            for follower in masked[index + 1:]:
                if follower == second:
                    return True
                if follower not in gap_words:
                    break
    return False


_CONFIRM_WORDS = ("confirm", "confirmed")

_APOSTROPHE_RE = re.compile("['\u2019\u2018\u02bc`\u00b4]")
_PUNCT_RE = re.compile(r"[^\w\s]+")
_WS_RE = re.compile(r"\s+")

#: C0 and C1 control characters, plus DEL. NOT covered by ``\s+``, which only
#: catches tab/newline/CR/FF/VT — ESC, BEL, SOH and friends pass straight
#: through it.
#:
#: These are the known-silent wedge, measured against real tmux: a body carrying
#: an ANSI escape or a BEL renders into the pane as an invisible control ACTION,
#: so ``capture-pane`` returns text that no longer contains the rendered needle.
#: ``flush_session``'s ``stuck`` substring test then misses, the #689 heal never
#: fires, ``_box_static`` classifies it no-penalty, and the message is
#: **permanently wedged: never healed, never dead-lettered, therefore never
#: emailed** — the same failure newlines cause, reached by character rewriting.
#:
#: The realistic carrier is NOT the transcript (a speech-to-text model does not
#: emit ESC) — it is ``instruction``, which is model-supplied and was only
#: length-bounded. So this is applied at BOTH ends: here, and at propose time
#: before the argv is frozen, so the frozen argv is clean by construction and
#: "frozen" still means what it claims.
#:
#: Costs nothing in verbatim fidelity: no human utterance contains ESC.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def strip_controls(text: str) -> str:
    """Remove C0/C1 controls and DEL. See :data:`_CONTROL_RE` for why."""
    return _CONTROL_RE.sub("", text)


def normalize(text: str) -> str:
    """Casefold, map digit spellings, strip punctuation, collapse whitespace.

    Normalization runs on BOTH sides before matching. That is the half the
    digit-nonce design failed to price: an exact matcher over an unnormalized
    transcript rejects correct approvals, and a rejected correct approval is a
    livelock, not a near-miss.
    """
    # Apostrophes are ELIDED, not spaced. This one line is what makes the
    # denial grammar reachable at all: replacing them with a space turns
    # "don't" into "don t", and no sane word list contains "t". Every common
    # Unicode apostrophe, because a transcriber emits the curly one.
    deapostrophed = _APOSTROPHE_RE.sub("", text.lower())
    flat = _WS_RE.sub(" ", _PUNCT_RE.sub(" ", deapostrophed)).strip()
    return " ".join(_NUMBER_WORDS.get(t, t) for t in flat.split())


def mint_nonce(taken: "set[str] | frozenset[str] | None" = None) -> str:
    """One word from :data:`NONCE_WORDS` that is not in *taken*.

    **The ONE way to get a nonce.** It draws from the FREE set rather than
    retrying a random draw, and those are not equivalent — the difference is a
    real bug that shipped and was caught as a test flake:

    With k of n words taken, retry-until-unique fails spuriously with
    probability ``(k/n)**tries``. At 19 of 20 taken and 64 tries that is
    **3.8%** — a legitimate proposal refused while a nonce was still free, rare
    enough to read as a flake and frequent enough to happen. Drawing from the
    free set makes exhaustion an error and near-exhaustion a non-event.

    Uniqueness among live proposals is what closes "one approval, two
    proposals", so exhaustion must raise rather than reuse.
    """
    free = [w for w in NONCE_WORDS if w not in (taken or ())]
    if not free:
        raise RuntimeError(
            "no free nonce — too many proposals outstanding; reusing one would "
            "let a single approval satisfy two"
        )
    return secrets.choice(free)


def spoken_nonce(nonce: str) -> str:
    """How the buddy should SAY the nonce. It is already a word."""
    return nonce


#: The classification of an utterance against a proposal's nonce.
APPROVED = "approved"
DENIED = "denied"
WRONG_NONCE = "wrong_nonce"
NO_MATCH = "no_match"
#: The RIGHT nonce, inside the buddy's own announcement frame ("to approve,
#: say confirm tango"). Its own outcome rather than folded into WRONG_NONCE,
#: because the spoken reason is the owner's entire diagnostic and "that was a
#: different code word" is false here — the word was right, the FRAMING is
#: what refused it, and sending the owner to re-ask for a code they already
#: have fixes the one thing that was not broken.
QUOTED_FRAME = "quoted_frame"


def classify(text: str, nonce: str) -> str:
    """Classify *text* against *nonce*.

    Four outcomes rather than a boolean, because each one calls for DIFFERENT
    advice to the owner and collapsing them is how a recoverable state becomes
    a loop:

    - ``APPROVED`` — the phrase, said.
    - ``DENIED`` — a take-back. Correct advice: stop. A boolean matcher would
      report "wasn't the phrase, say it again", inviting the owner to repeat a
      thing they just retracted.
    - ``WRONG_NONCE`` — "confirm" plus a nonce that is not this proposal's.
      Correct advice: ask what the code was. Repeating the wrong word forever
      is the failure this outcome exists to prevent.
    - ``NO_MATCH`` — no confirm phrase at all. Correct advice: say it.

    **Matched by CONTAINMENT, not whole-utterance.** Whole-utterance strictness
    was inherited from a design whose grammar was "yes" — a token carrying no
    entropy, where containment let "yeah, that's right, anyway" through. The
    nonce carries the entropy itself, so no incidental utterance contains it,
    and strictness now buys nothing while rejecting the two most natural
    phrasings ("confirm tango please", "yeah, confirm tango"). Rejecting a
    correct approval is the expensive error here.
    """
    tokens = normalize(text).split()
    if not tokens:
        return NO_MATCH

    target = normalize(nonce)
    positions = [i for i, t in enumerate(tokens) if t in _CONFIRM_WORDS]
    if not positions:
        return NO_MATCH

    # The announcement frame, not an approval. The buddy's own proposal line
    # is "… To approve, say confirm <nonce>." — and speechSynthesis audio is
    # outside WebRTC echo cancellation, so a fragment of it can land in the
    # USER transcript (#950 defect 4). The structural fix is that the fallback
    # channel never carries the nonce; this is defence in depth for the frame
    # itself: "confirm" immediately preceded by "say", in an utterance that
    # also frames with "approve", is quoted instruction, and no human phrases
    # an approval that way. Deliberately NARROW — both conditions — because
    # the false-reject half is priced too: refusing a bare "say confirm
    # tango" from an owner parroting the advice line would loop them against
    # advice that says exactly those words. What this does NOT establish: an
    # echo chunked down to bare "confirm <nonce>" (frame lost) still
    # approves; only the nonce-free fallback text closes that.
    # Fillers are skipped BETWEEN the confirm word and the nonce, for the same
    # reason the denial grammar strips them before matching: "confirm, uh,
    # tango" is the phrase, said by someone hesitating before a code word,
    # which is exactly how people say code words. Requiring strict adjacency
    # refused a CORRECT approval and burned an attempt against the budget —
    # the false-reject half, and in this channel that is a silent loop.
    #
    # Safe by the file's own asymmetry argument: BOTH content words are still
    # required, in order. What is skipped is a closed set of disfluencies, and
    # an unlisted one fails CLOSED (no match, re-propose) rather than open. The
    # widening it does buy is real and small: "confirm — you know, tango was
    # the word" now approves. Containment already approves any utterance
    # carrying "confirm <nonce>", so this adds only the filler-separated
    # spelling of the same thing.
    def _rest_after(index: int) -> "list[str]":
        return [t for t in tokens[index + 1:] if t not in _FILLERS]

    def _quoted_frame(index: int) -> bool:
        preceding = [t for t in tokens[:index] if t not in _FILLERS]
        return bool(preceding) and preceding[-1] == "say" and "approve" in tokens

    quoted = False
    for index in positions:
        rest = _rest_after(index)
        if not rest or rest[0] != target:
            continue
        if _quoted_frame(index):
            quoted = True
            continue
        # Found "confirm <nonce>". A denial anywhere in the utterance — before
        # or after — is a take-back, and outranks the phrase.
        if _denial_tokens(tokens):
            return DENIED
        return APPROVED

    # "confirm <something else>" is a different problem from "no confirm
    # phrase at all", and the owner's next move differs.
    if _denial_tokens(tokens):
        return DENIED
    # Before the wrong-nonce scan, or a quoted correct nonce falls through to
    # it (the target IS in NONCE_WORDS) and reports "different code word"
    # about the right one.
    if quoted:
        return QUOTED_FRAME
    for index in positions:
        rest = _rest_after(index)
        if rest and rest[0] in NONCE_WORDS:
            return WRONG_NONCE
    return NO_MATCH


def matches_nonce(text: str, nonce: str) -> bool:
    """Convenience predicate: does *text* approve *nonce* outright?"""
    return classify(text, nonce) == APPROVED


def _edit_distance(a: str, b: str) -> int:
    """Plain Levenshtein distance. Inputs are short normalized tokens."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
        previous = current
    return previous[-1]


def near_miss_heard(text: str, nonce: str) -> str:
    """The token *text* rendered CLOSE to *nonce*, or ``""`` when none did.

    The false-reject half of the gate, made diagnosable (#1039): when the
    transcriber renders the code word as ``tang go`` or ``tangle``, the
    taxonomy used to answer ``refused`` — "say confirm and then the word I gave
    you" — which reads to the owner as "you were not heard at all". Naming what
    WAS heard turns the retry into one word instead of a renegotiation.

    This is messaging only, never matching: a near miss REFUSES exactly as it
    always did, burns an attempt exactly as it always did, and nothing here can
    approve. The bounds are what keep the diagnosis honest:

    - Candidates are single normalized tokens and adjacent-pair merges (the
      segmentation failure: ``tang go``), fillers removed.
    - Distance is bounded (1 for a nonce of four letters or fewer, else 2) —
      beyond that, "close" would be a guess spoken as a fact.
    - **Distance zero is excluded.** An utterance carrying the exact nonce
      without the confirm framing stays ``refused``, because the heard word is
      spoken back in the refusal line and the fallback channel must never
      carry the nonce (#953). Structural, not cosmetic: this function's return
      value is guaranteed to differ from *nonce*.
    - A candidate at or under the bound must also be STRICTLY closer to this
      nonce than to every other word in :data:`NONCE_WORDS`, and never one of
      them outright — a near miss must not cross into another live token, and
      an exact other-alphabet word is ``wrong_nonce`` territory, not this.
    """
    target = normalize(nonce)
    bound = 1 if len(target) <= 4 else 2
    tokens = [t for t in normalize(text).split() if t not in _FILLERS]
    candidates = list(tokens)
    candidates.extend(a + b for a, b in zip(tokens, tokens[1:]))
    best = ""
    best_distance = bound + 1
    for candidate in candidates:
        if candidate == target or candidate in NONCE_WORDS:
            continue
        if candidate in _CONFIRM_WORDS or candidate in _DENIAL_WORDS:
            continue
        distance = _edit_distance(candidate, target)
        if distance == 0 or distance > bound or distance >= best_distance:
            continue
        if any(
            _edit_distance(candidate, other) <= distance
            for other in NONCE_WORDS
            if other != nonce
        ):
            continue
        best, best_distance = candidate, distance
    return best


def request_utterance_from(ring) -> str:
    """The owner's REQUEST sentence, read from the ring at propose time (#953).

    This is what fills the body's ``said:`` slot. It used to be the APPROVING
    utterance — which the gate guarantees is ``confirm <nonce>``, so the slot
    shipped the nonce to the recipient on every approved write and carried
    none of the paraphrase-check content §4b built it for. The request
    utterance is the newest complete entry at propose time: the sentence that
    asked for the message, spoken BEFORE this proposal's nonce existed, so it
    cannot contain it by construction.

    One selection rule, and it is selection rather than redaction: an entry
    containing a confirm word is skipped. A stale ``confirm <word>`` from a
    PRIOR proposal (wrong-nonce, expired, retried) can sit newest in the ring,
    and it is not a request — it is the one remaining path a nonce string
    could re-enter the body through. Skipping falls back to the next-newest
    entry, and an empty result drops the slot entirely (:func:`render_body`),
    so the false-reject half costs a missing annotation, never a blocked or
    garbled write.
    """
    for entry in reversed(ring.snapshot()):
        if not entry.complete:
            continue
        if any(t in _CONFIRM_WORDS for t in normalize(entry.text).split()):
            continue
        return entry.text
    return ""


def carries_denial(text: str) -> bool:
    """Does *text* contain a refusal? Scanned over utterances AFTER an approval."""
    return _denial_tokens(normalize(text).split())


# =============================================================================
# Proposals
# =============================================================================


@dataclass
class Proposal:
    """One frozen write, waiting for the owner's spoken nonce.

    ``argv_prefix`` and ``instruction`` are captured at propose time and never
    reassigned. :meth:`build_argv` is the only way to turn them into a command,
    and it takes no caller-supplied parameters.

    ``anchor_seq`` is the logical time at which the buddy finished SPEAKING this
    proposal, supplied by the client's ``onSpoken`` — evidence the announcement
    was uttered, by the model OR by the ``speechSynthesis`` fallback. It is
    ``None`` until then, and an unanchored proposal is not confirmable: at
    propose time the owner has not yet heard what they would be approving, and
    barge-in is native on WebRTC. Anchoring to the moment it was HEARD rather
    than to the tool call is what makes "postdates the proposal" mean "after the
    owner heard it".

    An earlier version of this sentence sourced the stamp from "the client's
    ``response.done`` for that turn". The fallback path has no such turn, so
    that reading anchored nothing whenever the browser voice was what spoke —
    #951.

    **Everything is frozen at propose time — including the body.** It used to
    be that the body carried the confirm-time approving utterance; #953 killed
    that, because the approving utterance is ``confirm <nonce>`` by
    construction, so the slot shipped the nonce and verified nothing. The
    ``said:`` slot now carries ``request_utterance``, captured from the
    transcript ring at propose. Confirm's entire model-supplied surface is one
    token string, and it no longer reaches the body at all.
    """

    id: str
    token: str
    nonce: str
    tool: str
    session: str
    instruction: str
    argv_prefix: tuple[str, ...]
    created_at: float
    anchor_seq: "int | None" = None
    anchored_at: float = 0.0
    attempts: int = 0
    params: dict = field(default_factory=dict)
    #: The owner's request sentence at propose time — see
    #: :func:`request_utterance_from`. Empty means unknown, and the body's
    #: ``said:`` slot is then omitted rather than shipped empty.
    request_utterance: str = ""
    #: Whether :meth:`build_argv` appends the rendered §4b body. The msg
    #: handoff carries one; an argv-only write (every element validated at
    #: freeze time, no free text) does not, and appending a body to it would
    #: hand the CLI a positional argument it never asked for.
    append_body: bool = True
    #: What the buddy says when THIS write executes. Empty falls back to the
    #: msg-shaped "queued" phrasing — see :meth:`Verdict.to_dict` for why the
    #: two claims must differ (§3.6: never claim more than the write did).
    success_say: str = ""

    @property
    def announced(self) -> bool:
        return self.anchor_seq is not None

    def expired(self, now: float, ttl: float) -> bool:
        # The TTL runs from the moment the owner HEARD it, not from the tool
        # call: a proposal the buddy has not finished speaking has not started
        # costing the owner anything yet.
        started = self.anchored_at or self.created_at
        return now >= started + ttl

    def build_argv(self) -> list[str]:
        # No parameters, deliberately: the approving utterance must never
        # reach the body again (#953), and a parameterless signature makes
        # that structural rather than a calling convention.
        if not self.append_body:
            return list(self.argv_prefix)
        prefix = list(self.argv_prefix)
        # The full relay (#1015) is written HERE, at execution, not at propose:
        # a proposal the owner cancels or lets expire must leave nothing on
        # disk. ``write_relay`` never raises — this runs after the
        # ``_proposals.pop()`` with the utterance spent, and while a throw now
        # degrades to a spoken ``build_failed`` (#1005) rather than silence,
        # that outcome still costs the owner the message and a re-propose, so
        # a relay miss must not be allowed to buy it.
        written = ""
        # Matched against the path this id DERIVES, and matched at the TAIL:
        # ``ConfirmSpine.propose`` appends its pair last, so the tail is the
        # only position that identifies OUR pair rather than someone else's.
        # Both halves of that matter. Reading "the first ``--ref``" would let a
        # future spec's own frozen ``--ref`` steer this write — the one thing a
        # frozen argv must never contain is a model-supplied path we then open
        # — and it would ALSO leave our pair in place on the mismatch, shipping
        # exactly the dangling pointer this code argues is worse than none,
        # while the removal below deleted the other spec's pair instead of
        # ours. Tail-matching makes both unreachable. An id that is not one (a
        # hand-built Proposal) simply means no relay.
        try:
            expected = str(relay.relay_path(self.id))
        except ValueError:
            expected = ""
        if expected and prefix[-2:] == ["--ref", expected]:
            written = relay.write_relay(
                Path(expected),
                proposal_id=self.id,
                session=self.session,
                sender=self._reply_target(),
                instruction=self.instruction,
                request_utterance=self.request_utterance,
            )
            if not written:
                # A pointer to a file that is not there is worse than no
                # pointer: the recipient reads a missing path as "the real
                # instruction is elsewhere" and stops, where an excerpt at
                # least says something true. Drop the flag with the slot.
                del prefix[-2:]
        return [
            *prefix,
            render_body(
                self.instruction,
                self.request_utterance,
                self.id,
                reply_to=self._reply_target(),
                full_path=written,
            ),
        ]

    def _flag_value(self, flag: str) -> str:
        """The value frozen after *flag* in the argv prefix, or ``""``."""
        prefix = self.argv_prefix
        for index, token in enumerate(prefix[:-1]):
            if token == flag:
                return prefix[index + 1]
        return ""

    def _reply_target(self) -> str:
        """The sender name from the frozen argv — who a reply should address.

        Read from the frozen ``--from`` rather than passed separately, so the
        nudge can never name anyone other than the identity the message
        actually goes out under.
        """
        return self._flag_value("--from")


# =============================================================================
# Attribution rendering (spec §4b)
# =============================================================================

#: Hard cap on the rendered body. **Measured in a real Claude Code pane**, not
#: reasoned about — reproduce with ``tools/voice_heal_probe.py``.
#:
#: The binding constraint is ``flush_session``'s ``stuck`` test: a plain
#: substring match against the input box with NO #851 window path, so once the
#: box renders only a WINDOW of the message the #689 heal never fires and the
#: message wedges permanently — never healed, never dead-lettered, therefore
#: never emailed.
#:
#: Measured at 80x24 on 2026-08-06, by rendered-line length::
#:
#:     470  ->  box 482   stuck hit    ✓
#:     500  ->  box 512   stuck hit    ✓
#:     520  ->  box 532   stuck hit    ✓        <- last passing
#:     540  ->  box 480   stuck MISS   ✗        <- box starts windowing
#:     880  ->  box  16   stuck MISS   ✗        <- [Pasted text …] chip
#:
#: So the real boundary is a rendered line of ~520 chars, and there are TWO
#: failure regimes above it, not one: the box windows first, and only much
#: later collapses to the chip.
#:
#: 300 is the BODY cap, and the rendered line adds the ``[MSG from <sender> ·
#: <kind>] `` prefix and the ``  ⟨#id6⟩`` tail — 31 chars plus the sender name
#: with ``kind=voice``, so 63 for a 32-character worktree sender. That lands the
#: worst case at 363 against a measured 520, keeping ~30% margin. (Derived from
#: ``inbox.Message.render``'s format, not from a probe reading: the probe's
#: numbers describe synthetic bodies at chosen lengths, and quoting one of those
#: as "the worst case" is how this comment previously arrived at ~57 and ~385.)
#:
#: **Re-measured for #985, and the number deliberately did not move.** Slice 1b
#: changed both halves of the arithmetic: the ``<voice> `` prefix left the body
#: (8 chars back to the instruction/utterance/nudge budget) and the kind slot
#: went ``request`` → ``voice`` (2 chars off the rendered prefix). Both point
#: the same way — the worst rendered line fell 365 → 363 — so the headroom the
#: pane measurement bought grew slightly rather than shrinking, and no new
#: measurement is owed. The freed 8 body chars are spent where #981 finding 6
#: says they compete: the droppable reply nudge now fits in more cases. Raising
#: MAX_BODY_CHARS to consume the headroom would still need a fresh pane probe.
#:
#: **Re-measured for #1015, and again the number did not move.** The relay
#: pointer adds a ``full: <path>`` slot INSIDE this cap, so the worst rendered
#: line is unchanged at 363 — the pointer is paid for out of the excerpt and the
#: droppable nudge, never out of the margin. (363 is this arithmetic, against
#: ``WORST_SENDER_CHARS = 32``; a sweep over real bodies with the 33-character
#: sender the tests use reports 364. Same margin, one character of sender.)
#: The probe was re-run at 80x24 on
#: 2026-08-11 with the pointer riding, and the cliff sits where it did::
#:
#:     476  ->  box 488   stuck hit    ✓        <- last passing probed
#:     546  ->  box 480   stuck MISS   ✗        <- box windows
#:    1026  ->  box  16   stuck MISS   ✗        <- [Pasted text …] chip
#:
#: The recorded 520/540 boundary sits inside that 476–546 window, so it stands.
#: The round trip closed on the real shipped worst case (336 chars rendered):
#: pasted whole, ``stuck`` hit, ``finish_submit`` submitted, dedup found it.
#:
#: **The margin is deliberate and the measurement is pane-dependent.** The box
#: shows a bounded number of ROWS, so a shorter pane windows sooner than 80x24
#: did. Do not raise this cap to consume the measured headroom without
#: re-measuring at the smallest pane you care about.
#:
#: The second constraint, ``VERIFY_SCROLLBACK_LINES = 200`` bounding the dedup
#: needle, is not binding at these lengths: 520 chars is ~7 rows of an 80-column
#: pane. Verified live — the dedup found the message after the heal submitted it.
MAX_BODY_CHARS = 300

#: The measured rendered-line boundary above, so tests can assert against the
#: measurement rather than against a number retyped from a comment.
MEASURED_STUCK_LIMIT_CHARS = 520

#: The longest line a buddy write can put in a recipient's box, as arithmetic
#: rather than prose — ``MAX_BODY_CHARS`` plus what ``inbox.Message.render``
#: wraps around it. Pinned in the tests against a REAL rendered ``Message``, so
#: a change to that format fails here instead of quietly eating the margin the
#: pane measurement bought.
#:
#: The overhead: ``"[MSG from "`` (10) + sender + ``" · "`` (3) +
#: ``"voice"`` (5) + ``"] "`` (2) + ``"  ⟨#"`` (4) + 6-char id + ``"⟩"`` (1).
WORST_SENDER_CHARS = 32
_RENDER_OVERHEAD_CHARS = 31
WORST_RENDERED_LINE_CHARS = (
    MAX_BODY_CHARS + _RENDER_OVERHEAD_CHARS + WORST_SENDER_CHARS
)

#: How much of the verbatim utterance survives into the rendered line.
MAX_UTTERANCE_CHARS = 90

#: How much of the instruction survives INTO THE RENDERED LINE. Distinct from
#: ``write_tools.MAX_INSTRUCTION_CHARS``, which bounds what the model may
#: propose at all; this one bounds what the recipient's pane has to render.
#:
#: Since #1015 this is a PREVIEW budget rather than the message: anything it
#: clips is still reachable in full through the ``full:`` pointer below. Before
#: that it was the message, and a long spoken request simply lost its tail.
MAX_RENDERED_INSTRUCTION_CHARS = 160

#: The body's slot separator. One definition, because the budget arithmetic in
#: :func:`render_body` counts it — a second spelling would make the cap
#: arithmetic silently wrong rather than visibly different.
SEP = " ┃ "

#: The label on the pointer to the full relay file (#1015).
POINTER_LABEL = "full: "

#: The preview never shrinks below this. A pathologically long relay path (a
#: deep ``$HOME``) would otherwise eat the excerpt entirely; below this floor
#: the pointer is dropped instead. **Both halves priced:** dropping it costs a
#: recoverable tail again (today's behaviour), while keeping it costs the
#: recipient every scannable word of what the message is even about — and a
#: message nobody reads the file for is not more recoverable than one nobody
#: can read.
MIN_EXCERPT_CHARS = 80


def _one_line(text: str) -> str:
    """Collapse *text* to a single line.

    Load-bearing, and measured rather than assumed. The paste itself is safe
    with newlines — ``pane_manager.send_to_target`` uses ``tmux paste-buffer
    -p`` (bracketed paste) with ``enter=False`` — and the #621 dedup is safe
    too, because ``message_on_scrollback`` whitespace-normalizes both sides.

    **The #689 heal is what breaks.** A multi-line paste renders in Claude Code
    as the ``[Pasted text #N +M lines]`` chip and nothing else, so
    ``flush_session``'s ``stuck`` substring test finds nothing, ``finish_submit``
    never runs, ``_box_static`` classifies it no-penalty after three sweeps, and
    the message is **permanently wedged: never healed, never dead-lettered,
    therefore never emailed** — surfacing only via ``doctor`` after two hours.
    For a channel whose entire justification is "the owner is not watching a
    screen", that is the worst available failure.

    Control characters are stripped here for the SAME failure reached a
    different way — see :data:`_CONTROL_RE`. ``\\s+`` does not cover them.
    """
    return _WS_RE.sub(
        " ", strip_controls(text).replace("\r", " ").replace("\n", " ")
    ).strip()


def _clip(text: str, limit: int) -> str:
    text = _one_line(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


#: Leading dashes and whitespace, in any interleaving. ``lstrip("-")`` is NOT
#: this: it stops at the first non-dash, so ``"- - force"`` keeps its second
#: dash. See :func:`_lead_safe` for why the difference was data loss.
_LEAD_DASH_RE = re.compile(r"^[-\s]+")


def _lead_safe(text: str) -> str:
    """Strip every leading dash so *text* can never open the rendered body.

    Until #985 this was free: the body opened with the ``<voice>`` marker, so
    the model-supplied instruction was never in first position. Moving
    attribution to the kind slot put it there, and the body is passed to the
    CLI as a POSITIONAL — a leading ``-`` is parsed as a flag, a bug this repo
    has shipped twice (see ``tools._SESSION_RE``'s comment, which records both).

    **This function must be TOTAL, and that is a data-loss argument rather than
    a tidiness one.** The guarantee used to be enforced by an ``assert`` in
    :func:`render_body`, with this function merely usually-right: it stripped
    only the first dash RUN, so ``"- - force a restart"`` reached the assert and
    raised. ``render_body`` is called from ``Proposal.build_argv()``, which
    ``ConfirmSpine.confirm`` runs **after** ``_proposals.pop()`` — the proposal
    is already consumed and the approving utterance already spent, and at the
    time this shipped nothing guarded that path, so the raise destroyed the
    message with no retry and, for a screenless owner, nothing anywhere saying
    why. (#1005 has since put a structural guard around ``build_argv`` — a
    throw there now degrades to a spoken ``build_failed`` — but that outcome
    still costs the owner the message, so totality here remains the cheaper
    property, not a redundant one.)

    So: no raise on this path, and no ``assert`` either. An assert is compiled
    out by ``python -O``, which turned the incomplete strip into the *silent*
    version of the same bug — a flag-shaped body shipped with the guard gone.
    A guarantee that evaporates under a standard interpreter flag is not a
    guarantee. The regex is total and idempotent, ``render_body`` applies it to
    the finished body as the single enforcement point, and totality is swept in
    ``test_voice_kind.py`` over every dash/space/tab prefix up to length 4 plus
    a real ``-O`` subprocess.

    Stripping rather than escaping because nothing real is lost: a spoken
    instruction does not begin with a hyphen, and one that does is a
    mis-transcription.
    """
    return _LEAD_DASH_RE.sub("", _one_line(text)).strip()


def reply_nudge(reply_to: str) -> str:
    """The body's reply-path slot: the literal command that reaches the buddy.

    #962's live failure: the recipient answered a buddy request IN ITS OWN
    TERMINAL, and the reply never came back — the owner is listening, not
    watching that pane, so an on-screen answer is a lost one. ``--from buddy``
    and the ``· voice`` kind slot say who asked; neither says how to answer.
    This slot does, as a runnable command rather than prose, because the
    recipient is an agent and the one thing it reliably does with a command is
    run it.

    The role text (worker/orchestrator) states the same etiquette; the slot is
    what covers recipients running with no agentwire role text at all.
    """
    return f'reply: agentwire msg send --to {_clip(reply_to, 40)} --kind done "<answer>"'


def render_body(
    instruction: str,
    request_utterance: str,
    proposal_id: str,
    *,
    reply_to: str = "",
    full_path: str = "",
) -> str:
    """The fixed one-line shape every buddy write carries (§4b).

    Part one is what the buddy asked for; part two is what the owner actually
    said when REQUESTING it, verbatim, plus the proposal id. The recipient can
    check the paraphrase rather than trust it — and can see it when the buddy
    got it wrong.

    **The request utterance, never the approving one (#953).** The approving
    utterance is ``confirm <nonce>`` by construction — content-free for the
    paraphrase check, and a leak of the nonce into the recipient's scrollback
    on every write. When no request utterance was captured, the slot is
    OMITTED: a slot whose expected content is empty must not survive.

    **The body never begins with a dash, and that is a safety property, not an
    accident of layout.** ``instruction`` is model-supplied and now leads the
    body, so it could reach the CLI as a FLAG rather than a value — this repo
    has shipped exactly that bug twice. Until #985 the ``<voice>`` marker
    guaranteed this for free by occupying first position; :func:`_lead_safe`
    now owns it deliberately, and the assertion below is the check on that one
    mechanism.

    Visible separators rather than newlines: scannable without being a wall,
    and without the wedging failure newlines cause.

    **Attribution is NOT in the body — it is the ``voice`` kind (#985).** §4
    rules that ``--from buddy`` alone is not enough: a recipient must tell a
    buddy-originated message from a human-typed one without reading carefully.
    Slice 1 satisfied that with a ``<voice>`` marker at the front of the body,
    explicitly as a stand-in, because the kind slot then said ``request`` and
    distinguished nothing. Slice 1b puts it in the slot that actually drives
    behaviour: ``inbox.Message.render`` prints ``[MSG from buddy · voice]``, so
    the distinguisher sits in the same on-screen position the marker held while
    also being the thing ``ESCALATE_KINDS`` and the drain read. No marker
    remains; a body that still carries one is stale text, not attribution.

    **The inline text is a PREVIEW, and the pointer is the message (#1015).**
    The caps here cannot be raised — they are the measured boundary past which
    the #689 heal stops firing — so a long spoken request had exactly two
    fates, and the shipped one silently dropped its tail into the recipient's
    lap ("Treat it as a running list for anyt…"). *full_path* names a file
    holding the whole thing (:mod:`~agentwire.voice_layer.relay`), and the
    ``full:`` slot puts it on the delivered line, where an agent can read it.

    That slot is NOT droppable — it is the recoverability of everything the
    other slots clip, so dropping it under budget pressure would be dropping
    the fix. The reply nudge stays droppable and the excerpt shrinks; both are
    losses the pointer makes recoverable. It rides exactly when something WAS
    clipped — asked of the body as it ships TODAY, without the pointer's own
    cost (see the predicate below, which is self-fulfilling if asked the other
    way): a body carrying the whole utterance already needs no pointer to it,
    and paying ~50 characters of a 300-character line for a redundant one would
    cost the excerpt and the nudge on every short message.
    """
    instruction_line = _one_line(instruction)
    tail = f"#{proposal_id}"
    said = (
        f'said: "{_clip(request_utterance, MAX_UTTERANCE_CHARS)}"'
        if request_utterance.strip()
        else ""
    )
    pointer = f"{POINTER_LABEL}{_one_line(full_path)}" if full_path.strip() else ""

    def excerpt_budget(said_slot: str, pointer_slot: str) -> int:
        """What is left for the preview once every other slot is paid for."""
        cost = len(tail)
        for slot in (said_slot, pointer_slot):
            if slot:
                cost += len(slot) + len(SEP)
        return min(MAX_RENDERED_INSTRUCTION_CHARS, MAX_BODY_CHARS - cost - len(SEP))

    # **Asked WITHOUT the pointer's own cost, and that is the whole predicate.**
    # Deducting it first makes the pointer manufacture the clipping it then
    # claims to be recovering: with a 90-char quote and a 47-char path the
    # budget falls 160 → 133, so a 145-character instruction that rendered
    # WHOLE before #1015 would be clipped to 133 and handed a pointer — a
    # message made worse by the fix for messages being made worse. The question
    # is "does this body lose anything as it ships today?", so it is asked of
    # today's body.
    lost = (
        len(instruction_line) > excerpt_budget(said, "")
        or len(_one_line(request_utterance)) > MAX_UTTERANCE_CHARS
    )
    if not lost:
        pointer = ""  # nothing was clipped, so there is nothing to recover
    if pointer and excerpt_budget(said, pointer) < MIN_EXCERPT_CHARS:
        # A long ``$HOME`` can squeeze the preview below its floor. What gives
        # way FIRST is the ``said:`` quote, and the ordering is the whole
        # ruling: the quote is reproduced verbatim in the file the pointer
        # names, so dropping it costs a slot the recipient can still read,
        # while dropping the pointer costs the only copy of everything the
        # other slots clipped. Recoverable yields to unrecoverable.
        dropped_said, said = said, ""
        if excerpt_budget(said, pointer) < MIN_EXCERPT_CHARS:
            # The path is long enough that even that was not enough, so the
            # pointer goes after all — and the quote comes BACK. Dropping it
            # bought room for a slot that is no longer there, and shipping
            # neither would be strictly worse than shipping what main shipped.
            pointer, said = "", dropped_said

    parts = [_lead_safe(_clip(instruction_line, excerpt_budget(said, pointer)))]
    if said:
        parts.append(said)
    if pointer:
        parts.append(pointer)
    parts.append(tail)
    body = SEP.join(parts)
    # The reply-path slot (#962) is DROPPABLE, whole-or-not-at-all: it rides
    # only when the full body still fits MAX_BODY_CHARS, and it slots in
    # BEFORE the id so the id is never what pays for it. Both halves priced:
    # included, it makes the reply path a runnable command; dropped, the cost
    # is a missing nudge — the role text still states the etiquette — never a
    # half-truncated command or a clipped id. A budget bump here would need
    # the pane re-measurement MAX_BODY_CHARS documents; a droppable slot does
    # not.
    if reply_to.strip():
        with_nudge = SEP.join([*parts[:-1], reply_nudge(reply_to), parts[-1]])
        if len(with_nudge) <= MAX_BODY_CHARS:
            body = with_nudge
    body = _clip(body, MAX_BODY_CHARS)
    # THE enforcement point, and it corrects rather than complains. `_clip` only
    # truncates the tail, so this cannot touch the id; `_lead_safe` is total and
    # idempotent, so on the overwhelmingly common path it is a no-op over a
    # string that already passed through it. What it replaces was an `assert`,
    # which (a) raised from inside `build_argv()` — after the proposal was
    # consumed, so the owner simply lost the message — and (b) vanished under
    # `python -O`, shipping the flag-shaped body it existed to prevent.
    return _lead_safe(body)


# =============================================================================
# Outcomes
# =============================================================================

#: What the buddy SAYS for each outcome, keyed on the owner's next move (§3.4).
#: Deliberately distinct: ``refused`` and ``pending_transcript`` require
#: OPPOSITE behaviour, so collapsing them trains the owner to repeat into a
#: system that needed them to wait.
#:
#: One dict, so an outcome without a line fails a test rather than shipping mute.
SPOKEN = {
    "no_proposal": (
        "I don't have anything pending, so there's nothing to confirm. "
        "Tell me again what you'd like sent."
    ),
    "expired": (
        "That one expired before you confirmed it. Ask me again and I'll set it up fresh."
    ),
    # A not_announced that fails to announce is the recursion §3.4 exists to
    # prevent. That is the one place the timer-armed fallback has to be
    # unconditional.
    #
    # Concretely: this outcome fires when the buddy has not finished SPEAKING
    # the proposal, which is exactly when a response is in flight — the
    # `responseActive` branch. If the announcement of "I haven't finished
    # saying it yet" is itself swallowed by the response it is describing, the
    # owner hears nothing, waits, and the conversation deadlocks on two parties
    # each waiting for the other. The announcer must not special-case it, must
    # not skip the cancel for it (the cancel is gated on the in-flight mirror,
    # which is TRUE in exactly this state), and its fallback must stay
    # reachable — see client.py's createAnnouncer: the timer is armed before
    # anything that can fail, and TWO bounded deferrals may POSTPONE it,
    # neither of which can cancel it. BOTH are live in this state, which is not
    # obvious and was got wrong once. The in-flight leg keys on a response
    # created AFTER the announce (`sawCreate` is only set while the item is
    # current), so the response already in flight HERE cannot take it — but
    # that is not the only response in play: this outcome fires with
    # `responseActive` TRUE, so pump() cancels that one and creates OURS, and
    # the server's ack of our create sets `sawCreate` while the item is still
    # current. The owner-speaking leg (`maxOwnerDeferrals`, 3) never cared which
    # response is running. So at `fallbackMs` 6000 the announcer's own half of
    # the wait is its general worst case, 5 intervals — 30s — dropping to 4
    # (24s) only in the sub-case where our own create is never acked at all.
    # The deadlock sentence above was written against 2 intervals, 12s.
    #
    # And that half is not the whole wait (#997, closed by #1009). Both
    # deferrals are counted from the moment this refusal becomes `current` in
    # the announcer's FIFO, and pump() holds a queued item UPSTREAM of that
    # while a fallback utterance is still playing — bounded by that
    # utterance's own speaking budget (30s floor + 140ms/char, client.py's
    # speakingBudget). So in the one state where the browser voice is
    # mid-utterance when this refusal is queued, the worst case is one
    # speaking budget in front of the 30s above, and a coalesced five-reply
    # notice puts minutes there, not seconds. The interval arithmetic above is
    # complete only when nothing is being spoken.
    #
    # The deadlock argument survives the bigger number, and not by calling 30s
    # tolerable. It survives because a deferral is not a suppression: each is
    # counted per item and the re-armed timer eventually speaks with no
    # condition left to fail. And the leg that grew is the one that costs the
    # owner nothing — the owner-speaking deferral is taken only when the owner
    # IS speaking at the moment of the check, so it extends the buddy's wait,
    # not the owner's silence. An owner who stops talking stops that leg
    # deferring at once; the in-flight leg does not key on them at all, so at
    # most one unspent deferral still lands between their silence and the
    # speech. That bounds the silence anyone can be left in waiting for THIS
    # refusal at 12s, whatever the 30s above does to the buddy's own patience.
    # The pump leg does not add to it: it is taken only while a fallback
    # utterance is PLAYING — audio the owner is hearing the whole time — so it
    # too extends the wait and never the silence, which is why the deferral
    # was chosen over talking over it. A deadlock needs both parties waiting;
    # only one of them ever is.
    "not_announced": (
        "Hang on — I haven't finished telling you what I'd send yet."
    ),
    # "already SENT" was the same over-claim §3.6 forbids on the success path,
    # which says "queued" precisely because msg send queues. A refusal may not
    # claim more certainty than the success it refers back to.
    "replayed": "I already did that one, so I'm not doing it again.",
    "refused": (
        "I didn't hear the confirmation phrase, so I haven't sent anything. "
        "Say confirm and then the word I gave you."
    ),
    # A refusal that can NAME what was heard (#1039). ``{heard}`` is filled by
    # :attr:`Verdict.spoken` from ``heard_word``, which ``near_miss_heard``
    # guarantees is never the nonce itself — the fallback voice speaks this
    # line, and the fallback channel must never carry the nonce (#953). The
    # advice is the full phrase, not the bare word, because the bare word does
    # not approve and advice that cannot work is the taxonomy's cardinal sin.
    "near_miss": (
        "I heard {heard} — close, but not quite the code word, so I haven't "
        "sent anything. Say confirm and the word I gave you, once more."
    ),
    "wrong_nonce": (
        "That was a different code word, so I haven't sent anything. "
        "Ask me what the word was and I'll say it again."
    ),
    # The right word inside the announcement frame ("to approve, say confirm
    # tango"). NOT wrong_nonce: telling this owner their code word was wrong
    # sends them to re-ask for the one thing they already have. The word was
    # right; the phrasing read as my own announcement quoted back.
    "quoted_frame": (
        "That sounded like my own announcement coming back, so I haven't "
        "sent anything. The word was right — just say confirm and the word, "
        "on its own."
    ),
    # Covers "no" AND "wait"/"hold on", so it must not assert the owner said
    # the word "no" — a reason that misinforms is the defect §3.4 is about.
    #
    # "Say the phrase again" is TRUE HERE and only here: an in-band denial
    # leaves the proposal LIVE, so the nonce still works. It is false on the
    # retraction paths, which pop — hence `cancelled` below rather than one
    # line stretched over two different next moves.
    "denied": (
        "I heard you hold off, so I haven't sent it. "
        "Say the phrase again when you're ready."
    ),
    # An explicit retraction that RETIRED the proposal: `cancel`, and the
    # confirm barrier that finds a cancel already recorded.
    #
    # Split from `denied` for the reason the taxonomy exists — the owner's next
    # move differs. `denied`'s advice is "say the phrase again", and following
    # it here lands on `no_proposal` ("Tell me again what you'd like sent") one
    # turn later: advice that cannot work, pointing at the exact line
    # `_cancel_refusal` was built to keep off this path.
    #
    # A pure stand-down, and that is the whole content: the owner asked for
    # this, so nothing further is required of them. It deliberately does NOT
    # offer to set it up again — pressing for the write just retracted is the
    # same defect from the other side.
    "cancelled": "Okay — I've dropped that one, so I haven't sent anything.",
    "pending_transcript": (
        "Give me a second — I'm still catching up on what you said. Don't repeat it yet."
    ),
    # A duplicate confirm on a token already being processed. It must NOT say
    # "nothing was sent" — the first confirm may be inside the runner as this
    # is spoken — and it must not send the owner to re-propose, which is how a
    # duplicate becomes a double delivery. Wait, then hear the real outcome.
    "in_flight": (
        "Hang on — I'm already working on that one. "
        "I'll tell you how it went in a moment; don't repeat it yet."
    ),
    # A CANCEL that lost the race to a confirm already INSIDE THE RUNNER
    # (#990). It must not say "I haven't sent it" — that is the over-claim
    # `in_flight` is worded to avoid, and here it would be worse, because the
    # owner asked for exactly that outcome and would hear it granted.
    #
    # It must also not tell them to wait and stop there: the one thing they
    # tried to do is the one thing that is no longer available, so the line
    # names the uncertainty AND the move that is still open (undo it after the
    # fact, from the recipient's side), which is what "wait" alone would hide.
    #
    # The condition is `_dispatching`, NOT `_in_flight`, and the difference is
    # this line's whole truth value: the claim is held across the ≤2.5s await,
    # where nothing has been sent and this sentence would be false. See
    # ConfirmSpine.cancel.
    "cancel_in_flight": (
        "Too late to stop that one — it's already going out. Don't repeat it; "
        "I'll tell you how it went in a moment and we can undo it from there."
    ),
    # A cancel with nothing of ours to retract — never proposed, already
    # retired, or expired. It collapses `no_proposal` and `expired` for the
    # cancel caller ONLY, and the reason is that both of their confirm-shaped
    # lines argue for the write: "Tell me again what you'd like sent" and "Ask
    # me again and I'll set it up fresh". Answering a retraction by pressing
    # for the thing retracted is the taxonomy defect §3.4 names, reached
    # through a reused line rather than a wrong one.
    "nothing_to_cancel": (
        "There's nothing waiting on my side, so there's nothing to take back — "
        "I haven't sent anything."
    ),
    "too_many_attempts": (
        "I've got that wrong too many times, so I've dropped it. Ask me again from the top."
    ),
    # Names the owner's next move AND the uncertainty, because the two are not
    # separable here and naming only one produces a different defect.
    #
    # "nothing was sent" was a definite claim the system cannot verify:
    # `run_agentwire_cmd` returns success=False on `subprocess.TimeoutExpired`
    # (core.py), and a timed-out CLI may already have enqueued. Pairing
    # that false certainty with "ask me again" invited a re-propose that
    # DOUBLE-DELIVERS — the acting-twice failure, reached through a spoken line
    # asserting more than the system knows.
    #
    # Note the next move CHANGES once the uncertainty is stated: verify-then-
    # decide, not re-propose. That is the honest instruction, and it is only
    # reachable by admitting what is unknown.
    "dispatch_failed": (
        "That failed partway and I can't tell whether it took effect. "
        "Check that session before asking me again."
    ),
    # The argv could not even be BUILT (#1005): the throw landed between the
    # pop and the runner, so — unlike `dispatch_failed`, whose whole line is
    # the uncertainty — the system positively knows the runner was never
    # called and nothing went out. That difference is the owner's next move:
    # here a re-propose is SAFE and is the only remedy (the utterance is
    # spent, the proposal popped), while `dispatch_failed`'s "check that
    # session" would send them to verify a write that provably never left.
    "build_failed": (
        "Something broke on my side before anything went out, so nothing "
        "was sent. Ask me again and I'll set it up fresh."
    ),
}

#: How many tokens of the buddy's own line an utterance must reproduce, VERBATIM
#: and CONTIGUOUSLY, before :func:`is_buddy_echo` will call it an echo.
#:
#: This number is the entire false-reject budget of that rule, so it is chosen
#: against the collision rather than for tidiness. The denial triggers that
#: appear in :data:`SPOKEN` are short and ordinary — ``hold off``, ``hang on``,
#: ``dont`` — and they are exactly what a real owner says to retract. At two or
#: three tokens the rule would suppress a genuine "hold off" spoken over the
#: browser voice, and a suppressed retraction WRITES.
#:
#: Six tokens is past the point where a human utters a machine's sentence word
#: for word by coincidence: it takes ``hang on im already working on`` rather
#: than ``hang on``.
#:
#: **Guarded from BELOW only, and that asymmetry is deliberate.** Lowering this
#: is what the tests forbid, because it suppresses a real retraction and a
#: suppressed retraction WRITES. Raising it is the fail-closed direction — a
#: genuine echo stops being recognised, denies, and costs one re-spoken
#: approval — so nothing pins it from above. Do not read the pins as saying 6
#: is optimal in both directions; they say it is a FLOOR.
_ECHO_MIN_TOKENS = 6


def is_buddy_echo(text: str) -> bool:
    """Is *text* the buddy's own voice coming back through the microphone?

    ``speechSynthesis`` is outside the WebRTC path, so ``echoCancellation`` does
    not cover the fallback voice: what it says re-enters the mic and lands in
    the USER transcript. Approval is safe from that by construction — the
    fallback channel never carries a nonce (#953) — but :func:`carries_denial`
    is not nonce-gated, and several :data:`SPOKEN` lines CONTAIN denial
    triggers ("I heard you hold off…", "Hang on — I'm already working on that
    one", "I don't have anything pending…"). Echoed inside the approval→confirm
    window, one of those retroactively denies the owner's own approval and
    reports a take-back they never spoke (#992).

    **The discriminator is content, not timing, and that choice is the whole
    decision.** The obvious rule — "utterances transcribed while the fallback
    voice is speaking do not count as denials" — is unusable here: barge-in over
    the robot voice is the NORMAL way to retract in this channel, so that rule
    drops genuine take-backs and the write goes out. That is the acting-twice
    direction, which is strictly worse than the wrongful refusal it fixes (one
    utterance to recover, via the newest-first binding in
    :meth:`ConfirmSpine._judge`). A content rule has no such cost: it cannot
    fire on words the buddy never said.

    Two properties keep it on the safe side:

    - **The WHOLE utterance must be the echo.** A barge-in captured together
      with the tail of the buddy's line ("hang on im already working on that one
      no stop") is not a contiguous run of that line, so it denies. Only a clean
      capture of the machine's own words is suppressed.
    - **The enumeration fails CLOSED.** A short or garbled echo — the
      transcriber catching three words of it — misses :data:`_ECHO_MIN_TOKENS`
      and denies, costing one re-spoken approval. Nothing about being
      incomplete here can make a write happen.

    **What it does NOT cover, stated rather than implied.** :data:`SPOKEN` is
    the buddy's own taxonomy, so this covers only what THIS module makes it
    say. Attacker-influenceable text (a delivered message body, spoken verbatim
    by ``composeNotice``) is not in it. That text cannot reach this window
    today for a separate reason — ``client.py``'s ``canSpeak``/``canInterrupt``
    both require ``!confirmGate.outstanding()``, and the gate is closed from the
    moment a proposal is spoken until its terminal outcome or TTL, which is
    exactly the approval→confirm window. If that gate is ever relaxed, this
    function needs the spoken text fed to it rather than read from
    :data:`SPOKEN`, and the ``said-during-fallback`` mark stays the wrong
    instrument for the reason above.
    """
    tokens = normalize(text).split()
    if len(tokens) < _ECHO_MIN_TOKENS:
        return False
    return any(_contains_run(normalize(line).split(), tokens) for line in SPOKEN.values())


def _contains_run(haystack: "list[str]", needle: "list[str]") -> bool:
    """Does *needle* appear in *haystack* as a contiguous run?"""
    if not needle or len(needle) > len(haystack):
        return False
    first = needle[0]
    span = len(needle)
    for index, token in enumerate(haystack):
        if token == first and haystack[index:index + span] == needle:
            return True
    return False


#: Outcomes whose correct owner response is to WAIT rather than to speak again.
#: Named so the persona and the tests can both reason about it.
#: ``in_flight`` belongs here on both counts the flag drives: the owner's
#: correct move is to wait, and ``confirm_terminal`` must stay False — closing
#: the gate on a duplicate would close it out from under the confirm that is
#: actually running.
#: ``cancel_in_flight`` belongs here for the same two reasons ``in_flight``
#: does: the owner's correct move is to wait for the real outcome, and
#: ``confirm_terminal`` must stay False — a cancel that lost the race must not
#: close the handshake out from under the confirm that is still running.
WAIT_OUTCOMES = frozenset(
    {"pending_transcript", "not_announced", "in_flight", "cancel_in_flight"}
)

#: Every reason :class:`ConfirmSpine` can return. The SSOT for the taxonomy.
#:
#: The guard on :data:`SPOKEN` has to run in BOTH directions. Checking only
#: "every outcome has a line" catches a mute refusal but lets a LINE WITHOUT AN
#: OUTCOME ship as dead code — which is exactly what happened:
#: ``too_many_attempts`` had a carefully written spoken line and no producer,
#: so the attempt that actually retired a proposal reported ``refused`` and
#: told the owner to say the phrase again at the precise moment that stopped
#: being possible.
REASONS = frozenset(
    {
        "no_proposal", "expired", "not_announced", "replayed", "refused",
        "near_miss", "wrong_nonce", "quoted_frame", "denied",
        "pending_transcript",
        "too_many_attempts", "dispatch_failed", "build_failed", "in_flight",
        "cancel_in_flight", "nothing_to_cancel", "cancelled",
    }
)


@dataclass
class Verdict:
    """The outcome of a confirm. ``approved`` is the only one that writes."""

    approved: bool
    reason: str
    utterance: str = ""
    argv: "list[str] | None" = None
    #: The ring entry the approval matched, so it can be spent on success.
    utterance_item_id: str = ""
    #: The proposal's own success line, when it declared one. Empty keeps the
    #: msg-shaped "queued" claim, which is the only honest default for a write
    #: that enqueues rather than completes.
    success_say: str = ""
    #: The session the approved write acted on, copied from the proposal
    #: FROZEN at propose time — never from anything supplied at confirm. The
    #: client's re-raise ledger keys on this to retire reminders; before it,
    #: the client guessed by remembering the last proposal, which is wrong
    #: whenever two proposals interleave. Empty means the write has no session
    #: target, and the key is then omitted rather than shipped empty.
    acted_session: str = ""
    #: The near-miss token, set only on ``reason == "near_miss"``. Filled into
    #: the spoken line so the owner hears what WAS heard (#1039). Guaranteed by
    #: :func:`near_miss_heard` to never be the nonce.
    heard_word: str = ""

    @property
    def spoken(self) -> str:
        if self.approved:
            return ""
        line = SPOKEN.get(self.reason) or "I couldn't do that, so nothing was sent."
        if self.reason == "near_miss":
            # The template needs a word; an empty one (a hand-built Verdict)
            # degrades to the plain refused line rather than speaking a blank.
            if not self.heard_word:
                return SPOKEN["refused"]
            return line.format(heard=self.heard_word)
        return line

    def to_dict(self) -> dict:
        if self.approved:
            # "queued", never "sent" (§3.6). ``agentwire msg send`` QUEUES: the
            # CLI says so verbatim, delivery happens at the next safe boundary
            # and can defer behind the box gates. From the owner's ear, "I told
            # the orchestrator" followed by nothing is worse than a silent
            # refusal, because success was affirmatively claimed.
            if self.success_say:
                # A write that COMPLETES when the runner returns says so
                # plainly; "queued" would under-claim it the same way "sent"
                # over-claims a queue (§3.6 cuts both ways).
                return {
                    "success": True,
                    "reason": "done",
                    "approved_by": self.utterance,
                    "say": self.success_say,
                    "must_speak": True,
                    "confirm_terminal": True,
                    # Only APPROVED payloads carry acted_session: a cancel is
                    # terminal but is not acting, and retiring a reminder on
                    # it silently deletes the re-raise (#967).
                    **(
                        {"acted_session": self.acted_session}
                        if self.acted_session
                        else {}
                    ),
                }
            return {
                "success": True,
                "reason": "queued",
                "queued": True,
                "sent": False,
                "approved_by": self.utterance,
                "say": (
                    "Queued it — it'll land when that session is free."
                ),
                "must_speak": True,
                "confirm_terminal": True,
                **(
                    {"acted_session": self.acted_session}
                    if self.acted_session
                    else {}
                ),
            }
        return {
            "success": False,
            "reason": self.reason,
            "say": self.spoken,
            "must_speak": True,
            "owner_should_wait": self.reason in WAIT_OUTCOMES,
            # Name-independent handshake signal: True exactly when this
            # outcome ENDS the confirm exchange (the client's gate currently
            # keys on tool names; this is the generic key to move it to).
            "confirm_terminal": self.reason not in WAIT_OUTCOMES,
            **({"heard": self.utterance} if self.utterance else {}),
        }


class ConfirmSpine:
    """The propose/confirm token store plus the code-side approval evaluation.

    One per bridge, injected into tool dispatch. Deliberately not a module-level
    singleton: a process-global store of pending writes outlives the
    conversation that proposed them.
    """

    def __init__(
        self,
        ring: TranscriptRing,
        *,
        ttl_s: float = PROPOSAL_TTL_S,
        wait_s: float = APPROVAL_WAIT_S,
        runner=None,
        clock=None,
    ):
        import time as _time

        self._ring = ring
        self._ttl_s = ttl_s
        self._wait_s = wait_s
        self._runner = runner
        self._clock = clock or _time.monotonic
        self._lock = threading.Lock()
        self._proposals: dict[str, Proposal] = {}
        #: Tokens whose write genuinely went out. ``replayed`` means THIS.
        self._succeeded: set[str] = set()
        #: Tokens whose write was attempted and FAILED. Kept apart from
        #: _succeeded so a retry is told the truth rather than "already sent".
        self._failed: set[str] = set()
        #: Tokens claimed by a confirm that has not returned yet. See _claim.
        #: **Claimed is not dispatching.** The claim is taken at the TOP of
        #: :meth:`confirm`, before the ≤2.5s await and before the judge, so for
        #: most of its life this set means "a confirm is deciding", not "a write
        #: is going out". Anything that speaks about the WRITE must read
        #: ``_dispatching`` instead — see :meth:`cancel`.
        self._in_flight: set[str] = set()
        #: Tokens whose argv has been handed to the runner and has not come
        #: back. The ONLY state in which "it's already going out" is true.
        self._dispatching: set[str] = set()
        #: Tokens the owner retracted while a confirm held the claim. The
        #: confirm re-reads this under the lock immediately before dispatch, so
        #: a cancel that arrives during the await genuinely WINS rather than
        #: being told it was too late (#990, review round 2).
        self._cancelled: set[str] = set()

    # -- propose ------------------------------------------------------------

    def propose(
        self,
        *,
        tool: str,
        session: str,
        instruction: str,
        argv_prefix: "list[str] | tuple[str, ...]",
        params: "dict | None" = None,
        append_body: bool = True,
        success_say: str = "",
    ) -> Proposal:
        """Mint a single-use, TTL-bounded proposal with the argv frozen.

        Nonces are unique among LIVE proposals: two outstanding proposals
        sharing one would re-open the "one approval, two proposals" hole the
        nonce exists to close.
        """
        with self._lock:
            self._expire_locked()
            # One minting path, and it is mint_nonce's. A second, subtly
            # different way to draw a nonce sitting next to the right one is
            # how the wrong one gets called later.
            nonce = mint_nonce({p.nonce for p in self._proposals.values()})
            proposal_id = secrets.token_hex(3)
            # The relay pointer is frozen HERE, with the rest of the argv, so
            # "the whole argv is frozen at propose" stays literally true (#966).
            # It can be: the path is a pure function of the id this line just
            # minted, so it is knowable before the file exists — and it is
            # code-derived, never model-supplied. ``build_argv`` writes the file
            # and drops this pair again if the write fails.
            #
            # **This makes ``append_body=True`` imply "the target verb accepts
            # ``--ref``"**, which is true of ``msg send`` and of every spec that
            # exists. A future body-carrying spec pointed at a different verb
            # inherits the flag and fails at the CLI — so the coupling is named
            # here rather than left for whoever writes that spec to discover.
            # ``append_body=False`` writes are unaffected: they get no pair.
            if append_body:
                argv_prefix = (
                    *argv_prefix, "--ref", str(relay.relay_path(proposal_id)),
                )
            proposal = Proposal(
                id=proposal_id,
                token=secrets.token_urlsafe(18),
                nonce=nonce,
                tool=tool,
                session=session,
                instruction=instruction,
                argv_prefix=tuple(argv_prefix),
                created_at=self._clock(),
                params=dict(params or {}),
                request_utterance=request_utterance_from(self._ring),
                append_body=append_body,
                success_say=success_say,
            )
            self._proposals[proposal.token] = proposal
        return proposal

    def announce(self, proposal_id: str, seq: int) -> bool:
        """Anchor a proposal on evidence its announcement was SPOKEN.

        Called from the client's ``onSpoken`` — for the model path (a
        ``response.done`` whose transcript carried the text) and equally for the
        ``speechSynthesis`` fallback, which produces no model turn. Until this
        lands the proposal is not confirmable — see :attr:`Proposal.anchor_seq`.

        An earlier docstring said "the ``response.done`` in which it was
        spoken". #951 retired that reading: it has no meaning on the fallback
        path, so the mechanism added to GUARANTEE speech became the reason a
        proposal could never be anchored.
        """
        with self._lock:
            for proposal in self._proposals.values():
                if proposal.id == proposal_id and proposal.anchor_seq is None:
                    proposal.anchor_seq = seq
                    proposal.anchored_at = self._clock()
                    return True
        return False

    def pending(self) -> list[Proposal]:
        with self._lock:
            self._expire_locked()
            return list(self._proposals.values())

    # -- confirm ------------------------------------------------------------

    def confirm(self, token: str) -> Verdict:
        """Evaluate the gate for *token*; on approval, run the frozen argv.

        The only parameter is the token, so there is structurally nothing to
        mutate between propose and confirm.
        """
        proposal, refusal = self._claim(token)
        if refusal is not None:
            return refusal

        try:
            return self._confirm_claimed(proposal, token)
        finally:
            # Released on EVERY exit, including a raising runner. A marker left
            # set is a proposal wedged for its whole TTL, answering every retry
            # with "still working on that one" — a silent loop, which is the
            # expensive failure in a channel with no screen.
            with self._lock:
                self._in_flight.discard(token)

    def _confirm_claimed(self, proposal: Proposal, token: str) -> Verdict:
        anchor = proposal.anchor_seq or 0
        # The high-water mark is read TWICE: once before the await, and again
        # after it, taking the larger. The post-approval scan must be bounded —
        # an unbounded ring tail lets an utterance from a different context
        # retroactively deny — but bounding it to the PRE-await snapshot left
        # the guard closed only for utterances the owner had already started
        # when confirm was entered.
        #
        # The hole that leaves: a denial the owner BEGINS during the ≤2.5s
        # await records its speech_started above the snapshot and carries no
        # transcript yet, so `after` (which filters on complete) cannot see it
        # and `unheard_between` excluded it for exceeding the ceiling. The
        # write went out with a take-back mid-transcription. Re-reading makes
        # the bound "everything the owner had started by the time this confirm
        # reached its verdict" — still bounded, and still strictly before the
        # verdict. The widened `found` seqs are subsumed, since high_seq
        # advances on every ring event.
        #
        # THE PRICE, and it is now bounded rather than open-ended (#989). The
        # widened window used to cost more than "one pending_transcript wait":
        # any speech_started with no transcript to follow — a cough, a VAD blip,
        # TTS bleed — sat here refusing every confirm as a WAIT outcome, so no
        # attempt was burned, `too_many_attempts` never fired, and the owner
        # heard "give me a second" for up to the whole TTL and then "that one
        # expired". A spoken loop, which in a screenless channel is the
        # expensive failure.
        #
        # The bound lives in TranscriptRing, not here, and that division is the
        # same one as before: this function cannot tell a never-completing entry
        # from a slow one without the ring's clock. What the ring adds is two
        # bounds rather than one — `transcribed` retires the cough (an EMPTY
        # transcript is an answer, not a wait) and the commit splits the rest
        # into "the audio closed, so ASR is overdue" and "the owner may still be
        # speaking". See transcript.unheard_between; the two-shape argument is
        # in that module's docstring rather than duplicated here.
        ceiling = max(self._ring.high_seq, anchor)
        found = self._ring.await_utterance_after(anchor, self._wait_s)
        ceiling = max(ceiling, self._ring.high_seq)
        verdict = self._judge(proposal, found, ceiling)

        if not verdict.approved:
            if verdict.reason in WAIT_OUTCOMES:
                # A timing miss is not the model's fault and must not burn an
                # attempt, or a slow transcriber would exhaust the proposal.
                return verdict
            if self._penalize(token):
                # The attempt that hits the cap RETIRES the proposal, so it must
                # SAY so. Returning `refused` here — "say confirm and then the
                # word I gave you" — tells the owner to do the one thing that
                # can no longer work, at the exact moment it stopped working.
                # Same shape as the pending_transcript token-burn trap, which
                # §3.0(a) closed upstream and which survived here.
                return Verdict(approved=False, reason="too_many_attempts")
            return verdict

        # THE CANCEL BARRIER (#990, review round 2). The claim is taken at the
        # top of `confirm`, so it has been held across the ≤2.5s await and the
        # judge — during which nothing has been sent. A cancel arriving in that
        # window is therefore not "too late": it WINS, and this is where the
        # confirm finds out.
        #
        # Under the lock together with the `_dispatching` mark, because the two
        # are one decision: reading `_cancelled` and then marking would leave a
        # window in which a cancel lands after the check and is told, truthfully
        # by then but wrongly at the time it spoke, that the write was going
        # out. The approving utterance is deliberately NOT spent on this path —
        # nothing was acted on, so nothing was consumed.
        with self._lock:
            if token in self._cancelled:
                # No pop here, deliberately: every writer of `_cancelled` pops
                # in the same lock hold, so this could never be the removing
                # one — an unreachable write that reads as a guarantee on the
                # next pass, which is what this module objects to two branches
                # away in `cancel`.
                return Verdict(
                    approved=False, reason="cancelled", utterance=verdict.utterance
                )
            self._dispatching.add(token)

        try:
            return self._dispatch(proposal, token, verdict)
        finally:
            with self._lock:
                self._dispatching.discard(token)

    def _dispatch(self, proposal: Proposal, token: str, verdict: Verdict) -> Verdict:
        """Run the frozen argv. Reached only past the cancel barrier."""
        self._ring.spend(verdict.utterance_item_id)
        with self._lock:
            self._proposals.pop(token, None)

        # GUARDED ON ITS OWN, not folded into the runner's except (#1005).
        # This line still runs after the pop with the approving utterance
        # already spent, and every function on the path is total on str — but
        # totality was the only thing standing between a future edit and
        # silent loss of an approved message, and nothing enforced it. Now a
        # throw degrades STRUCTURALLY to a spoken outcome.
        #
        # The ruling the move forced, priced on both halves: a build_argv
        # throw means the runner was NEVER called, so unlike `dispatch_failed`
        # the system positively knows nothing went out. Reusing that reason
        # here would claim uncertainty it does not have and send the owner to
        # check a session nothing was sent to — so this is its own outcome,
        # `build_failed`, whose line admits the loss and invites the
        # re-propose that is safe here and unsafe there. `buddy_sent` gets a
        # record with an EMPTY argv and success False: `delivery_state` reads
        # that as "it never went out, the reason is in detail", which is
        # literally true. And the token is deliberately marked NEITHER
        # `_failed` nor `_succeeded` — `_failed` would answer the retry with
        # "check that session"; unmarked, a retry lands on `no_proposal` and a
        # cancel on `nothing_to_cancel`, both of which are true.
        try:
            argv = proposal.build_argv()
        except Exception as exc:
            outbox.record_write(  # #958; never raises
                proposal,
                [],
                {"success": False, "error": f"build_argv raised before dispatch: {exc}"},
            )
            return Verdict(
                approved=False, reason="build_failed", utterance=verdict.utterance
            )
        verdict.argv = argv
        if self._runner is not None:
            try:
                result = self._runner(argv) or {}
            except Exception as exc:  # a dispatch that raises must not read as sent
                result = {"success": False, "error": str(exc)}
            outbox.record_write(proposal, argv, result)  # #958; never raises
            if not result.get("success", False):
                # NOT _succeeded. The write did not happen, and a token in
                # _succeeded makes the retry say "I already sent that one" —
                # over-claiming the SEND itself, on the one path where the
                # system already KNOWS it failed, to an owner who is not
                # watching a screen. ``replayed`` must mean it really went out.
                #
                # The retry gets dispatch_failed rather than another attempt on
                # purpose: a failed dispatch may have partially written (the CLI
                # can fail after enqueueing), so re-running the argv risks a
                # duplicate delivery — "the orchestrator acts twice", the §4
                # failure. Telling the owner the truth and letting them
                # re-propose is the safe direction.
                with self._lock:
                    self._failed.add(token)
                return Verdict(
                    approved=False, reason="dispatch_failed", utterance=verdict.utterance
                )
        with self._lock:
            self._succeeded.add(token)
        return verdict

    def cancel(self, token: str) -> Verdict:
        """Retire a proposal without writing — through the SAME claim (#990).

        "Never gated, because refusing is free" was true of the refusal and
        false of the SENTENCE. This used to pop whatever it found and say *"I
        heard you hold off, so I haven't sent it"* — including while a confirm
        holding the same token was inside the runner. That is the over-claim
        :data:`SPOKEN`'s ``in_flight`` line is deliberately worded to avoid, on
        the sibling path: the owner hears an affirmative "nothing was sent",
        with no screen to discover otherwise. #987 made confirm-vs-confirm safe
        by construction and stopped there; cancel-vs-in-flight-confirm is the
        same shape.

        **"A confirm holds the claim" is NOT "the write is going out", and
        conflating them re-committed the defect this issue is about** (review
        round 2). :meth:`_claim` is taken at the TOP of :meth:`confirm`, before
        the ≤2.5s await and before the judge, so the dominant occupant of
        ``_in_flight`` is a confirm still DECIDING. Refusing a cancel there with
        "it's already going out" is the same false statement, inverted: the
        measured shape is a cancel during the await, a confirm that then returns
        ``pending_transcript``, a runner never called, and a proposal the owner
        asked to drop still sitting pending.

        So the race is split on the thing the sentence is about:

        - ``_dispatching`` — the argv is inside the runner. This, and only this,
          is when ``cancel_in_flight`` is true.
        - ``_cancelled`` — a cancel that arrives while a confirm merely holds the
          claim WINS. It pops the proposal and answers ``denied``, honestly:
          nothing has been sent. The confirm re-reads the marker under the same
          lock immediately before dispatch and returns ``denied`` too, so the
          write does not go out behind the retraction.

        **The false-reject half, priced across all of it.** A cancel refused for
        the wrong reason leaves a proposal the owner believes is dead:

        - the announcement requirement is DROPPED (``require_announced=False``).
          A cancel of a proposal the buddy has not finished speaking must
          succeed — refusing it with ``not_announced`` would leave the proposal
          live for its full TTL over a technicality about who was talking.
        - **no cancel outcome leaves a live proposal behind.** The measured
          failure was "refused cancel, then refused confirm, wedged to TTL with
          no second word", and it came from refusing during the await; the split
          above removes it at the source, because that cancel now wins. The
          remaining refusals reach the owner only when the proposal is already
          gone (dispatched, replayed, failed, expired). Pinned as a property
          over every reachable cancel outcome rather than asserted here.
        - ``cancel_in_flight`` is a WAIT outcome with its own spoken line, and
          that line has to say what happens next, because the one thing the
          owner cannot do is take it back: the write is already going out and
          its real outcome is seconds away.
        - the shared claim's other refusals are TRANSLATED, not passed through
          (:meth:`_cancel_refusal`) — a cancel must never be answered with
          confirm-shaped advice to re-propose the write just retracted.
        """
        with self._lock:
            # THE RECORDED OUTCOME OUTRANKS EVERY IN-PROGRESS MARKER, and the
            # order of these three tests is the whole of it.
            #
            # `_succeeded`/`_failed` are facts about the WRITE; `_dispatching`
            # and `_in_flight` are facts about the ATTEMPT, and only the first
            # kind can contradict a spoken claim about sending. That rule was
            # stated one round ago and applied against `_in_flight` alone,
            # which left the identical hole on the other marker: a confirm adds
            # `_failed` and only THEN clears `_dispatching`, so in between, a
            # cancel testing the marker first was told "Too late to stop that
            # one — it's already going out … we can undo it from there" about a
            # dispatch already recorded as FAILED. Two definite claims, about
            # the one outcome the system has established it CANNOT characterise
            # (see `dispatch_failed`'s line: "I can't tell whether it took
            # effect"). A rule enforced against one of two markers is not a
            # rule; it is a fix at the site that happened to break.
            #
            # Hoisting costs nothing where `cancel_in_flight` is right: mid-
            # runner NEITHER terminal fact is set, so it still wins there.
            if token in self._succeeded:
                return Verdict(approved=False, reason="replayed")
            if token in self._failed:
                return Verdict(approved=False, reason="dispatch_failed")
            if token in self._dispatching:
                # The ONE true "too late": the argv is inside the runner, and
                # no outcome has been recorded for it yet.
                #
                # Nothing is popped or marked here, and that is deliberate
                # rather than an omission: `_dispatch` popped the proposal
                # before calling the runner, and the cancel barrier is already
                # behind us, so both would be writes nothing can read. A line
                # whose effect is unreachable reads as a guarantee on the next
                # pass — the same objection this module makes to a property
                # that documents a rule nothing enforces. The property they
                # looked like they were buying — no cancel outcome leaves a
                # live proposal — is real, and is pinned as a property.
                return Verdict(approved=False, reason="cancel_in_flight")
            if token in self._in_flight:
                # A confirm holds the claim, has not reached the runner, and
                # has not recorded a result. Nothing has been sent, so the
                # cancel is simply honoured.
                self._cancelled.add(token)
                self._proposals.pop(token, None)
                return Verdict(approved=False, reason="cancelled")

        proposal, refusal = self._claim(token, require_announced=False)
        if refusal is not None:
            return self._cancel_refusal(refusal)
        try:
            with self._lock:
                self._cancelled.add(token)
                self._proposals.pop(token, None)
            return Verdict(approved=False, reason="cancelled")
        finally:
            # Same discipline as confirm: released on every exit, or the token
            # is wedged for its TTL answering every retry with "still working
            # on that one" — a silent loop.
            with self._lock:
                self._in_flight.discard(token)

    @staticmethod
    def _cancel_refusal(refusal: Verdict) -> Verdict:
        """Re-key a shared-claim refusal for the CANCEL caller.

        The claim is shared so the two paths cannot drift, but its spoken lines
        are written for a confirm, and two of them argue for exactly the thing
        the owner just retracted: ``no_proposal`` says *"Tell me again what
        you'd like sent"* and ``expired`` says *"Ask me again and I'll set it up
        fresh"*. Answering a cancel with either is the system pressing for the
        write — in a screenless channel, with no way to see that it is
        answering the wrong question.

        The two COLLAPSE into one outcome deliberately, and that is consistent
        with the taxonomy rule rather than an exception to it: outcomes are kept
        apart when the owner's next move differs, and here both mean "there is
        nothing of mine to take back", whose next move is the same — none.

        ``replayed`` and ``dispatch_failed`` pass through unchanged: both are
        true of a cancel, and neither invites a re-propose ("I already did that
        one" / "check that session before asking me again").
        """
        if refusal.reason in ("no_proposal", "expired"):
            return Verdict(approved=False, reason="nothing_to_cancel")
        return refusal

    # -- internals ----------------------------------------------------------

    def _judge(
        self, proposal: Proposal, found: "list[Utterance]", ceiling: int
    ) -> Verdict:
        if not found:
            return Verdict(approved=False, reason="pending_transcript")

        usable = [u for u in found if not u.estimated]
        if not usable:
            # Only entries with unknown ordering are available. Treated as a
            # timing miss rather than a rejection: the owner's correct move is
            # still to wait, and telling them to repeat would be wrong.
            return Verdict(approved=False, reason="pending_transcript")

        # NEWEST approval first. Binding the OLDEST made a retraction PERMANENT:
        # "confirm juniper" / "no wait" / "confirm juniper" denied, and stayed
        # denied for the rest of the 120s TTL. Forward iteration broke on the
        # first approval, so the post-approval scan started at the OLD one and
        # the intervening denial sat inside the window forever — a newer, valid
        # approval could never become the match.
        #
        # Newest-first makes the stale denial PREDATE the match, so the existing
        # strictly-after rule excludes it and changing your mind back costs
        # exactly one utterance. This also has to be right before any false
        # reject can honestly be called cheap: "cheap" means recoverable, and
        # recovery ran through this loop.
        match = None
        wrong_nonce = None
        quoted = None
        for entry in reversed(usable):
            outcome = classify(entry.text, proposal.nonce)
            if outcome == DENIED and is_buddy_echo(entry.text):
                # Our own refusal line, back through the mic (#992). Skipped
                # rather than treated as a denial — and skipped HERE as well as
                # in the post-approval scan below, because an echo landing after
                # the approval is the newest entry, so this loop would return
                # `denied` before the scan was ever reached.
                continue
            if outcome == DENIED:
                # An explicit take-back wins immediately, and is a DIFFERENT
                # refusal from "that wasn't the phrase" — the owner should stop,
                # not repeat themselves.
                return Verdict(approved=False, reason="denied", utterance=entry.text)
            if outcome == WRONG_NONCE and wrong_nonce is None:
                wrong_nonce = entry
            if outcome == QUOTED_FRAME and quoted is None:
                quoted = entry
            if outcome == APPROVED:
                match = entry
                break
        if match is None:
            if quoted is not None:
                # The right word, quoted inside the announcement frame. More
                # specific than wrong_nonce, and the spoken advice differs:
                # the owner does not need a new code, only a bare phrasing.
                return Verdict(
                    approved=False, reason="quoted_frame", utterance=quoted.text
                )
            if wrong_nonce is not None:
                # "Right shape, wrong code" needs "ask me what the code was",
                # not "say it again" — repeating the wrong word loops forever.
                return Verdict(
                    approved=False, reason="wrong_nonce", utterance=wrong_nonce.text
                )
            # Before the generic refusal: did any usable entry render the code
            # word CLOSE to right (#1039)? Newest first, same order the judge
            # binds approvals. Messaging only — still a refusal, still burns an
            # attempt — but the spoken line names what was heard, so the retry
            # is one word rather than a renegotiation. Denials never reach
            # here: the loop above returns on DENIED before this runs.
            for entry in reversed(usable):
                heard = near_miss_heard(entry.text, proposal.nonce)
                if heard:
                    return Verdict(
                        approved=False,
                        reason="near_miss",
                        utterance=entry.text,
                        heard_word=heard,
                    )
            return Verdict(
                approved=False, reason="refused", utterance=usable[-1].text
            )

        # A denial committed AFTER the approval refuses the write. This closes
        # the stale-approval window the bounded await would otherwise leave
        # open: the owner said the phrase, then changed their mind before the
        # model got round to calling confirm.
        #
        # BOUNDED to the approval→confirm window, not the whole ring tail: an
        # unbounded scan lets an utterance from much later — including one that
        # arrives during a RETRY's bounded await — retroactively deny an
        # approval, and report "You said no" about something the owner said in
        # a different context. `ceiling` covers everything the owner had
        # started by the time this confirm reached its verdict: it is read
        # before the await AND again after it (see `confirm`), so an utterance
        # begun DURING the await is inside the window. Saying "as of this
        # confirm's entry" here described the old, narrower bound and would
        # have let the widened window be reviewed as the one it replaced.
        later = [
            entry
            for entry in self._ring.after(
                match.speech_started_seq, include_spent=True
            )
            if entry.speech_started_seq <= ceiling
        ]
        # The echo guard, not `carries_denial` alone: the buddy's own spoken
        # line echoing back through the un-echo-cancelled fallback channel is
        # not the owner changing their mind (#992). See :func:`is_buddy_echo`
        # for why the discriminator is content rather than "was the robot
        # talking", and the judge loop above for the second site it needs.
        if any(
            carries_denial(entry.text) and not is_buddy_echo(entry.text)
            for entry in later
        ):
            return Verdict(approved=False, reason="denied", utterance=match.text)

        # And the same window may hold an utterance the owner has SPOKEN whose
        # transcript has not landed. `after` cannot see it — it filters on
        # `complete` — so a denial spoken after the approval and still in
        # transcription used to sail straight past this scan and the write went
        # out. The sequence already tells us they spoke again; we simply cannot
        # yet say what they said, and "cannot yet say" is pending_transcript,
        # never approval. This is the bounded-await asymmetry applied to the
        # denial side, where it was missing.
        if self._ring.unheard_between(match.speech_started_seq, ceiling):
            return Verdict(
                approved=False, reason="pending_transcript", utterance=match.text
            )

        return Verdict(
            approved=True,
            reason="approved",
            utterance=match.text,
            utterance_item_id=match.item_id,
            success_say=proposal.success_say,
            acted_session=proposal.session,
        )

    def _claim(
        self, token: str, *, require_announced: bool = True
    ) -> "tuple[Proposal | None, Verdict | None]":
        """Take exclusive ownership of *token*, or say why not.

        ``require_announced=False`` is :meth:`cancel`'s (#990). Every other
        check applies to both callers — one claim, so the cancel path cannot
        drift away from the confirm path — but "the buddy has not finished
        saying it yet" is a reason to refuse a CONFIRM (the owner cannot have
        approved what they have not heard) and not a reason to refuse a
        RETRACTION, which needs no announcement to be meant.

        **Single use is a property of this method, not of the timing.** The
        proposal used to be consumed at the far side of the await and the
        judge, so two confirms carrying one token could both pass here and both
        reach the runner. It was not reproducible — client dispatch is
        sequential per response and the judge window is sub-timeslice — but
        each ``response.done`` spawns its own async IIFE and the bridge is a
        ``ThreadingHTTPServer``, so the sequencing that made it safe is nowhere
        in the code. This module's standard is guarantee by construction.

        The marker also fixes what the second confirm was TOLD. Arriving while
        the runner was mid-dispatch, it found the proposal already popped and
        reported ``no_proposal`` — "tell me again what you'd like sent" —
        inviting a re-propose of a write that was in the act of going out.
        """
        with self._lock:
            if token in self._in_flight:
                return None, Verdict(approved=False, reason="in_flight")
            if token in self._failed:
                return None, Verdict(approved=False, reason="dispatch_failed")
            if token in self._succeeded:
                return None, Verdict(approved=False, reason="replayed")

            proposal = self._proposals.get(token)
            # THIS token's expiry is decided before the general sweep. Sweeping
            # first deletes it and the lookup then reports "no_proposal", which
            # silently collapses two outcomes whose spoken advice differs
            # ("ask me again" vs "tell me again what you wanted") — exactly the
            # taxonomy collapse §3.4 forbids.
            if proposal is not None and proposal.expired(self._clock(), self._ttl_s):
                del self._proposals[token]
                self._expire_locked()
                return None, Verdict(approved=False, reason="expired")

            self._expire_locked()
            if proposal is None:
                return None, Verdict(approved=False, reason="no_proposal")
            if require_announced and not proposal.announced:
                return None, Verdict(approved=False, reason="not_announced")
            self._in_flight.add(token)
            return proposal, None

    def _penalize(self, token: str) -> bool:
        """Count a refused attempt. Returns True if that RETIRED the proposal."""
        with self._lock:
            proposal = self._proposals.get(token)
            if proposal is None:
                return False
            proposal.attempts += 1
            if proposal.attempts >= MAX_CONFIRM_ATTEMPTS:
                del self._proposals[token]
                return True
            return False

    def _expire_locked(self) -> None:
        now = self._clock()
        for token in [
            t for t, p in self._proposals.items() if p.expired(now, self._ttl_s)
        ]:
            del self._proposals[token]


__all__ = [
    "APPROVAL_WAIT_S",
    "MAX_BODY_CHARS",
    "MAX_CONFIRM_ATTEMPTS",
    "PROPOSAL_TTL_S",
    "SPOKEN",
    "WAIT_OUTCOMES",
    "ConfirmSpine",
    "Proposal",
    "Verdict",
    "APPROVED",
    "DENIED",
    "NONCE_WORDS",
    "NO_MATCH",
    "QUOTED_FRAME",
    "WORST_RENDERED_LINE_CHARS",
    "WRONG_NONCE",
    "carries_denial",
    "classify",
    "is_buddy_echo",
    "matches_nonce",
    "mint_nonce",
    "near_miss_heard",
    "normalize",
    "render_body",
    "reply_nudge",
    "request_utterance_from",
    "spoken_nonce",
]
