"""The transcript ring, ordered by CONVERSATION-ITEM time (spike).

This is the foundation the confirm gate stands on, and getting its clock right
is the whole of finding Rv2.

Why not wall-clock, and why that is not a tolerance problem
-----------------------------------------------------------

The obvious design stamps each utterance when the bridge *receives* the
forwarded ``conversation.item.input_audio_transcription.completed``. That is
when transcription **finished**, not when the audio was **spoken**, and the gap
between them is exactly the transcription latency the whole timing hazard is
about. An utterance spoken BEFORE a proposal but transcribed AFTER it stamps as
postdating it — **the predicate silently inverts**.

Widening a tolerance does not fix that, because the two sides are not the same
quantity: a receipt time compared against an intent time. The fix is to compare
two quantities that ARE the same, and the realtime session already provides one
— the order of items on the data channel.

So the ring is ordered by a **logical clock**: a monotonically increasing
sequence the client assigns in data-channel event order (see
``client.py``'s ``nextSeq``). Both sides of the comparison come from that one
ordered stream:

- an utterance is stamped at its ``input_audio_buffer.speech_started``;
- a proposal is anchored at the ``response.done`` of the turn in which the buddy
  SPOKE it (see ``confirm.Proposal.anchor_seq``).

"The approval postdates the proposal" then means
``speech_started_seq > anchor_seq``: one integer comparison, on one clock, in
conversation order. No skew, no latency, nothing to tune.

**Why speech-START and not the audio commit.** This is subtle and the obvious
choice is wrong in a way that reopens the exact hole the clock change exists to
close. ``input_audio_buffer.committed`` fires at the **end** of an utterance.
The barge-in case is the owner beginning to speak DURING the buddy's proposal
and finishing after it — so speech-start predates the proposal's
``response.done`` while the commit postdates it. Ordering on commit therefore
**approves the barge-in**: an approval for a proposal the owner never heard
stated. Speech-start is the intent time; the commit is not.

The commit event is still needed, for a different job: it is what binds the
``item_id`` used by the transcript event, and it is recorded (``commit_seq``) so
the ordering choice can be inspected rather than assumed. Both times live on the
entry; only ``speech_started_seq`` gates.

Ordering of the two forwards
----------------------------

The commit event and the transcript for the same ``item_id`` arrive as two
separate ``POST /utterance`` calls, and the confirm arrives as a third
(``POST /tool``). The client awaits the transcript forward before dispatching
any function call (Rv2c), but the bridge must not *depend* on that: a commit
that has not arrived yet leaves the entry absent rather than mis-stamped, and a
transcript arriving with no prior ``speech_started`` is recorded ``estimated``
and is never usable as an approval. (The COMMIT is not what decides that, and
saying so here described a gate this module deliberately does not have —
:meth:`TranscriptRing.transcribe` flags ``estimated`` on a missing
``speech_started_seq``, because that is the one the ordering predicate reads.)
Failing closed there is deliberate — if the ``speech_started`` events stopped
arriving, confirms stop working loudly rather than silently losing their
ordering guarantee.

Thread safety
-------------

The bridge is a ``ThreadingHTTPServer`` and the ring is shared mutable state
across request threads (Rv2c). Every read and write holds the lock. The lock is
also the mechanism for :meth:`TranscriptRing.await_utterance_after`: a ``/tool``
thread evaluating a confirm blocks on the condition until an ``/utterance``
thread notifies it. That is not a workaround for threading — it is how the
bounded await in §3.3 is implemented at all.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

#: How many utterances to keep. Minutes of conversation, and small enough that
#: the ring never becomes a transcript store. It is not a log — nothing here is
#: persisted, deliberately.
DEFAULT_CAPACITY = 32


@dataclass
class Utterance:
    """One thing the owner said, as the TRANSCRIPTION model rendered it.

    Two logical times, and only one of them gates:

    - ``speech_started_seq`` — when the owner BEGAN speaking. This is the intent
      time and the only thing the ordering predicate reads. See the module
      docstring for why the commit is the wrong choice here.
    - ``commit_seq`` — when the audio buffer closed. Recorded for inspection and
      for binding the transcript, never compared against a proposal.

    ``spent`` marks an entry already used to approve something, so one approval
    cannot satisfy a second proposal — the "acting twice" failure §4 names.
    """

    item_id: str
    speech_started_seq: int = 0
    commit_seq: int = 0
    text: str = ""
    estimated: bool = False
    spent: bool = False
    received_at: float = 0.0

    @property
    def complete(self) -> bool:
        return bool(self.text.strip())

    @property
    def ordered(self) -> bool:
        """Can this entry be placed in conversation order at all?

        False when no ``speech_started`` was seen. Such an entry is refused as
        an approval: guessing its position is what the whole clock change
        exists to stop.
        """
        return self.speech_started_seq > 0 and not self.estimated


class TranscriptRing:
    """A short, locked, in-memory ring of the owner's recent utterances."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, clock=time.monotonic):
        self._capacity = capacity
        self._clock = clock
        self._condition = threading.Condition()
        self._items: list[Utterance] = []
        #: Highest sequence the bridge has seen from ANY client event. Used to
        #: anchor a proposal when the client's own anchor has not arrived yet,
        #: never to order an utterance.
        self._high_seq = 0

    # -- writing ------------------------------------------------------------

    def speech_started(self, item_id: str, seq: int) -> Utterance:
        """Record that the owner BEGAN speaking item *item_id* at logical *seq*.

        This is the timestamp the gate orders on. Idempotent, and a repeated
        event keeps the FIRST sequence — the first one is closest to the actual
        onset of speech, which is the quantity being measured.
        """
        with self._condition:
            self._high_seq = max(self._high_seq, seq)
            entry = self._find(item_id)
            if entry is None:
                entry = Utterance(item_id=item_id, received_at=self._clock())
                self._append(entry)
            if not entry.speech_started_seq:
                entry.speech_started_seq = seq
            return entry

    def commit(self, item_id: str, seq: int) -> Utterance:
        """Record the audio-commit boundary for *item_id* at logical time *seq*.

        Recorded, never compared: the commit is the END of the utterance, and
        ordering on it approves the barge-in case (see the module docstring).
        Its job is binding the item and making the ordering choice inspectable.
        """
        with self._condition:
            self._high_seq = max(self._high_seq, seq)
            entry = self._find(item_id)
            if entry is None:
                entry = Utterance(item_id=item_id, received_at=self._clock())
                self._append(entry)
            if not entry.commit_seq:
                entry.commit_seq = seq
            return entry

    def transcribe(self, item_id: str, text: str, seq: int = 0) -> Utterance:
        """Attach the transcription model's text to *item_id*.

        An id with no prior :meth:`speech_started` is admitted but flagged
        ``estimated``: its position in the conversation is unknown, so the gate
        refuses it. It is still recorded, because a refusal that can point at
        what it heard is better than one that cannot.
        """
        with self._condition:
            if seq:
                self._high_seq = max(self._high_seq, seq)
            entry = self._find(item_id)
            if entry is None:
                entry = Utterance(
                    item_id=item_id, estimated=True, received_at=self._clock()
                )
                self._append(entry)
            if not entry.speech_started_seq:
                entry.estimated = True
            entry.text = text
            self._condition.notify_all()
            return entry

    def note_seq(self, seq: int) -> int:
        """Record a sequence observed on the channel; return the running high."""
        with self._condition:
            self._high_seq = max(self._high_seq, seq)
            return self._high_seq

    def reserve_epoch(self, gap: int, ceiling: int) -> int:
        """Claim an exclusive block of *gap* sequences. ONE lock, atomically.

        This is what ``/mint`` hands a new page as its clock origin, and the
        atomicity is the whole point rather than a nicety. ``high_seq`` read
        and then ``note_seq`` written is TWO acquisitions with a window
        between them, and two concurrent mints on the bridge's
        ``ThreadingHTTPServer`` can both read the same high and both be given
        the same base — which is precisely the "second tab, two interleaved
        counters" case the epoch exists to rule out, reintroduced inside the
        fix for it. It does not reproduce under ordinary threading (the window
        is a couple of bytecodes) and that is not the standard here: single-use
        in :class:`~agentwire.voice_layer.confirm.ConfirmSpine` is a property
        of its claim rather than of its timing for the same reason.

        Returns 0 — never a usable base — when the reservation would cross
        *ceiling*. The number leaves Python for the page's own counter, where
        past 2**53 an increment silently stops advancing, so exhaustion has to
        be an error the page refuses on rather than a number it counts from.

        A BLOCK, and the ceiling test says so: the page counts UP from its
        base, so what has to fit under *ceiling* is ``base + gap``, not
        ``base``. Testing the base alone let the final reservation land exactly
        on the ceiling — a page that mints successfully and then has zero
        usable sequences, every forward silently refused. Unreachable in
        practice (~35 million mints on one bridge process) and that is not the
        point: the sentence above claims a block, so the code has to reserve
        one.
        """
        with self._condition:
            base = self._high_seq + gap
            if base + gap > ceiling:
                return 0
            self._high_seq = base
            return base

    @property
    def high_seq(self) -> int:
        with self._condition:
            return self._high_seq

    def spend(self, item_id: str) -> None:
        """Mark an entry consumed so it can never approve a second proposal."""
        with self._condition:
            entry = self._find(item_id)
            if entry is not None:
                entry.spent = True

    # -- reading ------------------------------------------------------------

    def after(self, seq: int, *, include_spent: bool = False) -> list[Utterance]:
        """Completed, unspent utterances whose SPEECH BEGAN strictly after *seq*.

        Strictly after: an utterance sharing a sequence with the proposal's
        anchor cannot be proven to follow it, and the gate's job is to refuse
        what it cannot prove.
        """
        with self._condition:
            return self._after_locked(seq, include_spent)

    def await_utterance_after(
        self, seq: int, timeout: float
    ) -> "list[Utterance]":
        """Block up to *timeout* seconds for an utterance beginning after *seq*.

        Returns immediately if one is already present, and returns whatever
        exists at the deadline — possibly empty. An empty result is a DIFFERENT
        refusal from "a transcript arrived and did not match" (see the outcome
        taxonomy in :mod:`~agentwire.voice_layer.confirm`), and the caller must
        keep them apart because the owner's correct next move is opposite.
        """
        deadline = self._clock() + timeout
        with self._condition:
            while True:
                found = self._after_locked(seq, False)
                if found:
                    return found
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return []
                self._condition.wait(remaining)

    def unheard_between(self, after: int, ceiling: int) -> list[Utterance]:
        """Ordered utterances in ``(after, ceiling]`` with NO transcript yet.

        The denial half of the timing asymmetry the bounded await fixes for
        approvals. An utterance whose ``speech_started`` was recorded but whose
        transcript has not landed is invisible to :meth:`after` — it filters on
        ``complete`` — so a denial spoken after the approval and not yet
        transcribed did not block the write. The system already KNOWS the owner
        spoke again (the sequence advanced); it just cannot yet say what they
        said. That is ``pending_transcript``, never approval.
        """
        with self._condition:
            return [
                u
                for u in self._items
                if not u.complete
                and u.speech_started_seq > after
                and u.speech_started_seq <= ceiling
            ]

    def snapshot(self) -> list[Utterance]:
        with self._condition:
            return list(self._items)

    # -- internals ----------------------------------------------------------

    def _find(self, item_id: str) -> "Utterance | None":
        for entry in self._items:
            if entry.item_id == item_id:
                return entry
        return None

    def _append(self, entry: Utterance) -> None:
        self._items.append(entry)
        if len(self._items) > self._capacity:
            del self._items[: len(self._items) - self._capacity]

    def _after_locked(self, seq: int, include_spent: bool) -> list[Utterance]:
        # speech_started_seq, NOT commit_seq. Ordering on the commit approves
        # the barge-in case — see the module docstring.
        return [
            u
            for u in self._items
            if u.complete
            and u.speech_started_seq > seq
            and (include_spent or not u.spent)
        ]
