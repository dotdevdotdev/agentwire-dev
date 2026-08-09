"""The buddy's browser client — WebRTC to OpenAI Realtime (spike).

Embedded as a string rather than shipped as a static file on purpose: this is
branch-only, and adding it to ``agentwire/static/`` would put it one import
away from the portal, which is exactly the coupling this spike must not create.
No packaging change, no portal change.

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

- an utterance at ``input_audio_buffer.committed`` (the audio boundary);
- a proposal at the ``response.done`` of the turn in which the buddy SPOKE it,
  which is when the owner actually heard what they would be approving. Barge-in
  is native here, so anchoring at tool-call time would let an interrupting
  approval land on a proposal that was never stated.

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
  var setTimer = deps.setTimer;
  var clearTimer = deps.clearTimer;
  var onLog = deps.onLog || function () {};
  // Called with (meta, how) the moment there is EVIDENCE the text was spoken —
  // "model" when a response.done carried it, "fallback" when the browser voice
  // said it. This is what the proposal anchor is driven from: see the client's
  // onSpoken handler and BLOCKING 2 in the phase-2 review.
  var onSpoken = deps.onSpoken || function () {};
  // How long to wait for the model to actually say it before falling back to
  // the browser's own speech synthesis. Generous enough for a normal spoken
  // turn, short enough that the owner is not left in silence wondering.
  var fallbackMs = deps.fallbackMs || 6000;

  var queue = [];
  // { text, fallbackText, meta, timer, sawCreate, deferred }
  var current = null;
  var responseActive = false;

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
    var want = norm(text);
    if (!want.length) return true;
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
      var say = item.fallbackText || item.text;
      try { speak(say, function () { onSpoken(item.meta, "fallback"); }); }
      catch (e) { onSpoken(item.meta, "fallback"); }
      pump();
    }, fallbackMs);
  }

  function disarm(item) {
    if (item && item.timer) { clearTimer(item.timer); item.timer = null; }
  }

  function pump() {
    if (current || !queue.length) return;
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
      });
      pump();
    },
    // Withdraw announcements whose meta matches, queued or current (#963: the
    // owner speaking first CANCELS the greeting; queueing it behind them would
    // greet someone who has already moved on). A withdrawn item's fallback is
    // disarmed and onSpoken never fires for it — it was never heard, and
    // nothing downstream may believe it was. Note what this does NOT
    // establish: it cannot recall audio the model has already emitted; native
    // barge-in covers that, this covers the QUEUE and the TIMER.
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
    // Test/inspection surface.
    pending: function () { return (current ? 1 : 0) + queue.length; },
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
//   ackInbox()   -> Promise<{success, messages}>  read + advance the cursor
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
//   strays: [] page-lifetime array of acked-but-never-spoken replies (the
//              peek/ack race — see noticeSpoken). The cursor is already past
//              them, so the spool will NEVER return them again: this array is
//              their only way back to the owner's ear, and giving it to the
//              notifier to own meant a stop() before the next gated tick took
//              it to the grave. Passed in for the same reason `seen` is.
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
  var strays = deps.strays;
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

  function composeNotice(messages) {
    // An escalation in the batch names itself as one — the owner should be
    // able to hear the difference between news and an alarm.
    var prefix = messages.some(isUrgent) ? "Heads up \\u2014 " : "";
    if (messages.length === 1) {
      var m = messages[0];
      var verb = isUrgent(m) ? " escalated: " : " got back to you: ";
      return prefix + (m.from || "someone") + verb + trimBody(m.text);
    }
    var parts = messages.map(function (msg) {
      return "From " + (msg.from || "someone") + ": " + trimBody(msg.text);
    });
    return prefix + messages.length + " updates came in. " + parts.join(" ");
  }

  function pollOnce() {
    return fetchInbox().then(function (res) {
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
      // Strays: pull only what this tick may speak; the rest STAY in the
      // array — a stray is cursor-past, so dropping one here loses it.
      var pulled = [];
      for (var i = 0; i < strays.length; ) {
        if (take(strays[i])) pulled.push(strays.splice(i, 1)[0]);
        else i++;
      }
      var claimed = {};
      var fresh = pulled.concat((res.messages || []).filter(take)).filter(function (m) {
        if (!m || !m.id || seen[m.id] || inFlight[m.id] || claimed[m.id]) return false;
        claimed[m.id] = true;
        return true;
      });
      if (!fresh.length) {
        // A quiet full-gate tick is the natural gap a re-raise waits for.
        // Never on the interrupt tier: a reminder is not an alarm.
        if (full && reRaise) {
          var reminder = reRaise.dueText();
          if (reminder) {
            onLog("reraise", "second mention");
            announce(reminder, { reRaise: true });
          }
        }
        return;
      }
      var ids = fresh.map(function (m) { return m.id; });
      ids.forEach(function (id) { inFlight[id] = true; });
      onLog("inbox", "volunteering " + fresh.length + " message(s)");
      // inboxMsgs rides along so the page can register request/escalation
      // notices in the re-raise ledger AT THE MOMENT THEY ARE HEARD (its
      // onSpoken) — the ledger must never hold something the owner wasn't
      // actually told.
      announce(composeNotice(fresh), {
        inboxIds: ids,
        inboxMsgs: fresh.map(function (m) {
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
    return ackInbox().then(function (res) {
      if (!res || !res.success) {
        // Cursor not advanced: a page reload will re-read these. `seen`
        // suppresses a replay for THIS page's lifetime, which is the right
        // half to keep — re-reading is an annoyance, losing one is the bug.
        onLog("inbox", "ack failed: " + ((res && res.error) || "no response"));
        return;
      }
      // The ack read everything unread — including anything that landed
      // between our peek and now. Those were acked but never spoken; queue
      // them for the next tick's gate check rather than announcing here
      // (announcing outside the gate is barging in by another door).
      (res.messages || []).forEach(function (m) {
        if (m && m.id && !seen[m.id] && !inFlight[m.id]) strays.push(m);
      });
    }).catch(function (err) {
      onLog("inbox", "ack failed: " + err);
    });
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
    // The one output: text for everything due, or null. Marks what it returns
    // as re-raised, so a due item speaks exactly once — the CALLER decides
    // when a gap is a gap (the notifier's full gate), this only decides what
    // is due.
    dueText: function () {
      var due = Object.keys(items).map(function (id) { return items[id]; })
        .filter(function (it) {
          return !it.resolved && !it.reRaised && now() - it.at >= dueMs;
        });
      if (!due.length) return null;
      due.forEach(function (it) { it.reRaised = true; });
      var parts = due.map(function (it) { return it.from + " asked: " + it.text; });
      return "Still open from earlier — " + parts.join(" And ") +
        " Nothing has gone their way since. Second mention, so I'll leave it with you.";
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
  <span id="status">idle</span>
</div>
<div id="log"></div>
<script>
__ANNOUNCER__
__NOTIFIER__
__CONFIRM_GATE__
__RERAISE__

const TOKEN = __TOKEN__;
const CALLS_URL = "https://api.openai.com/v1/realtime/calls";

const $log = document.getElementById("log");
const $status = document.getElementById("status");
const $start = document.getElementById("start");
const $stop = document.getElementById("stop");

let pc = null, dc = null, micStream = null, audioEl = null;
let responseActive = false;

// --- conversation-item time -------------------------------------------------
// The confirm gate's ordering predicate lives on this counter, not on a clock.
// See the module docstring: a wall-clock comparison is between a receipt time
// and an intent time, and it silently inverts.
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
// Page-lifetime, like heardReplies: acked-but-never-spoken replies (the
// peek/ack race). The cursor is already past them and the spool has no
// ack-by-id, so this array is their only way back to the owner's ear — a
// stop() or reconnect must not take it down with the notifier. A full page
// unload still loses whatever is here; closing that residual needs an
// ack-by-id the inbox does not offer.
const strayReplies = [];
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
// The target of the most recent proposal — the only place the client can
// learn which session a confirmed send actually went to, because the send
// outcome deliberately carries no parameters (the argv is frozen at propose
// time). A confirm always executes the proposal whose token it holds, which
// in practice is the last one proposed; the mismatch window (confirming an
// older proposal after a newer propose) costs at most one wrongly-suppressed
// or one extra mention, never a lost message.
let lastProposedSession = null;

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
  // A terminal outcome from the write tools ends the confirm handshake and
  // reopens the volunteering gate. A wait outcome (owner_should_wait —
  // pending_transcript / not_announced) keeps the proposal live, so it keeps
  // the gate closed; the TTL covers an owner who never answers at all.
  if (item.name === "propose_session_message" && result && result.success && result.session) {
    lastProposedSession = result.session;
  }
  if (
    (item.name === "send_session_message" || item.name === "cancel_session_message")
    && result && !result.owner_should_wait
  ) {
    confirmGate.resolved();
    // A QUEUED send is the observable "acted on it" (#967): something the
    // owner was asked for actually went that session's way, so its pending
    // reminders retire. A cancel is not acting — the reminder stands.
    if (item.name === "send_session_message" && result.success && lastProposedSession) {
      reRaiseLedger.actedOn(lastProposedSession);
    }
  }
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
    const session = await post("/mint", {});
    if (!session.success) throw new Error(session.error || "mint failed");

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
      speak: (text, onSpokenAloud) => {
        const utterance = new SpeechSynthesisUtterance(text);
        utterance.onend = () => {
          log("speak", "browser voice finished", "tool");
          if (onSpokenAloud) onSpokenAloud();
        };
        utterance.onerror = (event) => {
          // Nothing left to escalate to, but the owner must not be left
          // believing they were told: say so on screen and in the log.
          log("error", "browser voice FAILED: " + (event && event.error), "err");
        };
        // No cancel() first: speechSynthesis queues natively, and cancelling
        // here killed the PREVIOUS announcement mid-utterance — under a burst
        // each notice interrupted the last and every one logged
        // "browser voice FAILED: interrupted" (#950 defect 3).
        window.speechSynthesis.speak(utterance);
      },
      onSpoken,
      setTimer: (fn, ms) => window.setTimeout(fn, ms),
      clearTimer: (handle) => window.clearTimeout(handle),
      onLog: (kind, detail) => log("speak", kind + ": " + detail, "tool"),
    });
    // The buddy's clock (#962). Its only voice is announce(); its gate is the
    // same state the announcer tracks — the owner not speaking, no response in
    // flight, nothing already queued to be said.
    inboxNotifier = createInboxNotifier({
      fetchInbox: () => post("/tool", { name: "buddy_inbox", arguments: { ack: false } }),
      ackInbox: () => post("/tool", { name: "buddy_inbox", arguments: { ack: true } }),
      announce: announce,
      canSpeak: () => !ownerSpeaking && !responseActive && !!announcer && announcer.pending() === 0 && !confirmGate.outstanding(),
      // The interrupt tier (#967): an escalation may pre-empt the buddy's own
      // speech — announce() cancels an in-flight response, which is the
      // existing mechanism, not a new voice — but the owner-speaking and
      // confirm-handshake legs of #962 stay unconditional.
      canInterrupt: () => !ownerSpeaking && !confirmGate.outstanding(),
      reRaise: reRaiseLedger,
      setTimer: (fn, ms) => window.setTimeout(fn, ms),
      clearTimer: (handle) => window.clearTimeout(handle),
      onLog: (kind, detail) => log("speak", kind + ": " + detail, "tool"),
      pollMs: INBOX_POLL_MS,
      seen: heardReplies,
      strays: strayReplies,
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
          if (announcer) announcer.cancel((m) => !!(m && m.greeting));
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
    if (!answer.ok) throw new Error("realtime connect failed (" + answer.status + ")");
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
  announcer = null;
  // Session-scoped readiness resets; `greeted` deliberately does NOT — a
  // reconnect must stay quiet (#963).
  sessionReady = false;
  audioAttached = false;
  ownerSpeaking = false;
  // The clock dies with the session; `heardReplies` and `strayReplies`
  // deliberately survive (a stray is cursor-past — losing the array loses the
  // message for good), and `confirmGate` survives too: the spine lives in the
  // bridge, so a proposal outlasts a stop() and its TTL is what reopens the
  // gate. `heardReplies` survives so
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

$start.addEventListener("click", start);
$stop.addEventListener("click", stop);
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


def confirm_gate_source() -> str:
    """The confirm-outstanding gate on its own, for the node-driven tests.

    Same rule as :func:`announcer_source`: the code under test is
    byte-identical to the code in the page.
    """
    return CONFIRM_GATE_JS


def page(buddy: str, token: str) -> str:
    """Render the client page for one buddy + one run token."""
    return (
        _PAGE.replace("__ANNOUNCER__", ANNOUNCER_JS)
        .replace("__NOTIFIER__", INBOX_NOTIFIER_JS)
        .replace("__CONFIRM_GATE__", CONFIRM_GATE_JS)
        .replace("__RERAISE__", RERAISE_JS)
        .replace("__BUDDY__", html.escape(buddy))
        .replace("__TOKEN__", json.dumps(token))
    )
