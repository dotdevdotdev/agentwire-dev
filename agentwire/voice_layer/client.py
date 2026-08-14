"""The buddy's browser client — WebRTC to OpenAI Realtime (spike).

Embedded as a string rather than shipped as a static file on purpose: adding it
to ``agentwire/static/`` would put it one import away from the portal, which is
exactly the coupling a beta-gated feature must not create — a gate the portal
reaches around is not a gate. No packaging change, no portal change.

The flow, per the current Realtime docs:

1. POST ``/mint`` here → ephemeral client secret (the API key stays server-side).
2. ``getUserMedia`` → ``RTCPeerConnection`` with the mic track added, plus an
   ``oai-events`` data channel for the JSON event stream.
3. POST the SDP offer to ``https://api.openai.com/v1/realtime/calls`` with the
   client secret as bearer; set the answer as the remote description.
4. On ``response.done``, any ``function_call`` items go to ``/tool`` here, and
   the result goes back as ``conversation.item.create`` +
   ``function_call_output``, then ``response.create``.

Two guards carried over from DocumentScribe's implementation, both of which they
paid for in bugs:

- **``responseActive``** — the server rejects a ``response.create`` while a
  response is in flight. ``server``/``semantic`` VAD fires its own responses, so
  ours must not race them.
- **Sequential tool dispatch** — one response can carry several
  ``function_call`` items; firing them concurrently makes their
  ``response.create`` calls race each other. Await each in turn.

Barge-in needs no code: the model is full-duplex, and ``echoCancellation`` on
the mic handles local speaker feedback the way any WebRTC call does.

Two things this page owns that the bridge cannot
------------------------------------------------

**1. Conversation-item time.** The confirm gate's ordering predicate ("the
approval postdates the proposal") is only well-defined on a single clock, and
the only single clock available is the ORDER OF EVENTS ON THIS DATA CHANNEL.
Wall-clock cannot work: the bridge can only stamp a transcript when
transcription *finished*, which is after the audio was *spoken* by exactly the
transcription latency the hazard is about — so an utterance spoken before a
proposal but transcribed after it would stamp as postdating it, and the
predicate silently inverts. So this page assigns a monotonic ``nextSeq()`` in
event order and stamps both sides from it:

- an utterance at ``input_audio_buffer.speech_started`` (the INTENT time, not
  the audio boundary — the commit fires at the END of an utterance, so ordering
  on it approves the barge-in case the gate exists to refuse; see
  :mod:`~agentwire.voice_layer.transcript`). The commit is recorded too, and
  never compared;
- a proposal at the moment there is POSITIVE EVIDENCE its announcement was
  spoken — ``onSpoken``, whose ``how`` is ``"model"`` (a ``response.done`` whose
  transcript carried the text) or ``"fallback"`` (the browser voice said it).
  Either way the owner heard it, which is the quantity being stamped. Barge-in
  is native here, so anchoring at tool-call time would let an interrupting
  approval land on a proposal that was never stated.

  **#951 retired an earlier wording here, and the implementation under it:**
  "the ``response.done`` of the turn in which the buddy spoke it". Read as *the
  next ``response.done`` carrying any text* it let the announcer's
  own cancel steal the anchor, and it has no reading at all for the fallback
  path, which produces no model turn. A fallback-spoken proposal was then
  anchored by nothing and the owner's correct nonce got ``not_announced`` until
  the TTL — the fallback that GUARANTEES speech becoming the reason nothing
  could be approved.

The transcript forward is **awaited before any function call dispatches**
(``forwardChain``). Without that the two are independent ``fetch`` calls racing
each other, and tool dispatch is already detached from event ordering.

**2. Making a refusal audible.** Returning a reason string does not make the
model say it — a ``function_call_output`` is context, and the model then says
whatever it says. Worse, there is a path where nothing is generated at all:
``maybeCreateResponse`` declines while a response is in flight, so the output
lands and no response is created. That is not the unlucky path for a timing
refusal, it is the *likely* one, because a timing refusal fires exactly when the
owner has just stopped speaking and VAD is producing its own responses.

So refusals go through :js:func:`createAnnouncer` — cancel the in-flight
response, ``response.create`` with scripted instructions, verify against the
following ``response.done``, and fall back to ``window.speechSynthesis`` if no
spoken confirmation lands. **A refusal that always speaks in a robot voice beats
one that usually speaks in a nice one**; that fallback is what makes "silence is
unacceptable" structurally true instead of aspirational.

The announcer is kept as a standalone factory with injected ``send``/``speak``/
timer so it can be exercised under ``node`` against a fake data channel — the
acceptance criterion is about what reaches the CHANNEL, and a test that asserts
on a Python return value is green in exactly the scenario this exists to
prevent.
"""

from __future__ import annotations

import html
import json

from . import realtime

