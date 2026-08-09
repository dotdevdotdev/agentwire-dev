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
strictness was buying. Hence :data:`NONCE_WORDS` (one spelling each, no digits)
and containment rather than whole-utterance matching. See :func:`classify`.

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

from . import outbox
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
NONCE_WORDS = (
    "tango", "harbor", "violet", "cobalt", "meadow", "falcon", "amber",
    "kestrel", "juniper", "onyx", "saffron", "walrus", "domino", "pelican",
    "quartz", "thistle", "vertigo", "narwhal", "gumbo", "ripcord",
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
#: better: **this exception suppresses exactly ONE token** — the ``dont`` at
#: that index — and cannot mask a denial signal anywhere else, because the word
#: loop continues past it and the bigram loop has already run. So its
#: incompleteness has nothing to be incomplete ABOUT.
#:
#: That is the form a future exception should be argued in. Its one known miss,
#: "don't forget, on second thought skip it", contains no grammar word at all
#: and is the stated §3.7 residual, not a gap in this entry.
_DENIAL_EXCEPTIONS = frozenset({("dont", "forget")})

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
#: So ``wait`` denies unconditionally — and that is **correct behaviour, not a
#: tolerated false reject.** The reason is semantic rather than budgetary: the
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
        if pair in _DENIAL_EXCEPTIONS or trio in _DENIAL_BIGRAM_EXCEPTIONS:
            for offset in range(index, min(index + len(trio or pair), len(masked))):
                masked[offset] = ""

    for index in range(len(masked) - 1):
        if tuple(masked[index:index + 2]) in _DENIAL_BIGRAMS:
            return True
    return any(token in _DENIAL_WORDS for token in masked)


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
    def _quoted_frame(index: int) -> bool:
        return index > 0 and tokens[index - 1] == "say" and "approve" in tokens

    quoted = False
    for index in positions:
        rest = tokens[index + 1:]
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
        rest = tokens[index + 1:]
        if rest and rest[0] in NONCE_WORDS:
            return WRONG_NONCE
    return NO_MATCH


def matches_nonce(text: str, nonce: str) -> bool:
    """Convenience predicate: does *text* approve *nonce* outright?"""
    return classify(text, nonce) == APPROVED


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
    proposal, supplied by the client's ``response.done`` for that turn. It is
    ``None`` until then, and an unanchored proposal is not confirmable: at
    propose time the owner has not yet heard what they would be approving, and
    barge-in is native on WebRTC. Anchoring to the spoken turn rather than to
    the tool call is what makes "postdates the proposal" mean "after the owner
    heard it".

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
        return [
            *self.argv_prefix,
            render_body(
                self.instruction,
                self.request_utterance,
                self.id,
                reply_to=self._reply_target(),
            ),
        ]

    def _reply_target(self) -> str:
        """The sender name from the frozen argv — who a reply should address.

        Read from the frozen ``--from`` rather than passed separately, so the
        nudge can never name anyone other than the identity the message
        actually goes out under.
        """
        prefix = self.argv_prefix
        for index, token in enumerate(prefix[:-1]):
            if token == "--from":
                return prefix[index + 1]
        return ""


# =============================================================================
# Attribution rendering (spec §4b)
# =============================================================================

#: The at-a-glance attribution marker, and it goes FIRST in the body. See
#: :func:`render_body` for why the front position is load-bearing rather than
#: decorative.
VOICE_MARKER = "<voice>"

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
#: request] `` prefix and the ``⟨#id6⟩`` tail — ~57 chars for a long worktree
#: sender name (verified at 498 rendered with a 33-char sender, still hitting).
#: That lands a worst case near 385 against a measured 520, keeping ~35%
#: margin.
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

#: How much of the verbatim utterance survives into the rendered line.
MAX_UTTERANCE_CHARS = 90

#: How much of the instruction survives INTO THE RENDERED LINE. Distinct from
#: ``write_tools.MAX_INSTRUCTION_CHARS``, which bounds what the model may
#: propose at all; this one bounds what the recipient's pane has to render.
MAX_RENDERED_INSTRUCTION_CHARS = 160


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


def reply_nudge(reply_to: str) -> str:
    """The body's reply-path slot: the literal command that reaches the buddy.

    #962's live failure: the recipient answered a buddy request IN ITS OWN
    TERMINAL, and the reply never came back — the owner is listening, not
    watching that pane, so an on-screen answer is a lost one. ``--from buddy``
    and the ``<voice>`` marker say who asked; neither says how to answer. This
    slot does, as a runnable command rather than prose, because the recipient
    is an agent and the one thing it reliably does with a command is run it.

    The role text (worker/orchestrator) states the same etiquette; the slot is
    what covers recipients running with no agentwire role text at all.
    """
    return f'reply: agentwire msg send --to {_clip(reply_to, 40)} --kind done "<answer>"'


def render_body(
    instruction: str, request_utterance: str, proposal_id: str, *, reply_to: str = ""
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
    accident of layout.** ``instruction`` is model-supplied and ``_clip`` does
    not strip leading dashes, so a body starting with the instruction could
    reach the CLI as a FLAG rather than a value — this repo has shipped exactly
    that bug twice (see ``tools._SESSION_RE``'s comment, which records both).
    :data:`VOICE_MARKER` leads for attribution reasons, which happens to also
    guarantee this; the assertion below makes the guarantee explicit rather
    than incidental, so moving the marker fails loudly instead of silently
    re-opening the hole.

    Visible separators rather than newlines: scannable without being a wall,
    and without the wedging failure newlines cause.

    **The ``<voice>`` marker goes FIRST, and that placement is the whole of
    Slice 1's attribution.** §4 rules that ``--from buddy`` alone is not enough
    — a recipient must tell a buddy-originated request from a human-typed one
    without reading carefully. With ``--kind request`` (see write_tools for why
    the ``voice`` kind is deferred) the kind slot distinguishes nothing, so the
    only prefix-level distinguisher left would be exactly the sender string §4
    rejected. Putting the marker at the front of the BODY puts it in the same
    position on screen the kind slot would have occupied, and touches no shared
    code. Slice 1 does not claim kind-slot attribution; that arrives with 1b.
    """
    parts = [f"{VOICE_MARKER} {_clip(instruction, MAX_RENDERED_INSTRUCTION_CHARS)}"]
    if request_utterance.strip():
        parts.append(f"said: \"{_clip(request_utterance, MAX_UTTERANCE_CHARS)}\"")
    parts.append(f"#{proposal_id}")
    body = " ┃ ".join(parts)
    # The reply-path slot (#962) is DROPPABLE, whole-or-not-at-all: it rides
    # only when the full body still fits MAX_BODY_CHARS, and it slots in
    # BEFORE the id so the id is never what pays for it. Both halves priced:
    # included, it makes the reply path a runnable command; dropped, the cost
    # is a missing nudge — the role text still states the etiquette — never a
    # half-truncated command or a clipped id. A budget bump here would need
    # the pane re-measurement MAX_BODY_CHARS documents; a droppable slot does
    # not.
    if reply_to.strip():
        with_nudge = " ┃ ".join([*parts[:-1], reply_nudge(reply_to), parts[-1]])
        if len(with_nudge) <= MAX_BODY_CHARS:
            body = with_nudge
    body = _clip(body, MAX_BODY_CHARS)
    # Explicit, not incidental — see the docstring. A body reaching the CLI as
    # a flag is a bug this repo has shipped twice.
    assert not body.startswith("-"), "rendered body must never lead with a dash"
    return body


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
    # which is TRUE in exactly this state), and a response already in flight
    # BEFORE the announce must never defer its fallback — see client.py's
    # createAnnouncer: the timer is armed before anything that can fail, and
    # the one bounded deferral keys only on a response created AFTER the
    # announce, which can delay speech but never suppress it.
    "not_announced": (
        "Hang on — I haven't finished telling you what I'd send yet."
    ),
    # "already SENT" was the same over-claim §3.6 forbids on the success path,
    # which says "queued" precisely because msg send queues. A refusal may not
    # claim more certainty than the success it refers back to.
    "replayed": "I already passed that one on, so I'm not doing it again.",
    "refused": (
        "I didn't hear the confirmation phrase, so I haven't sent anything. "
        "Say confirm and then the word I gave you."
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
    "denied": (
        "I heard you hold off, so I haven't sent it. "
        "Say the phrase again when you're ready."
    ),
    "pending_transcript": (
        "Give me a second — I'm still catching up on what you said. Don't repeat it yet."
    ),
    "too_many_attempts": (
        "I've got that wrong too many times, so I've dropped it. Ask me again from the top."
    ),
    # Names the owner's next move AND the uncertainty, because the two are not
    # separable here and naming only one produces a different defect.
    #
    # "nothing was sent" was a definite claim the system cannot verify:
    # `run_agentwire_cmd` returns success=False on `subprocess.TimeoutExpired`
    # (mcp_core.py:150), and a timed-out CLI may already have enqueued. Pairing
    # that false certainty with "ask me again" invited a re-propose that
    # DOUBLE-DELIVERS — the acting-twice failure, reached through a spoken line
    # asserting more than the system knows.
    #
    # Note the next move CHANGES once the uncertainty is stated: verify-then-
    # decide, not re-propose. That is the honest instruction, and it is only
    # reachable by admitting what is unknown.
    "dispatch_failed": (
        "The handoff failed and I can't tell whether it went out. "
        "Check that session before asking me again."
    ),
}

#: Outcomes whose correct owner response is to WAIT rather than to speak again.
#: Named so the persona and the tests can both reason about it.
WAIT_OUTCOMES = frozenset({"pending_transcript", "not_announced"})

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
        "wrong_nonce", "quoted_frame", "denied", "pending_transcript",
        "too_many_attempts", "dispatch_failed",
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

    @property
    def spoken(self) -> str:
        if self.approved:
            return ""
        return SPOKEN.get(self.reason) or "I couldn't do that, so nothing was sent."

    def to_dict(self) -> dict:
        if self.approved:
            # "queued", never "sent" (§3.6). ``agentwire msg send`` QUEUES: the
            # CLI says so verbatim, delivery happens at the next safe boundary
            # and can defer behind the box gates. From the owner's ear, "I told
            # the orchestrator" followed by nothing is worse than a silent
            # refusal, because success was affirmatively claimed.
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
            }
        return {
            "success": False,
            "reason": self.reason,
            "say": self.spoken,
            "must_speak": True,
            "owner_should_wait": self.reason in WAIT_OUTCOMES,
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

    # -- propose ------------------------------------------------------------

    def propose(
        self,
        *,
        tool: str,
        session: str,
        instruction: str,
        argv_prefix: "list[str] | tuple[str, ...]",
        params: "dict | None" = None,
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
            proposal = Proposal(
                id=secrets.token_hex(3),
                token=secrets.token_urlsafe(18),
                nonce=nonce,
                tool=tool,
                session=session,
                instruction=instruction,
                argv_prefix=tuple(argv_prefix),
                created_at=self._clock(),
                params=dict(params or {}),
                request_utterance=request_utterance_from(self._ring),
            )
            self._proposals[proposal.token] = proposal
        return proposal

    def announce(self, proposal_id: str, seq: int) -> bool:
        """Anchor a proposal to the ``response.done`` in which it was SPOKEN.

        Called from the client once the buddy's spoken turn completes. Until
        this lands the proposal is not confirmable — see
        :attr:`Proposal.anchor_seq`.
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

        anchor = proposal.anchor_seq or 0
        # Snapshot the conversation's high-water mark BEFORE the await, so the
        # post-approval denial scan is bounded to what the owner had actually
        # said by the time this confirm started.
        ceiling = max(self._ring.high_seq, anchor)
        found = self._ring.await_utterance_after(anchor, self._wait_s)
        ceiling = max(ceiling, *(u.speech_started_seq for u in found)) if found else ceiling
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

        self._ring.spend(verdict.utterance_item_id)
        with self._lock:
            self._proposals.pop(token, None)

        argv = proposal.build_argv()
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
        """Retire a proposal without writing. Never gated — refusing is free."""
        with self._lock:
            self._proposals.pop(token, None)
        return Verdict(approved=False, reason="denied")

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
        # a different context. `ceiling` is the ring's high-water mark as of
        # this confirm's entry.
        later = [
            entry
            for entry in self._ring.after(
                match.speech_started_seq, include_spent=True
            )
            if entry.speech_started_seq <= ceiling
        ]
        if any(carries_denial(entry.text) for entry in later):
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
        )

    def _claim(self, token: str) -> "tuple[Proposal | None, Verdict | None]":
        with self._lock:
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
            if not proposal.announced:
                return None, Verdict(approved=False, reason="not_announced")
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
    "VOICE_MARKER",
    "WRONG_NONCE",
    "carries_denial",
    "classify",
    "matches_nonce",
    "mint_nonce",
    "normalize",
    "render_body",
    "reply_nudge",
    "request_utterance_from",
    "spoken_nonce",
]