#: The announcer, kept separate from the page so tests can run it under ``node``
#: with a fake ``send``/``speak``/timer and assert on the DATA CHANNEL. Spliced
#: into the page by :func:`page`; also exported by :func:`announcer_source` for
#: the test harness. Plain ES5-ish function syntax, no modules — it has to work
#: both inside a ``<script>`` tag and under a bare ``node -e`` eval.
ANNOUNCER_JS = """
// Makes a refusal AUDIBLE. See the module docstring for why returning a reason
// string does not achieve that, and for the confirmed silent branch this
// replaces.
//
// Injected dependencies rather than globals, so this is testable against a fake
// channel: `send(event) -> bool`, `speak(text)` (the non-model fallback),
// `setTimer(fn, ms) -> handle`, `clearTimer(handle)`.
function createAnnouncer(deps) {
  var send = deps.send;
  var speak = deps.speak;
  // Cuts the fallback voice's AUDIO, immediately (#1041). speak() starts an
  // utterance this page cannot otherwise recall — speechSynthesis is outside
  // the WebRTC path, so the model's native barge-in never touches it, and an
  // owner talking over a fallback-spoken notice was talked straight through.
  var stopSpeak = deps.stopSpeak || function () {};
  var setTimer = deps.setTimer;
  var clearTimer = deps.clearTimer;
  var onLog = deps.onLog || function () {};
  // Called with (meta, how) the moment there is EVIDENCE the text was spoken —
  // "model" when a response.done carried it, "fallback" when the browser voice
  // said it. This is what the proposal anchor is driven from: see the client's
  // onSpoken handler and BLOCKING 2 in the phase-2 review.
  var onSpoken = deps.onSpoken || function () {};
  // The other half of onSpoken, and it did not exist: speechSynthesis reports
  // failure through `onerror` and the client logged it without telling anyone,
  // so an announcement that was neither heard NOR acked stayed suppressed for
  // the rest of the session (#978 item 5). Called with the meta of an item
  // there is positive evidence was NOT spoken. Never called speculatively —
  // "we cannot know" is the timer's job, not this one's.
  var onNotSpoken = deps.onNotSpoken || function () {};
  // Whether the OWNER is speaking right now. The gate check happens when an
  // announcement is queued; the fallback speaks 6-12s later, and until this
  // dep existed the timer could not re-check (#978 item 3).
  var ownerSpeaking = deps.ownerSpeaking || function () { return false; };
  // How long to wait for the model to actually say it before falling back to
  // the browser's own speech synthesis. Generous enough for a normal spoken
  // turn, short enough that the owner is not left in silence wondering.
  var fallbackMs = deps.fallbackMs || 6000;
  // How many times the fallback may stand down for an owner who is still
  // talking. BOTH halves priced: without a bound, a long monologue swallows a
  // refusal entirely, which is the one outcome the default-on timer exists to
  // rule out — talking over the owner once beats never telling them.
  //
  // The worst case is fallbackMs * (2 + maxOwnerDeferrals), not (1 + …): this
  // deferral is checked FIRST and re-arms, so the one-shot in-flight deferral
  // below is still available on the re-armed timer and the two STACK. That is
  // correct — they answer different questions ("is the owner talking" and "is
  // this our own audio still playing"), and sharing a budget would let a
  // monologue consume the grace that stops the buddy speaking over itself —
  // but it makes the bound 5 intervals, and the arithmetic is now pinned by a
  // test rather than asserted here.
  var maxOwnerDeferrals =
    deps.maxOwnerDeferrals === undefined ? 3 : deps.maxOwnerDeferrals;
  // How long the browser voice may be believed to still be talking with no
  // end event. speechSynthesis can drop an utterance without firing `onend`
  // OR `onerror`, and `speaking` below gates volunteering — so without this
  // the false-reject half is an UNBOUNDED mute, which is strictly worse than
  // the interjection it prevents.
  //
  // SCALED BY LENGTH, because a flat bound is not "long enough for any real
  // utterance" and saying so was false: composeNotice coalesces up to 240
  // characters PER MESSAGE, so a three-reply batch is around a minute of
  // speech and a five-reply batch several. A flat 30s watchdog fires
  // mid-utterance, and what that costs names exactly what this budget rules
  // out: `speaking` is what pending() and anchorPending() count, so an early
  // fire empties it and reopens the NOTIFIER's gates — canSpeak and
  // canInterrupt — letting a volunteered notice or an escalation be announced
  // over the browser voice.
  //
  // What the scaling itself does not rule out — and what now does. The budget
  // gates pending()/anchorPending(), which are the NOTIFIER's gates; it said
  // nothing about the announcer's own FIFO. armFallback nulls `current`,
  // starts speak(), and calls pump() in the SAME tick, so a second must_speak
  // item queued behind a long notice was promoted and its response.create went
  // out while the browser voice was starting the first one's audio: two
  // voices, at any watchdog length, reached without the watchdog firing at all
  // (#997). pump() now defers while `speaking` is non-empty — see the bound
  // there, which is this same budget rather than a new number, because this is
  // exactly how long the page is willing to BELIEVE the browser voice is
  // talking.
  //
  // The per-character rate is deliberately SLOWER than any real voice (~7
  // characters a second against a typical 15). The two directions are not
  // symmetric: over-estimating only delays a backstop that matters solely
  // when the browser has already silently dropped the utterance, while
  // under-estimating produces the overlap on every ordinary long notice.
  var speakingBaseMs = deps.speakingMaxMs || fallbackMs * 5;
  var speakingMsPerChar =
    deps.speakingMsPerChar === undefined ? 140 : deps.speakingMsPerChar;
  function speakingBudget(text) {
    return speakingBaseMs + String(text || "").length * speakingMsPerChar;
  }

  var queue = [];
  // { text, fallbackText, meta, timer, speakTimer, sawCreate, deferred,
  //   ownerDeferrals }
  var current = null;
  var responseActive = false;
  //: Items whose FALLBACK AUDIO has started and not yet ended (review F4).
  //: The real speak() is asynchronous — onSpokenAloud runs from
  //: utterance.onend — and armFallback nulls `current` before calling it, so
  //: between those two moments the announcer reported nothing pending at all:
  //: canInterrupt passed and an escalation went out while the browser voice
  //: was still saying "...say confirm tango". The unit fixture called back
  //: synchronously, so that window had zero width and nothing could see it.
  var speaking = [];

  // Function words. They carry nothing about WHETHER the reason was stated,
  // and they dominate a short conversational line — the greeting is 7 of its
  // 9 tokens — so an unrelated reply that happened to share them scored a
  // disarm (#978 item 7). Closed and small on purpose: this is a weighting,
  // not a language model, and every word added to it is one the model no
  // longer has to say.
  var STOPWORDS = {};
  ("a about all am an and any are as at be been but by can could did do does " +
   "for from had has have he her him his how i if in is it its just me my no " +
   "not of on or our out over re s she so some t than that the their them " +
   "then there these they this to up us ve was we were what when which who " +
   "why will with would you your m ll d o").split(" ").forEach(function (w) {
    STOPWORDS[w] = true;
  });

  // The model is told to say it exactly, but "exactly" is prompt compliance and
  // prompt compliance is not a mechanism — so verification is a word-overlap
  // test rather than an equality test. A paraphrase that carries most of the
  // reason has reached the owner's ear, which is the actual requirement; a
  // response about something else has not.
  function carriedTheReason(transcript, text) {
    if (!transcript) return false;
    var norm = function (s) {
      return String(s).toLowerCase().replace(/[^a-z0-9 ]+/g, " ").split(/\\s+/).filter(Boolean);
    };
    var uniq = function (list) {
      var out = [], seenWord = {};
      list.forEach(function (w) {
        if (!seenWord[w]) { seenWord[w] = true; out.push(w); }
      });
      return out;
    };
    var all = norm(text);
    if (!all.length) return true;
    // What must be echoed back: UNIQUE CONTENT words. Two fixes, one defect
    // each (#978 item 7). Stopwords out, because a stopword-heavy script is
    // matched by almost any sentence — the greeting's disarm was decided by
    // "what's on your mind" and nothing else, so the greet-as-health-check
    // reported the model-audio path healthy when the greeting never happened.
    // Deduplicated, because the overlap counted repeats, so a reply saying one
    // shared word eight times could carry the score on its own.
    //
    // Which way this errs, deliberately: a paraphrase that drops content words
    // now falls through to the browser voice, so the owner hears it TWICE.
    // Double-speak is the cheap failure here and believed-spoken silence is
    // the expensive one, so the threshold stays where it was and only the
    // thing being counted changed.
    var want = uniq(all.filter(function (w) { return !STOPWORDS[w]; }));
    // A line that is nothing BUT stopwords ("What is it about?") would
    // otherwise have nothing left to compare and could never disarm — an
    // unconditional double-speak on every such line. Compare its own tokens.
    if (!want.length) want = uniq(all);
    var got = {};
    norm(transcript).forEach(function (w) { got[w] = true; });
    var hits = want.filter(function (w) { return got[w]; }).length;
    return hits / want.length >= 0.6;
  }

  // THE FALLBACK IS ARMED BY A TIMER, NOT TRIGGERED BY A DETECTED FAILURE.
  //
  // This is the part that decides whether "silence is unacceptable" is true or
  // merely intended. Every way this announcement can be lost is invisible from
  // here:
  //
  //   - `responseActive` is a CLIENT-SIDE MIRROR of server state and is stale
  //     by construction. If we read it false and skip the cancel while the
  //     server has just started a VAD-driven response, the server REJECTS the
  //     overlapping response.create and the announcement is dropped
  //     server-side — with our own state reporting success.
  //   - `send()` is fire-and-forget over a data channel; nothing correlates a
  //     later `error` event with a specific create.
  //
  // So any design that routes the fallback through *detecting* failure leaks
  // exactly the cases that matter. Instead: speech is the DEFAULT, and only
  // positive evidence suppresses it — a response.done whose transcript
  // actually carries the reason. Default-on, disarmed by success. Never
  // "on failure, speak".
  function armFallback(item) {
    item.timer = setTimer(function () {
      // NEVER OVER THE OWNER — re-checked HERE, not only at gate time. The
      // gate that promised this ran when the announcement was queued; this
      // fires 6-12s later, and the injected deps did not expose the signal at
      // all, so the promise held for exactly the moment nobody was speaking
      // (#978 item 3). Bounded, and the bound is the point: a fallback that
      // waits for silence forever is a refusal the owner never hears.
      if (ownerSpeaking() && item.ownerDeferrals < maxOwnerDeferrals) {
        item.ownerDeferrals += 1;
        onLog("fallback", "deferred — the owner is speaking (" +
          item.ownerDeferrals + "/" + maxOwnerDeferrals + ")");
        armFallback(item);
        return;
      }
      // ONE bounded deferral, on one narrow signal: a response was CREATED
      // after our announce went out and has not finished. That response is
      // plausibly the model speaking this very announcement, still mid-audio
      // — firing now is the two-voices defect (#950 defect 1). Deferring can
      // only DELAY speech, never suppress it: the flag is per-item, checked
      // once, and the re-armed timer speaks unconditionally. A response
      // created BEFORE the announce (the not_announced recursion's state)
      // never defers — sawCreate is only set while this item is current.
      if (item.sawCreate && !item.deferred) {
        item.deferred = true;
        onLog("fallback", "deferred once — a response is mid-flight");
        armFallback(item);
        return;
      }
      onLog("fallback", "no spoken confirmation within " + fallbackMs + "ms");
      if (current === item) current = null;
      // The owner DID hear it — in a robot voice, but they heard it. Anything
      // keyed on "was this spoken" (the proposal anchor) must be told so here,
      // or the fallback that GUARANTEES speech becomes the reason a proposal
      // is never anchored and the owner's correct nonce is refused forever.
      //
      // What this channel UTTERS is `fallbackText` when the payload carries
      // one. speechSynthesis is outside the WebRTC path, so echo cancellation
      // does not cover it — its audio can re-enter the mic and land in the
      // USER transcript. A payload whose spoken text must not be echoable
      // into an approval (a proposal carrying its nonce) supplies a
      // fallback-safe variant; everything else falls through to `text`.
      //
      // And the failure leg, which did not exist: speechSynthesis reports
      // `onerror` and the page merely logged it, so an announcement that was
      // demonstrably NOT spoken was also never released — its id sat in the
      // notifier's inFlight map and suppressed every later tick for the rest
      // of the session (#978 item 5). A throw from speak() is different: it
      // means we cannot know, and the existing "assume heard" reading is the
      // safe one there — claiming not-spoken would replay a notice the owner
      // may well have heard.
      var say = item.fallbackText || item.text;
      // The buddy is SPEAKING from here until an end event says otherwise —
      // armed before speak(), because a synchronous callback would otherwise
      // clear a flag that had not been set yet. Watchdogged: an utterance
      // dropped with no onend/onerror would leave the gates shut forever.
      speaking.push(item);
      var budget = speakingBudget(say);
      // Kept on the item because pump()'s deferral bound reads it: the
      // longest this page will believe this utterance is playing is the same
      // number that decides when to stop believing it (#997).
      item.speakBudget = budget;
      item.speakTimer = setTimer(function () {
        onLog("fallback", "no end event within " + budget + "ms");
        // NOT SPOKEN — and saying so is the fix for #996. The watchdog used to
        // call stopSpeaking() alone: it reopened the gates (its stated scope,
        // done correctly) but released nothing, so the ids stayed in the
        // notifier's inFlight map for the life of the page. Since #970 that is
        // no longer data loss — nothing announced is cursor-past, so a reload
        // recovers it — but a permanently-inFlight id wedges the contiguity
        // walk, so everything after it is spoken and never acked and a reload
        // REPEATS it. Suppression until reload, then duplicates.
        //
        // Both halves, since this is the announcer deciding the owner did not
        // hear something.
        //
        // FALSE-REJECT — a slow but LIVE utterance declared dropped — costs
        // more than "the owner hears it twice", and the cheap phrasing hid it:
        // stopSpeaking empties `speaking` but CANNOT cancel the browser's
        // audio (there is no cancel() on this path, deliberately — #950 defect
        // 3), so the re-announcement pumps while the first utterance is still
        // playing. The owner hears it twice SIMULTANEOUSLY, for whatever is
        // left of the utterance. That reopens #997 for exactly that window, on
        // purpose: overlapping speech the owner can still parse beats a notice
        // they never get. What keeps the window rare rather than routine is
        // the #993 budget, measured conservative by 2.6-4.5x against every
        // plausible macOS system voice.
        //
        // FALSE-ACCEPT — staying silent — costs the notice until a reload the
        // owner has no way to know they need. Double-speak is still the cheap
        // failure here, which is the same trade carriedTheReason makes.
        settleSpeech(item, false);
      }, budget);
      try {
        speak(
          say,
          function () { settleSpeech(item, true); },
          function () { settleSpeech(item, false); }
        );
      } catch (e) { settleSpeech(item, true); }
      pump();
    }, fallbackMs);
  }

  function disarm(item) {
    if (item && item.timer) { clearTimer(item.timer); item.timer = null; }
  }

  // The browser voice for this item is over — by its own end event, by its
  // error, or by the watchdog. Idempotent: all three can be reached, and only
  // the first one that arrives means anything.
  function stopSpeaking(item) {
    if (item && item.speakTimer) {
      clearTimer(item.speakTimer);
      item.speakTimer = null;
    }
    speaking = speaking.filter(function (it) { return it !== item; });
  }

  // The one place a fallback utterance gets an OUTCOME, and exactly once.
  //
  // Three callers can reach this for the same item — utterance.onend,
  // utterance.onerror, and the watchdog — and until #996 only the first two
  // reported anything, so the third was silent. Making the watchdog report
  // makes the LATCH load-bearing rather than tidy: without it a watchdog
  // firing at the budget and a late onend arriving afterwards would release
  // the ids (re-announce) and then ack them (mark heard), in that order, which
  // is a notice announced twice and acked once. First outcome wins; the rest
  // are the same utterance being described again.
  function settleSpeech(item, spoken) {
    if (!item || item.speechSettled) return;
    item.speechSettled = true;
    stopSpeaking(item);
    if (spoken) onSpoken(item.meta, "fallback");
    else onNotSpoken(item.meta);
    // The queue was waiting on this audio (#997) — promote now rather than on
    // the deferral's backstop timer.
    pump();
  }

  // #997's deferral. Cleared whenever the pump actually promotes, and on
  // teardown.
  var pumpDeferTimer = null;

  function releasePump() {
    if (pumpDeferTimer) { clearTimer(pumpDeferTimer); pumpDeferTimer = null; }
  }

  // How long the pump may wait for the browser voice: the budget of the
  // utterance actually in flight, never a new constant. See pump().
  function pumpBound() {
    var bound = speakingBaseMs;
    speaking.forEach(function (it) {
      if (it.speakBudget > bound) bound = it.speakBudget;
    });
    return bound;
  }

  // `force` is the bound expiring, and nothing else sets it.
  function pump(force) {
    if (current || !queue.length) return;
    // NEVER OVER THE BUDDY'S OWN VOICE (#997). The budget above gates
    // pending()/anchorPending() — the NOTIFIER's gates — and said nothing
    // about this FIFO, so an item queued behind a long fallback utterance was
    // promoted in the same tick that utterance started and its response.create
    // went out over it: the two-voices defect (#950), reached with no watchdog
    // fire and no gate violated.
    //
    // BOUNDED, and the bound is the whole design: an unbounded defer converts
    // an audio-quality defect into a SUPPRESSION defect, which is strictly
    // worse in a screenless channel. Both halves priced:
    //
    //   false-accept (waiting too long): the queued item is delayed behind
    //     audio the owner IS CURRENTLY HEARING. Not silence — the buddy is
    //     talking the whole time — and it ends when that audio does, because
    //     settleSpeech pumps.
    //   false-reject (promoting too early): two voices at once, which is the
    //     defect this exists to stop.
    //
    // So the bound is the SPEAKING BUDGET of the utterance in flight (#993:
    // 30s floor + 140ms/char), not a new number — that is precisely how long
    // this page is willing to BELIEVE the browser voice is talking. Past it
    // the watchdog has already declared the utterance dropped (#996), emptied
    // `speaking` and pumped; this timer is the backstop for the case where it
    // somehow has not, and it promotes rather than staying mute. The worst
    // case is therefore one budget, and it is bounded even if the watchdog is
    // broken.
    //
    // WHAT THIS DELAY IS UPSTREAM OF, said here because the bound it adds is
    // stated in another file: the announcer's own two bounded deferrals are
    // counted from the moment an item becomes `current`, and confirm.py's
    // not_announced note counts THIS deferral on top of that arithmetic — one
    // speaking budget in front of the fallbackMs intervals, in exactly the
    // state where a fallback utterance is live (#1009). The trade is still
    // the right one — the owner is listening to the buddy for the whole wait
    // rather than sitting in silence, so the deferral extends the wait and
    // never the silence. Change this bound and that note's pins fail with it:
    // the note derives its numbers from these constants.
    if (speaking.length && !force) {
      if (!pumpDeferTimer) {
        var bound = pumpBound();
        onLog("pump", "deferred — the browser voice is speaking (bound " +
          bound + "ms)");
        var handle = setTimer(function () {
          // Only the LIVE deferral may force. A cleared timer that fires
          // anyway — a fake-timer harness, a browser running a just-cancelled
          // callback — must not promote over a voice nothing is waiting on.
          if (pumpDeferTimer !== handle) return;
          pumpDeferTimer = null;
          onLog("pump", "deferral bound reached — promoting anyway");
          pump(true);
        }, bound);
        pumpDeferTimer = handle;
      }
      return;
    }
    releasePump();
    current = queue.shift();
    var item = current;

    // Armed FIRST, before anything that could fail silently.
    armFallback(item);

    // Cancel ONLY when our mirror says a response is active. The mirror is
    // stale by construction, and both stale directions are priced: stale-true
    // sends a cancel with nothing active (the server errors, and the client
    // suppresses that specific error rather than announcing it); stale-false
    // skips the cancel, our create is rejected server-side, and the TIMER
    // still speaks — nothing here needs to succeed for the owner to be told.
    // The unconditional cancel was one edge of a closed loop: cancel with
    // nothing active → error event → spoken error notice → another cancel
    // (#950 defect 2).
    if (responseActive) send({ type: "response.cancel" });

    send({
      type: "response.create",
      response: {
        instructions:
          "Say exactly this to the user, word for word, and say nothing else: " +
          item.text,
      },
    });
  }

  return {
    announce: function (text, meta, fallbackText) {
      if (!text) return;
      queue.push({
        text: String(text),
        fallbackText: fallbackText ? String(fallbackText) : null,
        meta: meta || null,
        timer: null,
        sawCreate: false,
        deferred: false,
        ownerDeferrals: 0,
      });
      pump();
    },
    // Everything this announcer holds dies HERE, not by the page dropping its
    // reference (#978 item 4). stop() nulled `announcer` and the armed
    // setTimeout closure survived it: 6s into "idle" the browser voice spoke,
    // and onSpoken(meta, "fallback") anchored the proposal on the bridge —
    // closing the NEXT session's volunteering gate for up to 120s over a
    // proposal nobody is answering. Nothing here reports anything as spoken:
    // a torn-down item was not heard, and the anchor is the one thing that
    // must never be told otherwise.
    teardown: function () {
      queue.forEach(disarm);
      queue = [];
      if (current) { disarm(current); current = null; }
      releasePump();
      // LATCHED, not merely removed from `speaking`. stopSpeaking cancels our
      // watchdog but cannot cancel the utterance's own onend, which the
      // browser fires whenever it fires — and that callback closes over the
      // item and would have anchored a proposal on the bridge from a torn-down
      // session, which is #978 item 4 in the one leg the reference-nulling fix
      // did not reach. Settling here with no outcome is the same statement the
      // rest of this function makes: a torn-down item was not heard, and
      // nothing downstream may believe it was.
      speaking.slice().forEach(function (it) {
        it.speechSettled = true;
        stopSpeaking(it);
      });
    },
    // The owner started speaking over the FALLBACK voice (#1041). Native
    // barge-in covers model audio only — the server truncates the response and
    // the WebRTC stream stops — while speechSynthesis is outside that path and
    // used to play straight through, which is the "sometimes she won't shut
    // up" regime: it correlated exactly with WHAT was playing.
    //
    // Synchronous on purpose: the cut lands inside the speech_started event
    // tick, so the bound on "owner speaks → audio stops" is VAD detection
    // latency alone, never the utterance's remaining length.
    //
    // Both halves priced. The cut utterance settles NOT SPOKEN — through the
    // same latch as onerror/watchdog, so the browser's own late events for it
    // are no-ops — which means an inbox notice is released and said again at
    // the next quiet tick (announced late, never lost: the truncated
    // confirm-spine outcome line is re-delivered, not dropped mid-word), and
    // a proposal cut mid-statement never anchors: refusing to approve a
    // half-stated proposal is what the anchor is FOR. The queue and the armed
    // fallback timer are deliberately untouched — the owner speaking delays
    // them (armFallback re-checks ownerSpeaking), it does not discard them.
    // Cost accepted: an owner who interrupts near the end hears the notice
    // twice; double-speak is the cheap failure, talked-through is the
    // expensive one.
    bargeIn: function () {
      if (!speaking.length) return 0;
      var cut = speaking.slice();
      // Global cancel — everything speechSynthesis holds is ours, and all of
      // it is audio the owner is now talking over.
      try { stopSpeak(); } catch (e) {}
      cut.forEach(function (it) { settleSpeech(it, false); });
      return cut.length;
    },
    // Withdraw announcements whose meta matches, queued or current (#963: the
    // owner speaking first CANCELS the greeting; queueing it behind them would
    // greet someone who has already moved on). A withdrawn item's fallback is
    // disarmed and onSpoken never fires for it — it was never heard, and
    // nothing downstream may believe it was. Note what this does NOT
    // establish: it cannot recall audio already EMITTED — the model's via
    // native barge-in, the fallback voice's via bargeIn() above; this covers
    // the QUEUE and the TIMER.
    cancel: function (match) {
      queue = queue.filter(function (it) { return !match(it.meta); });
      if (current && match(current.meta)) {
        disarm(current);
        if (responseActive) send({ type: "response.cancel" });
        current = null;
        pump();
      }
    },
    onResponseCreated: function () {
      responseActive = true;
      // A response beginning while this item is current is the one signal
      // that its audio may be OURS mid-flight — see the deferral in
      // armFallback. Only ever set while current, so a response that predates
      // the announce cannot defer it.
      if (current) current.sawCreate = true;
    },
    // A cancelled response only clears the in-flight flag. It must NEVER
    // disarm or count as spoken: a cancelled turn can carry partial audio that
    // said something else, and our OWN cancel produces one.
    onResponseCancelled: function () {
      responseActive = false;
      // Whatever was in flight is dead, so it can no longer be "our audio
      // still playing" — the deferral signal must not outlive it.
      if (current) current.sawCreate = false;
    },
    // Returns true ONLY when this transcript is the model speaking the
    // CURRENT scripted announcement — i.e. exactly when it disarms. The page's
    // transcript log keys its kind off this verdict (#957): a true verdict
    // logs as "heard" (the ASR of a text announce() already logged), anything
    // else stays a plain buddy line. After the FALLBACK fires, `current` is
    // cleared, so a late model utterance of the same text — the genuine
    // double-speak (#950) — verdicts false and keeps its two-line signature.
    onResponseDone: function (transcript) {
      responseActive = false;
      if (!current) { pump(); return false; }
      // The ONLY disarm: positive evidence that the reason was spoken.
      if (carriedTheReason(transcript, current.text)) {
        var done = current;
        disarm(done);
        current = null;
        // POSITIVE evidence this text was spoken by the model — the only
        // thing the anchor may key on.
        onSpoken(done.meta, "model");
        pump();
        return true;
      }
      // Otherwise leave the timer armed. A response that said something else
      // is not evidence the owner heard the refusal — and it has FINISHED, so
      // it is no longer a reason to defer either.
      current.sawCreate = false;
      return false;
    },
    // Is a PROPOSAL announcement still in the pipe — queued or mid-flight?
    //
    // The confirm gate closes at anchored(), i.e. once the proposal has been
    // SPOKEN. Between the write tool returning and that moment the gate is
    // still open, so an escalation ticking right then passed canInterrupt,
    // queued behind the proposal, and pump() promoted it the instant
    // anchoring closed the gate — an alarm spoken exactly between "say confirm
    // tango" and the owner's answer (#978 item 2). The announcer is the only
    // thing that can see that window, so canInterrupt asks it.
    anchorPending: function () {
      if (current && current.meta && current.meta.anchor) return true;
      var carriesAnchor = function (it) { return !!(it.meta && it.meta.anchor); };
      // `speaking` too: the fallback voice mid-utterance is the buddy STATING
      // the proposal, which is the middle of the handshake by any reading.
      return queue.some(carriesAnchor) || speaking.some(carriesAnchor);
    },
    // Test/inspection surface — and load-bearing: canSpeak keys on this, so
    // an item whose fallback audio is still playing has to count, or a notice
    // is volunteered straight over the buddy's own voice.
    pending: function () {
      return (current ? 1 : 0) + queue.length + speaking.length;
    },
    armed: function () { return !!(current && current.timer); },
  };
}
"""

#: The buddy's clock (#962), same discipline as the announcer: a standalone
#: factory with injected deps so the whole loop — peek, gate, coalesce,
#: announce, ack-after-spoken — runs under ``node`` against a fake bridge and a
#: fake timer. Spliced into the page by :func:`page`; exported by
#: :func:`notifier_source` for the test harness.
INBOX_NOTIFIER_JS = """
// The buddy's clock. Before this, client.py contained no polling of any kind —
// every action was downstream of the owner speaking, so the buddy could answer
// a topic but never open one. This is the one clock: poll the buddy's spool,
// and when a reply has arrived, volunteer it — through the injected
// `announce`, which is the page's ONE speaking path (#950). The notice is a
// bonus, never a contract: an empty spool produces silence, not chatter.
//
// Injected dependencies:
//   fetchInbox() -> Promise<{success, messages}>  PEEK — never advances the cursor
//   ackInbox(id) -> Promise<{success, acked}>  advance the cursor to EXACTLY id
//              (#970). The old bool ack advanced to the spool TAIL as it stood
//              at ack time, and the buddy acks only after SPEAKING — so mail
//              landing in that window was cursor-advanced past unread, with no
//              dead-letter and no screen to notice it on. #969 caught those in
//              a page-lifetime array and a page unload dropped them silently;
//              acking by id means there is nothing to catch.
//   announce(text, meta)   the page's announce() — no other voice exists here
//   canSpeak() -> bool     owner not speaking, no active response, nothing queued
//   canInterrupt() -> bool the RELAXED gate for escalation-kind messages
//              (#967, reconciled with #962): owner not speaking and no confirm
//              handshake outstanding — the two legs that stay unconditional —
//              but NOT waiting for the buddy's own speech to finish. An
//              escalation is the fleet's already-made judgment (the same
//              typed kind that emails the owner on dead-letter), so the
//              interrupt decision is a mechanism check on the message kind,
//              never "how urgent does the model feel". Optional; absent
//              means escalations wait like everything else.
//   reRaise    the re-raise ledger (optional). Ticked only on a FULL-gate
//              poll with nothing fresh to say — a re-raise is politeness,
//              never an interrupt, and fresh news always outranks a reminder.
//   setTimer(fn, ms) / clearTimer(handle), onLog(kind, detail), pollMs
//   seen: {}   page-lifetime map of ids the owner has actually HEARD. Passed in
//              rather than owned so it outlives this notifier: a reconnect
//              builds a fresh notifier over the same map and cannot replay a
//              spoken notice — while a notice announced but never SPOKEN is
//              not in it, and is correctly said again.
function createInboxNotifier(deps) {
  var fetchInbox = deps.fetchInbox;
  var ackInbox = deps.ackInbox;
  var announce = deps.announce;
  var canSpeak = deps.canSpeak;
  var setTimer = deps.setTimer;
  var clearTimer = deps.clearTimer;
  var onLog = deps.onLog || function () {};
  var pollMs = deps.pollMs;
  var seen = deps.seen;
  var canInterrupt = deps.canInterrupt || function () { return false; };
  var reRaise = deps.reRaise || null;

  // Announced this session but not yet confirmed spoken. Per-notifier on
  // purpose: if the session dies mid-announcement the map dies with it, so
  // the unheard notice is retried — `seen` alone decides what is settled.
  var inFlight = {};
  var timer = null;
  var stopped = false;

  function trimBody(text) {
    var t = String(text || "").replace(/\\s+/g, " ").trim();
    return t.length > 240 ? t.slice(0, 240) + "\\u2026" : t;
  }

  // Coalesce: several replies arriving together are ONE utterance, not a
  // volley of interruptions. No promise language — "got back to you" states
  // what happened, nothing about what will.
  function isUrgent(m) { return !!m && m.kind === "escalation"; }

  // Mail from the MACHINE rather than from a session (#982, #1016). The
  // wording matters because these are not replies: "fleet-activity got back to
  // you" tells the owner a session with a robot's name answered something they
  // never sent, and "fleet-alerts escalated" names an internal module out loud
  // as if it were a colleague. Both are sentences the owner HEARS, and the
  // announcer speaks composeNotice verbatim — the model does not get a chance
  // to rephrase it. Kept in sync with `fleet_alerts.MACHINE_SENDERS`, which is
  // the same list on the producing side.
  var MACHINE_SENDERS = { "fleet-alerts": 1, "fleet-activity": 1 };
  function isMachine(m) { return !!m && !!MACHINE_SENDERS[m.from]; }
  function speaker(m) { return isMachine(m) ? "the fleet" : ((m && m.from) || "someone"); }

  // HOW MANY BODIES ONE UTTERANCE MAY CARRY. Speech cannot be skimmed and the
  // owner cannot predict when it stops, so an unbounded coalesce is a monologue
  // waiting for a fan-out: ten workers landing in one 5s poll window produced
  // one ~2500-character utterance, spoken over nothing the owner asked for. The
  // overflow is NOT dropped and NOT acked — it stays unread and the next quiet
  // tick says the next three, which is the same "announced late, never lost"
  // property the interrupt tier already relies on.
  var MAX_NOTICE_BODIES = 3;

  function composeNotice(messages, waiting) {
    // An escalation in the batch names itself as one — the owner should be
    // able to hear the difference between news and an alarm.
    var prefix = messages.some(isUrgent) ? "Heads up \\u2014 " : "";
    // Said out loud rather than left implicit: the owner has to be able to tell
    // "that was everything" from "there is more coming", or a capped batch
    // sounds exactly like a complete one.
    var tail = waiting > 0 ? " And " + waiting + " more waiting." : "";
    if (messages.length === 1) {
      var m = messages[0];
      // No verb for machine mail: the body is already a whole statement
      // ("auth-fix is idle and done working"), so any verb here would be the
      // notifier narrating a relationship that does not exist.
      if (isMachine(m)) return prefix + "From the fleet: " + trimBody(m.text) + tail;
      var verb = isUrgent(m) ? " escalated: " : " got back to you: ";
      return prefix + (m.from || "someone") + verb + trimBody(m.text) + tail;
    }
    var parts = messages.map(function (msg) {
      return "From " + speaker(msg) + ": " + trimBody(msg.text);
    });
    return prefix + messages.length + " updates came in. " + parts.join(" ") + tail;
  }

  function pollOnce() {
    return fetchInbox().then(function (res) {
      // STOPPED MID-FLIGHT. This promise is not cancellable, so a poll begun
      // before stop() resolves after it — and the interrupt tier could still
      // pass, handing an escalation to a null announcer for a bare
      // speechSynthesis.speak with no meta: never acked, never seen (#978
      // item 6). Since #970 it is at least not cursor-past — only a SPOKEN
      // notice acks — so the next session re-reads it.
      if (stopped) return;
      if (!res || !res.success) {
        onLog("inbox", "poll failed: " + ((res && res.error) || "no response"));
        return;
      }
      // NEVER BARGE IN — on the OWNER. A blocked tick marks nothing and
      // loses nothing: the same replies are still unacked next tick, so
      // waiting is free. The gate is two-tier (#967 reconciling #962): the
      // full gate clears everything; the interrupt gate clears ONLY
      // escalation-kind messages, and still never fires while the owner is
      // speaking or a confirm handshake is outstanding. #962's rule survives
      // intact where it was about the human; the leg it loses is only
      // "wait for the buddy's own chatter to finish".
      var full = canSpeak();
      if (!full && !canInterrupt()) return;
      var take = function (m) { return full || isUrgent(m); };
      var unread = res.messages || [];
      var picked = {};
      var fresh = unread.filter(take).filter(function (m) {
        if (!m || !m.id || seen[m.id] || inFlight[m.id] || picked[m.id]) return false;
        picked[m.id] = true;
        return true;
      });
      if (!fresh.length) {
        // A quiet full-gate tick is the natural gap a re-raise waits for.
        // Never on the interrupt tier: a reminder is not an alarm.
        if (full && reRaise) {
          var reminder = reRaise.dueText();
          if (reminder) {
            onLog("reraise", "second mention");
            // The ids ride along so the page can commit the second mention
            // from onSpoken — dueText consumes nothing (see the ledger).
            announce(reminder.text, { reRaise: true, reRaiseIds: reminder.ids });
          }
        }
        return;
      }
      // CAP THE UTTERANCE, not the mail. Only what this notice actually SAYS
      // is claimed, marked in-flight and eligible to be acked; the rest is
      // untouched in the spool and the next quiet tick picks it up.
      var batch = fresh.slice(0, MAX_NOTICE_BODIES);
      var waiting = fresh.length - batch.length;
      var claimed = {};
      var ids = batch.map(function (m) { claimed[m.id] = true; return m.id; });
      ids.forEach(function (id) { inFlight[id] = true; });
      // HOW FAR THIS NOTICE MAY ACK (#970). The cursor is ONE id, so acking
      // through id X marks everything before X read too. The only safe target
      // is the end of the longest UNBROKEN run of unread messages that are
      // either being said now or were already heard — walked over the peek's
      // own list, so nothing the spool grows afterwards can widen it.
      //
      // Stopping at the first message this tick skipped is what keeps the
      // interrupt tier honest: it speaks an escalation and leaves an ordinary
      // report unread AHEAD of it, and there is no ack that covers the alarm
      // without also burying the report. So that tick acks nothing, and the
      // full tick that finally speaks the report clears both — the skipped
      // message is announced late, never lost.
      var ackThrough = "";
      for (var i = 0; i < unread.length; i++) {
        var m = unread[i];
        if (!m || !m.id || !(seen[m.id] || claimed[m.id])) break;
        ackThrough = m.id;
      }
      onLog("inbox", "volunteering " + batch.length + " message(s)"
        + (waiting ? " (" + waiting + " held for the next gap)" : ""));
      // inboxMsgs rides along so the page can register request/escalation
      // notices in the re-raise ledger AT THE MOMENT THEY ARE HEARD (its
      // onSpoken) — the ledger must never hold something the owner wasn't
      // actually told.
      announce(composeNotice(batch, waiting), {
        inboxIds: ids,
        ackThrough: ackThrough,
        inboxMsgs: batch.map(function (m) {
          return { id: m.id, from: m.from, kind: m.kind, text: m.text };
        }),
      });
    }).catch(function (err) {
      onLog("inbox", "poll failed: " + err);
    });
  }

  // ACK ONLY AFTER IT HAS ACTUALLY BEEN SPOKEN — the page calls this from the
  // announcer's onSpoken, the moment there is evidence the owner heard it
  // (model or fallback voice; either way, heard). The reverse order marks
  // delivered a report the owner never heard.
  function noticeSpoken(meta) {
    if (!meta || !meta.inboxIds) return Promise.resolve();
    meta.inboxIds.forEach(function (id) { seen[id] = true; });
    // Nothing contiguous to ack — this tick spoke past an unread message and
    // the cursor cannot express that. Marking `seen` is the whole receipt; the
    // ack rides the later tick that says the skipped one.
    if (!meta.ackThrough) return Promise.resolve();
    return ackInbox(meta.ackThrough).then(function (res) {
      if (!res || !res.success || res.acked === false) {
        // Cursor not advanced: a page reload will re-read these. `seen`
        // suppresses a replay for THIS page's lifetime, which is the right
        // half to keep — re-reading is an annoyance, losing one is the bug.
        // Said out loud on screen: a refused ack that reads as success
        // re-announces every session with nothing to explain why.
        onLog("inbox", "ack through " + meta.ackThrough + " failed: "
          + ((res && res.error) || "cursor not advanced"));
      }
    }).catch(function (err) {
      onLog("inbox", "ack through " + meta.ackThrough + " failed: " + err);
    });
  }

  // The announcement demonstrably did NOT reach the owner — the browser voice
  // reported an error. Release the ids so the next gated tick says it again.
  //
  // This is what made the comment on `inFlight` true. It promised "the unheard
  // notice is retried", but ids entered the map at ANNOUNCE time and never
  // left it: speechSynthesis fails silently, `utterance.onerror` only logged,
  // and so a notice that was neither heard nor acked was suppressed for the
  // rest of the session (#978 item 5).
  //
  // Releasing the id is now the WHOLE job (#970). It was not, while the ack
  // swept to the tail: a message pulled from the strays array was already
  // cursor-past, so releasing its id dropped it from both places and "the next
  // tick says it again" was false for exactly the class that became strays.
  // Since only a SPOKEN notice acks, and only through what it spoke, a failed
  // announcement leaves the message untouched in the spool — the next peek
  // returns it, and so does the next page.
  //
  // `seen` still outranks a release, but structurally rather than by a guard
  // here: pollOnce drops a `seen` message BEFORE it consults `inFlight`, so a
  // heard notice cannot be re-announced by a late failure whatever this map
  // says. The guard that used to stand here was load-bearing only while a
  // release could push a body back into the strays array; with the array gone
  // it could be cut with the whole suite green, which makes it a line claiming
  // a guarantee it no longer holds.
  function noticeFailed(meta) {
    if (!meta || !meta.inboxIds) return;
    meta.inboxIds.forEach(function (id) { delete inFlight[id]; });
  }

  function schedule() {
    if (stopped) return;
    timer = setTimer(function () {
      pollOnce().then(schedule, schedule);
    }, pollMs);
  }

  return {
    start: function () {
      stopped = false;
      pollOnce().then(schedule, schedule);
    },
    stop: function () {
      stopped = true;
      if (timer) { clearTimer(timer); timer = null; }
    },
    pollOnce: pollOnce,
    noticeSpoken: noticeSpoken,
    noticeFailed: noticeFailed,
  };
}
"""

#: Insistence as re-raise (#967). A peer says a thing once; if you visibly
#: did not act on it, they say it again — and that needs no interrupt licence
#: at all, because the re-raise waits for the same full gap any volunteered
#: notice waits for. Same injected-deps discipline as the announcer and the
#: notifier; exported by :func:`reraise_source` for the node tests.
RERAISE_JS = """
// "Told them, nothing changed." An item enters the ledger only when the owner
// actually HEARD it (the page registers from the announcer's onSpoken, never
// from the announce), because re-raising something never said is just saying
// it — and re-raising something the owner never heard as "still open" reads
// as an accusation.
//
// Resolution is an OBSERVED act, not a model judgment: the page marks a
// session acted-on when a confirmed write actually went its way. The owner
// acting outside the buddy's view (typing into the session themselves) is
// invisible here, and that is priced: the cost is at most ONE extra mention,
// because an item re-raises exactly once and is then dropped. Twice is a
// peer; a third time is a nag, and an unbounded reminder loop in a screenless
// channel is the nag with no off switch.
function createReRaiseLedger(deps) {
  var now = deps.now;
  // How long "nothing changed" has to persist before the second mention.
  var dueMs = deps.dueMs;
  var onLog = deps.onLog || function () {};

  var items = {}; // id -> { from, text, at, reRaised, resolved }

  function trim(text) {
    var t = String(text || "").replace(/\\s+/g, " ").trim();
    return t.length > 160 ? t.slice(0, 160) + "\\u2026" : t;
  }

  return {
    // Idempotent: the same heard notice registering twice (a retried
    // announcement after a reconnect) must not double the reminder.
    register: function (id, info) {
      if (!id || items[id]) return;
      items[id] = {
        from: (info && info.from) || "someone",
        text: trim(info && info.text),
        at: now(),
        reRaised: false,
        resolved: false,
      };
      onLog("reraise", "tracking " + id);
    },
    // A confirmed write went to `session` — everything it asked about counts
    // as acted on. Coarse on purpose: matching the reply to the exact request
    // would need judgment, and a wrongly-suppressed reminder here costs one
    // mention, not a lost message.
    actedOn: function (session) {
      Object.keys(items).forEach(function (id) {
        if (items[id].from === session) items[id].resolved = true;
      });
    },
    resolve: function (id) {
      if (items[id]) items[id].resolved = true;
    },
    // The one output: { text, ids } for everything due, or null. Marks
    // NOTHING — the caller announces it, and only evidence the owner HEARD
    // it (the page's onSpoken) commits the second mention via spoken(ids).
    // Consuming at compose time was the ack-before-spoken shape #962 D1 was:
    // a stop() or a cancelled announcement between compose and speech
    // silently lost the one reminder this ledger exists to produce. Between
    // announce and spoken the full gate stays closed (canSpeak requires an
    // empty announcer queue), so a due item cannot be composed twice in
    // flight; a withdrawn announcement simply comes due again — the retry
    // is the point.
    dueText: function () {
      var ids = Object.keys(items).filter(function (id) {
        var it = items[id];
        return !it.resolved && !it.reRaised && now() - it.at >= dueMs;
      });
      if (!ids.length) return null;
      var parts = ids.map(function (id) {
        return items[id].from + " asked: " + items[id].text;
      });
      return {
        text: "Still open from earlier — " + parts.join(" And ") +
          " Nothing has gone their way since. Second mention, so I'll leave it with you.",
        ids: ids,
      };
    },
    // The reminder was confirmed spoken — NOW the second mention is spent.
    spoken: function (ids) {
      (ids || []).forEach(function (id) {
        if (items[id]) items[id].reRaised = true;
      });
    },
    pending: function () {
      return Object.keys(items).filter(function (id) {
        var it = items[id];
        return !it.resolved && !it.reRaised;
      }).length;
    },
  };
}
"""

#: The confirm-outstanding gate (#962 never-barge-in, wave-2 D2). A proposal
#: SPOKEN but not yet answered is a handshake in progress: the buddy must not
#: volunteer an inbox notice between "say confirm X" and the owner saying it.
#: The interjection cannot corrupt the confirm — the nonce is unreachable from
#: any delivered body since #953 — it is barging in, which #962 forbids on its
#: own. Both halves of the guard are priced (the messaging drain's lesson): the
#: false-accept is one rude interjection; the false-reject is a buddy that goes
#: silently mute in a screenless channel, so the block EXPIRES on the
#: proposal's own TTL rather than on an outcome the owner may never produce.
#: Same injected-deps discipline as the announcer and the notifier, so the TTL
#: behaviour runs under node; exported by :func:`confirm_gate_source`.
CONFIRM_GATE_JS = """
// "A proposal is outstanding" is a client-side mirror of the spine, driven by
// the two edges the client actually observes: anchored() when the announcer
// confirms the proposal was SPOKEN (the same evidence that starts the server
// TTL), resolved() when a write tool returns a terminal outcome. An outcome
// the owner never produces is covered by the TTL, never waited on.
function createConfirmGate(deps) {
  var now = deps.now;
  var ttlMs = deps.ttlMs;
  // -1 is "no proposal", not a zero timestamp — a proposal anchored at
  // clock 0 is still a proposal.
  var since = -1;
  return {
    anchored: function () { since = now(); },
    resolved: function () { since = -1; },
    outstanding: function () {
      return since >= 0 && now() - since < ttlMs;
    },
  };
}
"""

#: Routes a write tool's outcome to the gate and the ledger. Extracted so the
#: routing runs under node against REAL verdict payloads — the gate leg used
#: to key on hard-coded tool names, which is exactly the condition #966's
#: generalisation invalidates the moment a second write is declared; exported
#: by :func:`outcome_router_source`.
OUTCOME_ROUTER_JS = """
// The client's read of a write tool's outcome. Two DIFFERENT signals, priced
// separately:
//
// 1. "The confirm handshake is over" reopens the volunteering gate. That is
//    the payload's own confirm_terminal — set by the spine for EVERY gated
//    write, so a newly declared write (#966) reopens the gate without this
//    file learning its name. Keying on tool names here is the false-reject
//    trap: the second write's terminal outcome would not match, the gate
//    would sit closed for its full TTL, and the buddy would go silently mute.
//
// 2. "The owner acted on that session" retires its re-raise reminders. NOT
//    the same predicate: a cancel is terminal but is not acting — retiring on
//    it loses the second mention the ledger exists to produce. The spine sets
//    acted_session on APPROVED payloads only, from the proposal frozen at
//    propose time, so this leg is as name-free as the gate leg. The old
//    client-side guess — remember the last proposal's session — retired the
//    WRONG session's reminders whenever two proposals interleaved.
function createOutcomeRouter(deps) {
  var gate = deps.gate;
  var ledger = deps.ledger;
  return {
    route: function (name, result) {
      if (!result || !result.confirm_terminal) return;
      gate.resolved();
      if (result.acted_session) ledger.actedOn(result.acted_session);
    },
  };
}
"""

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentwire buddy — spike</title>
<style>
  :root {
    --bg: #0b0e11; --fg: #e6edf3; --muted: #8b949e;
    --accent: #00ff66; --accent-2: #00bfff; --border: #21262d; --radius: 10px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
    font: 15px/1.5 ui-sans-serif, -apple-system, system-ui, sans-serif;
  }
  header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 20px; }
  h1 { font-size: 17px; margin: 0; font-weight: 600; }
  .tag {
    font-size: 11px; letter-spacing: .08em; text-transform: uppercase;
    color: var(--accent); border: 1px solid var(--border);
    border-radius: 999px; padding: 2px 9px;
  }
  button {
    font: inherit; font-weight: 600; padding: 10px 20px; border-radius: var(--radius);
    border: 1px solid var(--border); background: var(--accent); color: #04140a;
    cursor: pointer;
  }
  button.stop { background: transparent; color: var(--fg); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  #status { color: var(--muted); margin-left: 12px; font-size: 13px; }
  label.voice { color: var(--muted); font-size: 13px; margin-left: 12px; }
  select {
    font: inherit; font-size: 14px; padding: 8px 10px; border-radius: var(--radius);
    border: 1px solid var(--border); background: #0d1117; color: var(--fg);
  }
  select:disabled { opacity: .45; cursor: not-allowed; }
  #log {
    margin-top: 20px; border: 1px solid var(--border); border-radius: var(--radius);
    padding: 14px; height: 60vh; overflow-y: auto; background: #0d1117;
  }
  .row { padding: 6px 0; border-bottom: 1px solid #161b22; }
  .row:last-child { border-bottom: 0; }
  .who { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  .you .who { color: var(--accent-2); }
  .buddy .who { color: var(--accent); }
  .tool { color: var(--muted); font-family: ui-monospace, monospace; font-size: 12.5px; }
  /* The spoken-transcript kind (#957): the model's ASR of a scripted
     announcement already logged above it. Muted + italic so a normal
     announcement pair cannot be misread as the buddy speaking twice. */
  .heard { color: var(--muted); font-style: italic; }
  .heard .who { color: var(--muted); }
  .err { color: #ff7b72; }
</style>
</head>
<body>
<header>
  <h1>buddy: __BUDDY__</h1>
  <span class="tag">spike · reads + one confirmed write</span>
</header>
<div>
  <button id="start">Start talking</button>
  <button id="stop" class="stop" disabled>Stop</button>
  <label class="voice" for="voice">voice</label>
  <select id="voice">__VOICE_OPTIONS__</select>
  <span id="status">idle</span>
</div>
<div id="log"></div>
<script>
__ANNOUNCER__
__NOTIFIER__
__CONFIRM_GATE__
__RERAISE__
__OUTCOME_ROUTER__

const TOKEN = __TOKEN__;
const CALLS_URL = "https://api.openai.com/v1/realtime/calls";

const $log = document.getElementById("log");
const $status = document.getElementById("status");
const $start = document.getElementById("start");
const $stop = document.getElementById("stop");
const $voice = document.getElementById("voice");

// The voice this page will mint with. Seeded from the bridge, which resolved
// it from `--voice` → the buddy's record → the default, so a reload comes back
// showing whatever was last chosen rather than silently reverting (#1017).
let currentVoice = __VOICE__;

let pc = null, dc = null, micStream = null, audioEl = null;
let responseActive = false;

// --- conversation-item time -------------------------------------------------
// The confirm gate's ordering predicate lives on this counter, not on a clock.
// See the module docstring: a wall-clock comparison is between a receipt time
// and an intent time, and it silently inverts.
//
// THE ORIGIN IS NOT OURS (#978). This variable is page-scoped and a reload
// restarts it, while the ring and the spine live for the whole bridge run — so
// a reloaded page anchored its proposals BELOW last session's utterances,
// which are still in the ring, complete and unspent. They then reached the
// judge as non-matching (burning attempts toward retiring a proposal the owner
// was never asked about) and, worse, an old "no, hang on" landed
// strictly-after the new match in the post-approval denial scan and
// retroactively denied every legitimate approval. So start() seeds this from
// the mint's `seq_base`: the bridge owns the origin, this page owns the order.
let seqCounter = 0;
const speechSeq = {};        // item_id -> the seq at which the owner BEGAN speaking
const commitSeq = {};        // item_id -> the seq at which its audio committed
// Every forward to the bridge is chained, and tool dispatch awaits the chain.
// Without this the transcript POST and the confirm POST are independent fetches
// that can land reordered, and the gate evaluates against a ring that has not
// received the utterance it is about to be asked about.
let forwardChain = Promise.resolve();
let announcer = null;
let parseFailuresAnnounced = 0;

// --- the buddy's clock (#962) -----------------------------------------------
// How often to peek at the spool. 5 seconds: a reply is a human-scale event —
// the acceptance bound is "volunteered within a bounded interval", and 5s is
// far inside conversational patience while keeping the cost at one tiny POST
// to the LOCAL bridge per tick. Sub-second buys nothing (the notice still
// waits for a gap in the conversation); minutes would make the volunteer
// feature feel dead.
const INBOX_POLL_MS = 5000;
// Page-lifetime: ids the owner has actually HEARD. Never reset by stop() — a
// reconnect must not replay every notice.
const heardReplies = {};
let inboxNotifier = null;
let ownerSpeaking = false;

// --- insistence as re-raise (#967) -------------------------------------------
// "Told them, nothing changed" → one more mention at the next quiet full-gate
// tick, then dropped. Page-lifetime like heardReplies: what the owner was told
// must survive a stop()/reconnect, or every reconnect resets the peer's memory
// of its own words. How long "nothing changed" persists before the second
// mention — two minutes is a natural gap's scale, not a nag's.
const RERAISE_DUE_MS = 120000;
const reRaiseLedger = createReRaiseLedger({
  now: () => Date.now(),
  dueMs: RERAISE_DUE_MS,
  onLog: (kind, detail) => log("speak", kind + ": " + detail, "tool"),
});
// The confirm handshake's gate leg (#962, wave-2 D2): closed while a spoken
// proposal awaits the owner's confirm word, reopened by a terminal write-tool
// outcome or by the proposal's own TTL — never left waiting on an answer the
// owner may not give. Page-lifetime on purpose: the spine lives in the
// bridge, so a proposal survives a stop(), and a reconnect inside the TTL
// can still be answered.
const confirmGate = createConfirmGate({
  now: () => Date.now(),
  // Mirrors confirm.PROPOSAL_TTL_S — the bound on the false-reject half.
  ttlMs: 120000,
});

// Page-lifetime like the gate and the ledger it drives, and STATELESS — its
// own header says so. Remembering the most recent proposal's target session is
// precisely the client-side guess #966 removed: it retired the wrong session's
// reminders whenever two proposals interleaved. Both legs now read the
// payload — confirm_terminal for the gate, acted_session (frozen at propose)
// for the ledger.
const outcomeRouter = createOutcomeRouter({
  gate: confirmGate,
  ledger: reRaiseLedger,
});

// --- the greeting (#963) ----------------------------------------------------
// The buddy speaks first — and since #950 the write path is fail-closed on
// model audio, so a HEARD greeting proves the whole approval path at second
// zero. That is why the greeting must be spoken by the MODEL: a fallback-
// spoken greeting confirms the browser voice while the model's is dead and
// nothing can ever be approved. The announcer's fallback stays armed (silence
// is still unacceptable) but its text is MODEL_AUDIO_DEAD — the browser voice
// surfaces the failure instead of impersonating health.
const GREETING = "Hey, I'm listening. What's on your mind?";
const MODEL_AUDIO_DEAD = "Heads up: my main voice isn't working, so nothing can be approved. Try stopping and starting again.";
// Page-lifetime, never reset by stop(): a dropped and re-established
// connection must not re-greet. Also set by the owner speaking first — a late
// greeting after they've started talking is worse than none.
let greeted = false;
let sessionReady = false;   // the server's session.created arrived
let audioAttached = false;  // pc.ontrack wired the model's audio to a sink

// Channel-open is NOT readiness: session.created is the server saying the
// session exists, and the audio element is what makes model speech audible.
// Whichever lands last fires the greeting. What this does NOT establish: that
// audio actually PLAYS — the disarm keys on the model's transcript, so a muted
// tab still reads as healthy. That gap is inherent to every announcement, not
// introduced here.
function maybeGreet() {
  if (greeted || !sessionReady || !audioAttached) return;
  greeted = true;
  announce(GREETING, { greeting: true }, MODEL_AUDIO_DEAD);
}

function nextSeq() { return ++seqCounter; }

function setStatus(text) { $status.textContent = text; }

function log(who, text, cls) {
  const row = document.createElement("div");
  row.className = "row " + (cls || "");
  const label = document.createElement("div");
  label.className = "who";
  label.textContent = who;
  const body = document.createElement("div");
  body.textContent = text;
  row.append(label, body);
  $log.append(row);
  $log.scrollTop = $log.scrollHeight;
}

// The status code alone hid "You have no credits remaining" behind a bare
// 429 for a whole debugging session (#1037). When the body is the standard
// OpenAI error envelope, say what it said — and say whether retrying can
// help: insufficient_quota / credit_balance_exhausted is terminal, while a
// plain rate-limit stays exactly the message it was, so a retryable blip
// never reads as permanent (both halves priced). Unparseable bodies fall
// back to the bare status — never less than before.
function describeApiFailure(prefix, status, bodyText) {
  let detail = "";
  let terminal = false;
  try {
    const err = (JSON.parse(bodyText) || {}).error;
    if (err && (err.message || err.code)) {
      const parts = [];
      if (err.message) parts.push(err.message);
      if (err.code) parts.push("[" + err.code + "]");
      detail = ": " + parts.join(" ");
      terminal = ["insufficient_quota", "credit_balance_exhausted"]
        .some((c) => c === err.code || c === err.type);
    }
  } catch (e) { /* not JSON — the bare status is the fallback */ }
  return prefix + " (" + status + ")" + detail
    + (terminal ? " — add credits; retrying won't help" : "");
}

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return res.json();
}

// Queue a forward to the bridge onto the ordered chain. Never blocks the event
// handler, never rejects out of it — but IS awaited before any tool dispatch,
// which is the ordering guarantee the gate depends on. A forward that fails is
// spoken, not swallowed: a lost transcript turns into a refusal the owner
// otherwise has no way to understand.
function forward(path, body) {
  forwardChain = forwardChain.then(
    () => post(path, body).catch((err) => {
      log("error", "forward to " + path + " failed: " + err, "err");
      announce("I'm having trouble hearing you — the local bridge didn't answer.");
    }),
    () => {},
  );
  return forwardChain;
}

// Everything the owner must HEAR goes through here. Never `log()` alone for
// anything that changes what they should do next. `fallbackText`, when given,
// is what the browser-voice fallback utters instead of `text` — the
// un-echo-cancelled channel gets the echo-safe variant.
function announce(text, meta, fallbackText) {
  log("buddy", text, "buddy");
  if (announcer) announcer.announce(text, meta, fallbackText);
  else {
    try {
      window.speechSynthesis.speak(
        new SpeechSynthesisUtterance(fallbackText || text));
    } catch (e) {}
  }
}

// One spoken error notice at a time. An error event while a previous notice
// is still unspoken logs but does not announce — the first is the actionable
// signal, and announcing each one is the other edge of the announce → error →
// announce loop (#950 defect 2). Cleared when the notice is actually SPOKEN
// (see onSpoken), so a later, distinct error can announce again.
let errorNoticePending = false;

// The proposal anchor. Driven ONLY by evidence the proposal was spoken:
// `how` is "model" (a response.done carried the text) or "fallback" (the
// browser voice said it, and the owner did hear it).
//
// Anchoring on "the next response.done with any text" is what let the
// announcer's own cancel steal the anchor, and left a fallback-spoken proposal
// anchored by nothing at all — the owner hears the proposal, says the nonce,
// and gets not_announced until the TTL. That is the one corner where the two
// safety mechanisms defeated each other: not_announced is never SILENT — it
// speaks correctly every time — but it can be PERSISTENTLY WRONG, and what
// made it wrong was the fallback firing, which is the mechanism added to
// GUARANTEE speech.
function onSpoken(meta, how) {
  if (meta && meta.errorNotice) { errorNoticePending = false; return; }
  if (meta && meta.reRaise) {
    // The second mention is spent only NOW — the same ack-after-spoken
    // discipline as inbox notices (#962): a reminder announced but never
    // spoken comes due again instead of dying marked-but-unheard.
    reRaiseLedger.spoken(meta.reRaiseIds || []);
    return;
  }
  if (meta && meta.inboxIds) {
    // A volunteered inbox notice was heard — NOW it may be acked (#962), and
    // NOW anything in it that asks for action enters the re-raise ledger
    // (#967). Only kinds that ask — a done/note is news, not a request, and
    // re-raising news is chatter.
    if (inboxNotifier) inboxNotifier.noticeSpoken(meta);
    (meta.inboxMsgs || []).forEach((m) => {
      if (m && (m.kind === "request" || m.kind === "escalation")) {
        reRaiseLedger.register(m.id, m);
      }
    });
    return;
  }
  if (meta && meta.greeting) {
    // The health check's verdict (#963). "fallback" here means the model
    // never spoke the greeting — model audio is dead and, with #950's
    // fail-closed write path, nothing can be approved. The browser voice has
    // already said MODEL_AUDIO_DEAD aloud; this is the on-screen record.
    if (how === "fallback") {
      log("error", "model audio is DEAD — the write path cannot approve anything", "err");
    }
    return;
  }
  if (!meta || !meta.anchor) return;
  // The proposal is now HEARD and the confirm window is open — the same
  // evidence that starts the server-side TTL closes the volunteering gate.
  confirmGate.anchored();
  log("speak", "anchored proposal " + meta.anchor + " (" + how + ")", "tool");
  forward("/anchor", { proposal_id: meta.anchor, seq: nextSeq() });
}

// The not-spoken counterpart to onSpoken (#978 item 5, #996). TWO callers, and
// they are not the same kind of evidence:
//
//   - `utterance.onerror` — a POSITIVE report from speechSynthesis that this
//     utterance failed;
//   - the speaking watchdog — an INFERENCE from the ABSENCE of any end event
//     past a budget deliberately slower than any real voice. It is a guess,
//     and it is a guess made in one direction on purpose: the utterance it
//     describes has already outlived the longest time this page will believe
//     a voice is talking (#996).
//
// What they share is the only thing this handler needs — a state that is
// indistinguishable from success downstream unless something says otherwise,
// leaving the announcement neither delivered nor retried. A *throw* from
// speak() is the third state and is deliberately NOT routed here: it means we
// cannot know, and "assume heard" is the safe reading, because claiming
// not-spoken would replay a notice the owner may well have heard.
function onNotSpoken(meta) {
  if (!meta) return;
  if (meta.errorNotice) {
    // Let a later error announce again; this one never reached the owner.
    errorNoticePending = false;
    return;
  }
  if (meta.inboxIds) {
    // Release the ids so the next gated tick volunteers them again. NOT acked
    // — the cursor never moved, because it only moves from onSpoken.
    if (inboxNotifier) inboxNotifier.noticeFailed(meta);
    return;
  }
  if (meta.anchor) {
    // Nothing to retry: a proposal is announced once, and the spine's TTL is
    // what ends it. What is left is to say so on screen rather than let the
    // owner hear nothing and then be told `not_announced` for 120s.
    log("error",
      "proposal " + meta.anchor + " was never spoken — it cannot be approved",
      "err");
  }
}

function send(event) {
  if (!dc || dc.readyState !== "open") return false;
  dc.send(JSON.stringify(event));
  return true;
}

// The server rejects a response.create while one is in flight, and VAD creates
// its own responses — this is what keeps ours from racing them.
//
// NOTE the deliberate division of labour: this is fine for an ORDINARY tool
// result, where letting VAD's own in-flight response carry the answer is
// correct. It is NOT fine for a refusal, which is why refusals do not go
// through here at all — they go through the announcer, which cancels rather
// than declining. Returning false here used to mean "nothing is ever said",
// silently.
function maybeCreateResponse() {
  if (responseActive) return false;
  return send({ type: "response.create" });
}

// `suppressResponse` is for results the ANNOUNCER will speak. The output still
// has to land — an unresolved function call hangs the conversation — but the
// ordinary response must not be created, because the announcer is about to
// create a scripted one and two creates race.
function sendFunctionCallOutput(callId, output, suppressResponse) {
  const ok = send({
    type: "conversation.item.create",
    item: { type: "function_call_output", call_id: callId, output: JSON.stringify(output) },
  });
  if (!ok) {
    // The data channel is closed, so the model will never see this result and
    // can never speak it. Say it here instead of dropping it.
    announce("I lost the connection to the voice service, so I couldn't finish that.");
    return;
  }
  if (!suppressResponse) maybeCreateResponse();
}

async function handleFunctionCall(item) {
  let args = {};
  try { args = item.arguments ? JSON.parse(item.arguments) : {}; }
  catch { sendFunctionCallOutput(item.call_id, { error: "malformed arguments JSON" }); return; }
  log("tool", item.name + " " + JSON.stringify(args), "tool");

  let result;
  try {
    // The bridge's own discipline is "errors come back as data, never
    // exceptions — a stalled function call leaves the conversation hanging."
    // That discipline used to exist on one side of the wire only: an
    // un-awaited rejection here left the call unresolved and the conversation
    // silently hung, which is precisely the failure the discipline names.
    result = await post("/tool", { name: item.name, arguments: args });
  } catch (err) {
    log("error", "tool dispatch failed: " + err, "err");
    announce("I couldn't reach my own tools just then, so I did nothing.");
    sendFunctionCallOutput(item.call_id, {
      success: false, error: "bridge unreachable: " + err,
    });
    return;
  }

  // Anything the owner must hear goes through the announcer, which does not
  // depend on the model choosing to verbalize it. Note the ORDER: the output
  // is delivered first (an unresolved function call hangs the conversation),
  // with the ordinary response suppressed, and only then is the scripted
  // response created. Announcing first would create a response against an
  // unresolved call and race the ordinary one.
  //
  // A proposal rides along as `meta.anchor`: it is anchored when the announcer
  // confirms the text was actually SPOKEN, by the model or by the fallback
  // voice — never merely because some response.done happened to carry text.
  // A terminal outcome from the write tools (confirm_terminal — set by the
  // spine for every gated write, so a second declared write reopens the gate
  // too) ends the confirm handshake and reopens the volunteering gate. A wait
  // outcome — pending_transcript, not_announced, or in_flight (#987, a
  // duplicate confirm on a token the runner is already processing) — keeps the
  // proposal live, so it keeps the gate closed; the TTL covers an owner who
  // never answers at all. NOTHING HERE ENUMERATES THEM: the router keys on
  // confirm_terminal, which the spine sets False for exactly this set, so
  // #987 added a third wait outcome without touching a line of dispatch. That
  // is the property to preserve — a client-side list of wait outcomes is the
  // same false-reject trap as a client-side list of tool names.
  // A QUEUED send is also the observable "acted on it" (#967) that
  // retires the target session's reminders — a cancel is not acting, so the
  // reminder stands. The router holds both rules.
  outcomeRouter.route(item.name, result);
  const mustSpeak = !!(result && result.must_speak && result.say);
  sendFunctionCallOutput(item.call_id, result, mustSpeak);
  if (mustSpeak) {
    // `say` is literal text to utter — the one payload that used to carry a
    // model DIRECTIVE here got read aloud verbatim (#950 root cause).
    // `fallback_say`, when present, is the echo-safe text for the
    // browser-voice channel (it never carries a nonce).
    announce(result.say, result.anchor_proposal_id
      ? { anchor: result.anchor_proposal_id } : null, result.fallback_say);
  }
}

function spokenText(output) {
  return (output || [])
    .filter((i) => i.type === "message")
    .flatMap((i) => i.content || [])
    .map((p) => p.transcript || p.text || "")
    .filter(Boolean)
    .join(" ");
}

async function start() {
  $start.disabled = true;
  setStatus("minting session…");
  try {
    // The voice rides the mint, because a voice change IS a new session: the
    // API fixes the voice once the model has emitted audio, and the buddy
    // greets on connect, so there is never a live session whose voice a
    // `session.update` could still change (#1017).
    const session = await post("/mint", { voice: currentVoice });
    if (!session.success) throw new Error(session.error || "mint failed");
    // The bridge is the authority on what it actually minted with — a voice it
    // refused or normalised must show in the picker, not just in the log.
    if (session.voice) { currentVoice = session.voice; $voice.value = session.voice; }
    if (session.voice_persisted === false) {
      log("error", "voice not saved for next time: " + (session.voice_persist_error || ""), "err");
    }
    // The clock's ORIGIN, from the bridge (#978). Seeded here — before the
    // data channel exists, so nothing has emitted an event and nothing has
    // called nextSeq() yet. No `|| 0` default: a bridge that did not answer
    // with a base is broken, and silently restarting at zero is the exact
    // defect this closes.
    //
    // SAFE integer, not merely a number: `typeof Infinity === "number"` is
    // true, and a counter at Infinity never advances and serializes every
    // anchor as `null` — not_announced forever, silently. Anything at or past
    // 2**53 has the same shape without the tell, since ++ stops advancing
    // there. The bridge caps what it hands out (server.MAX_SEQ); this is the
    // half that refuses to run on a base that got past it anyway.
    if (!Number.isSafeInteger(session.seq_base) || session.seq_base < 0) {
      throw new Error("mint returned an unusable sequence base");
    }
    seqCounter = session.seq_base;

    setStatus("requesting microphone…");
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });

    pc = new RTCPeerConnection();
    micStream.getTracks().forEach((t) => pc.addTrack(t, micStream));

    audioEl = new Audio();
    audioEl.autoplay = true;
    pc.ontrack = (e) => { audioEl.srcObject = e.streams[0]; audioAttached = true; maybeGreet(); };

    dc = pc.createDataChannel("oai-events");
    announcer = createAnnouncer({
      send,
      // The non-model fallback. Not a nicety: it is what makes "a refusal
      // always speaks" structurally true rather than dependent on the model
      // choosing to comply, and it costs nothing and no dependency.
      // The last-resort voice. `speechSynthesis.speak()` does NOT throw when it
      // silently fails — it just does nothing — so a try/except around it is
      // "we tried and cannot know". `onend`/`onerror` are the one piece of
      // evidence actually available, and for the mechanism whose entire job is
      // that silence is unacceptable, taking it is cheap.
      speak: (text, onSpokenAloud, onSpeakFailed) => {
        const utterance = new SpeechSynthesisUtterance(text);
        utterance.onend = () => {
          log("speak", "browser voice finished", "tool");
          if (onSpokenAloud) onSpokenAloud();
        };
        utterance.onerror = (event) => {
          // Nothing left to escalate to, but the owner must not be left
          // believing they were told: say so on screen and in the log — AND
          // tell the announcer, which is what makes the retry real. Logging
          // alone left the announcement neither heard nor released (#978
          // item 5).
          log("error", "browser voice FAILED: " + (event && event.error), "err");
          if (onSpeakFailed) onSpeakFailed();
        };
        // No cancel() first: speechSynthesis queues natively, and cancelling
        // here killed the PREVIOUS announcement mid-utterance — under a burst
        // each notice interrupted the last and every one logged
        // "browser voice FAILED: interrupted" (#950 defect 3).
        window.speechSynthesis.speak(utterance);
      },
      // The barge-in cut for the channel above (#1041). cancel() is global —
      // fine, because everything queued in speechSynthesis is this page's own
      // fallback audio, and every bit of it is being talked over.
      stopSpeak: () => window.speechSynthesis.cancel(),
      onSpoken,
      onNotSpoken,
      // The timer's own "never over the owner" re-check (#978 item 3). The
      // gate ran when the announcement was queued; this is read 6-12s later,
      // when it actually speaks.
      ownerSpeaking: () => ownerSpeaking,
      setTimer: (fn, ms) => window.setTimeout(fn, ms),
      clearTimer: (handle) => window.clearTimeout(handle),
      onLog: (kind, detail) => log("speak", kind + ": " + detail, "tool"),
    });
    // The buddy's clock (#962). Its only voice is announce(); its gate is the
    // same state the announcer tracks — the owner not speaking, no response in
    // flight, nothing already queued to be said.
    inboxNotifier = createInboxNotifier({
      fetchInbox: () => post("/tool", { name: "buddy_inbox", arguments: { ack: false } }),
      ackInbox: (through) => post("/tool", { name: "buddy_inbox", arguments: { ack_through: through } }),
      announce: announce,
      canSpeak: () => !ownerSpeaking && !responseActive && !!announcer && announcer.pending() === 0 && !confirmGate.outstanding(),
      // The interrupt tier (#967): an escalation need not wait for the buddy's
      // own chatter to finish. What that buys is narrower than it sounds and
      // the overstatement was in this comment: pre-emption is real only
      // against a VAD response, because announce() cancels an in-flight
      // response. Against an ANNOUNCER item the escalation still queues behind
      // it, plus up to one 6s in-flight deferral and up to three owner-speaking
      // ones. The owner-speaking and confirm-handshake legs of #962 stay
      // unconditional, and the handshake leg gained the announcer half it was
      // missing (#978 item 2): the gate closes at anchored(), so a proposal
      // still queued or mid-flight is a handshake in progress that
      // confirmGate.outstanding() cannot yet see. A live announcer is required
      // too — after stop() this gate could still pass, and announce() with a
      // null announcer speaks with no meta, so an escalation went out never
      // acked and never seen (#978 item 6).
      canInterrupt: () => !ownerSpeaking && !confirmGate.outstanding() && !!announcer && !announcer.anchorPending(),
      reRaise: reRaiseLedger,
      setTimer: (fn, ms) => window.setTimeout(fn, ms),
      clearTimer: (handle) => window.clearTimeout(handle),
      onLog: (kind, detail) => log("speak", kind + ": " + detail, "tool"),
      pollMs: INBOX_POLL_MS,
      seen: heardReplies,
    });
    dc.addEventListener("open", () => { setStatus("listening"); $stop.disabled = false; });
    dc.addEventListener("close", () => setStatus("closed"));
    dc.addEventListener("message", (e) => {
      let payload;
      try { payload = JSON.parse(e.data); } catch (err) {
        // Silent-drop path, closed. A flood of parse failures must not become
        // a flood of speech, so the owner is told once and the rest are logged:
        // the first one is the actionable signal, the rest are the same fact.
        log("error", "unparseable event from the realtime service", "err");
        if (parseFailuresAnnounced === 0) {
          parseFailuresAnnounced = 1;
          announce("I'm getting garbled data from the voice service — I may miss things.");
        }
        return;
      }
      switch (payload.type) {
        // The server's own readiness signal — the session exists and can take
        // a response.create. One leg of the greeting gate (#963).
        case "session.created":
          sessionReady = true;
          maybeGreet();
          // Start the clock only once the session is real — and after the
          // greeting is queued, so the first tick defers behind it.
          if (inboxNotifier) inboxNotifier.start();
          break;
        case "response.created":
          responseActive = true;
          if (announcer) announcer.onResponseCreated();
          break;
        // A CANCELLED response only clears the in-flight flag. It is never
        // evidence of anything: it can carry partial audio that said something
        // else, and our own announcer produces one on every refusal. Treating
        // it as a spoken turn is how the proposal anchor got stolen — hole 2b
        // reintroduced one layer below where the clock fix closed it.
        case "response.cancelled":
          responseActive = false;
          if (announcer) announcer.onResponseCancelled();
          break;
        case "response.done": {
          responseActive = false;
          const output = (payload.response && payload.response.output) || [];
          const said = spokenText(output);
          // The anchor is driven from the announcer's own confirmation that
          // the proposal text was spoken (see onSpoken below), NOT from "the
          // next response.done carrying any text".
          const saidOurScript =
            announcer ? announcer.onResponseDone(said) === true : false;
          // #957: the ASR of an announcement announce() already logged gets
          // the distinct "heard" kind — one utterance, two visibly different
          // entries (and a scripted-vs-spoken divergence stays inspectable).
          // Anything else keeps the plain buddy kind, INCLUDING the model
          // re-speaking a text the fallback already uttered — so a genuine
          // double-speak (#950) still reads as the same line twice.
          if (said) log(saidOurScript ? "heard" : "buddy", said, saidOurScript ? "heard" : "buddy");

          const calls = output.filter((i) => i.type === "function_call");
          // Sequential, never concurrent — two dispatches would race their own
          // response.create against each other. And every pending forward is
          // awaited FIRST: the transcript POST and this tool POST are
          // independent fetches, so without this the gate can be asked about an
          // utterance the ring has not received yet.
          (async () => {
            try {
              await forwardChain;
              for (const c of calls) await handleFunctionCall(c);
            } catch (err) {
              log("error", "dispatch loop failed: " + err, "err");
              announce("Something went wrong handling that, so I did nothing.");
            }
          })();
          break;
        }

        // --- the confirm gate's evidence ------------------------------------
        // speech_started is the INTENT time and the only thing the gate orders
        // on. Not the commit: the commit fires at the END of an utterance, and
        // the barge-in case is the owner starting to speak DURING the proposal
        // and finishing after it — so ordering on the commit approves an
        // approval for a proposal the owner never heard stated. That is the
        // hole the clock change exists to close, and the commit reopens it.
        case "input_audio_buffer.speech_started":
          // The owner is talking. A greeting not yet fired is suppressed for
          // good; one queued or mid-flight is withdrawn — cancelled, never
          // queued behind them (#963). Native barge-in cuts any audio already
          // playing; this kills the QUEUE and the fallback TIMER.
          ownerSpeaking = true;
          greeted = true;
          if (announcer) {
            announcer.cancel((m) => !!(m && m.greeting));
            // BARGE-IN ON THE FALLBACK VOICE (#1041): speechSynthesis is
            // outside the WebRTC path, so native barge-in never stops it —
            // this does, synchronously, inside this event tick.
            const cut = announcer.bargeIn();
            if (cut) log("speak", "barge-in: cut the browser voice (" + cut + " utterance(s))", "tool");
          }
          // Instrumentation for the other regime (#1041): model audio is cut
          // by the SERVER (interrupt_response) and the stream just stops —
          // this line is what lets a "talked through me" report be correlated
          // with what was actually playing.
          if (responseActive) log("speak", "barge-in during a model response — server VAD truncates it", "tool");
          if (payload.item_id) {
            const startSeq = nextSeq();
            speechSeq[payload.item_id] = startSeq;
            forward("/utterance", {
              item_id: payload.item_id, speech_started_seq: startSeq,
            });
          }
          break;
        // The commit still matters — it binds the item and makes the ordering
        // choice inspectable — but it never gates.
        case "input_audio_buffer.committed":
          ownerSpeaking = false;
          if (payload.item_id) {
            const seq = nextSeq();
            commitSeq[payload.item_id] = seq;
            forward("/utterance", { item_id: payload.item_id, commit_seq: seq });
          }
          break;
        case "conversation.item.input_audio_transcription.completed":
          if (payload.transcript) log("you", payload.transcript, "you");
          if (payload.item_id) {
            forward("/utterance", {
              item_id: payload.item_id,
              transcript: payload.transcript || "",
              speech_started_seq: speechSeq[payload.item_id] || 0,
              commit_seq: commitSeq[payload.item_id] || 0,
            });
          }
          break;

        case "error": {
          // Was DOM-only, i.e. silent to the ear. An error the owner cannot
          // hear about is one they will keep talking into.
          log("error", JSON.stringify(payload), "err");
          // Our OWN best-effort cancel produces this one when the response it
          // aimed at already finished. Announcing an error the announcer
          // itself generated is the announce → cancel → error → announce loop
          // (#950 defect 2): nothing was missed, nothing to say.
          const code = payload.error && payload.error.code;
          if (code === "response_cancel_not_active") break;
          if (!errorNoticePending) {
            errorNoticePending = true;
            announce("The voice service reported an error, so I may have missed that.",
              { errorNotice: true });
          }
          break;
        }
      }
    });

    setStatus("connecting…");
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const answer = await fetch(CALLS_URL, {
      method: "POST",
      body: offer.sdp,
      headers: {
        "Authorization": "Bearer " + session.client_secret,
        "Content-Type": "application/sdp",
      },
    });
    if (!answer.ok) {
      // Read the body BEFORE throwing: OpenAI delivers the real reason there
      // ("You have no credits remaining"), and discarding it reduced a
      // terminal condition to a retry-me status code (#1037).
      const bodyText = await answer.text().catch(() => "");
      throw new Error(describeApiFailure("realtime connect failed", answer.status, bodyText));
    }
    await pc.setRemoteDescription({ type: "answer", sdp: await answer.text() });
  } catch (err) {
    log("error", String(err && err.message || err), "err");
    setStatus("error");
    stop();
  }
}

function stop() {
  if (dc) { dc.close(); dc = null; }
  if (pc) { pc.close(); pc = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  audioEl = null;
  responseActive = false;
  // TORN DOWN, not merely dropped (#978 item 4). Nulling the reference left
  // the current item's armed setTimeout closure alive: 6s into "idle" the
  // browser voice spoke and anchored a proposal on the bridge, closing the
  // next session's volunteering gate for up to 120s. d45601b covered the
  // page-lifetime strays array (itself deleted in #970); `disarm` was
  // reachable only from onResponseDone and cancel, neither of which stop()
  // calls.
  if (announcer) announcer.teardown();
  announcer = null;
  // Session-scoped readiness resets; `greeted` deliberately does NOT — a
  // reconnect must stay quiet (#963).
  sessionReady = false;
  audioAttached = false;
  ownerSpeaking = false;
  // The clock dies with the session; `confirmGate` deliberately survives — the
  // spine lives in the bridge, so a proposal outlasts a stop() and its TTL is
  // what reopens the gate. `heardReplies` survives so
  // the fresh notifier after a reconnect cannot replay a spoken notice (#962),
  // and the re-raise ledger survives with it (#967): what the owner was told
  // is page-lifetime state, or a reconnect wipes the peer's memory of its own
  // words and every pending reminder dies with it.
  if (inboxNotifier) { inboxNotifier.stop(); inboxNotifier = null; }
  // A notice pending when the session died was never going to be spoken by
  // it — carrying the flag into the next session would suppress that
  // session's FIRST error notice, silently.
  errorNoticePending = false;
  $start.disabled = false;
  $stop.disabled = true;
  setStatus("idle");
}

// One click, and the reconnect is the mechanism rather than the price (#1017).
// Before this, changing voice meant Ctrl-C the bridge, re-serve with a
// different --voice, and reload the page by hand — three steps for a setting
// with ten values. It cannot be a `session.update`: the Realtime API fixes the
// voice once the model has emitted audio, and the buddy greets on connect.
//
// The greet latch is deliberately RELEASED here, against the #963 rule that a
// reconnect stays quiet. That rule is about a reconnect the owner did not ask
// for, where a re-greet is noise. This one they asked for, and the entire
// observable result of it is how the buddy SOUNDS — a silent switch is
// indistinguishable from a switch that did not happen, in a channel where the
// owner has no screen. So it speaks, in the new voice, which is the answer.
async function switchVoice() {
  const chosen = $voice.value;
  if (chosen === currentVoice) return;
  currentVoice = chosen;
  const wasLive = !!pc;
  log("speak", "voice → " + chosen, "tool");
  // ABOVE the idle return, not inside the live branch. `stop()` deliberately
  // leaves `greeted` set (#963), so the ordinary Stop → pick → Start sequence
  // would otherwise connect on the new voice and say nothing at all — the
  // silent switch this whole gesture exists to avoid, reached by the calmer
  // of the two routes to it. The asymmetry had no reason: the owner asked for
  // the change in both cases, and in both cases hearing it is the answer.
  greeted = false;
  if (!wasLive) { setStatus("idle · " + chosen); return; }
  // Locked for the round trip: a second change mid-reconnect would tear down
  // the session the first one is still building.
  $voice.disabled = true;
  try {
    stop();
    await start();
  } finally {
    $voice.disabled = false;
  }
}

$start.addEventListener("click", start);
$stop.addEventListener("click", stop);
$voice.addEventListener("change", switchVoice);
</script>
</body>
</html>
"""


def announcer_source() -> str:
    """The announcer factory on its own, for the node-driven data-channel tests.

    Exported rather than re-extracted by the test, so the code under test is
    byte-identical to the code in the page. A test that re-derives its subject
    from a copy proves something about the copy.
    """
    return ANNOUNCER_JS


def notifier_source() -> str:
    """The inbox-notifier factory on its own, for the node-driven tests.

    Same rule as :func:`announcer_source`: the code under test is
    byte-identical to the code in the page.
    """
    return INBOX_NOTIFIER_JS


def reraise_source() -> str:
    """The re-raise ledger on its own, for the node-driven tests.

    Same rule as :func:`announcer_source`: the code under test is
    byte-identical to the code in the page.
    """
    return RERAISE_JS


def outcome_router_source() -> str:
    """The write-outcome router on its own, for the node-driven tests.

    Same rule as :func:`announcer_source`: the code under test is
    byte-identical to the code in the page.
    """
    return OUTCOME_ROUTER_JS


def confirm_gate_source() -> str:
    """The confirm-outstanding gate on its own, for the node-driven tests.

    Same rule as :func:`announcer_source`: the code under test is
    byte-identical to the code in the page.
    """
    return CONFIRM_GATE_JS


def voice_options(selected: str) -> str:
    """The picker's ``<option>`` list, from the one enumeration (#1017).

    Rendered server-side from :data:`realtime.VOICES` rather than built in JS
    from an injected array: the list the owner picks from is then the same list
    the bridge validates against, with no second copy to drift. The two the
    docs single out are labelled, so the choice reads as a recommendation
    rather than ten equal strings.

    The "(newer)" label is derived from the ORDER of :data:`realtime.VOICES`
    rather than from a second hard-coded pair — a re-encoded claim next to the
    thing it claims about is the drift this module keeps closing.
    """
    newer = realtime.VOICES[:2]
    return "".join(
        '<option value="{v}"{sel}>{label}</option>'.format(
            v=html.escape(voice),
            sel=" selected" if voice == selected else "",
            label=html.escape(f"{voice} (newer)" if voice in newer else voice),
        )
        for voice in realtime.VOICES
    )


def page(buddy: str, token: str, voice: str = "") -> str:
    """Render the client page for one buddy + one run token.

    *voice* is what the bridge resolved, so a reload shows the voice actually
    in use rather than the default.
    """
    voice = voice or realtime.DEFAULT_VOICE
    return (
        _PAGE.replace("__ANNOUNCER__", ANNOUNCER_JS)
        .replace("__NOTIFIER__", INBOX_NOTIFIER_JS)
        .replace("__CONFIRM_GATE__", CONFIRM_GATE_JS)
        .replace("__RERAISE__", RERAISE_JS)
        .replace("__OUTCOME_ROUTER__", OUTCOME_ROUTER_JS)
        .replace("__BUDDY__", html.escape(buddy))
        .replace("__VOICE_OPTIONS__", voice_options(voice))
        .replace("__VOICE__", json.dumps(voice))
        .replace("__TOKEN__", json.dumps(token))
    )
