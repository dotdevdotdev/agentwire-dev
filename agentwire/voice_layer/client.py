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
  // How long to wait for the model to actually say it before falling back to
  // the browser's own speech synthesis. Generous enough for a normal spoken
  // turn, short enough that the owner is not left in silence wondering.
  var fallbackMs = deps.fallbackMs || 6000;

  var queue = [];
  var current = null;       // { text, timer }
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
      onLog("fallback", "no spoken confirmation within " + fallbackMs + "ms");
      if (current === item) current = null;
      try { speak(item.text); } catch (e) { /* nothing left to try */ }
      pump();
    }, fallbackMs);
  }

  function disarm(item) {
    if (item && item.timer) { clearTimer(item.timer); item.timer = null; }
  }

  function pump() {
    if (current || !queue.length) return;
    current = { text: queue.shift(), timer: null };
    var item = current;

    // Armed FIRST, before anything that could fail silently.
    armFallback(item);

    // Best-effort cancel. response.cancel against an already-finished response
    // is an error — ignore it and carry on: the timer is what guarantees the
    // announcement, so nothing here needs to succeed for the owner to be told.
    send({ type: "response.cancel" });

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
    announce: function (text) {
      if (!text) return;
      queue.push(String(text));
      pump();
    },
    onResponseCreated: function () { responseActive = true; },
    onResponseDone: function (transcript) {
      responseActive = false;
      if (!current) { pump(); return; }
      // The ONLY disarm: positive evidence that the reason was spoken.
      if (carriedTheReason(transcript, current.text)) {
        disarm(current);
        current = null;
        pump();
      }
      // Otherwise leave the timer armed. A response that said something else
      // is not evidence the owner heard the refusal.
    },
    // Test/inspection surface.
    pending: function () { return (current ? 1 : 0) + queue.length; },
    armed: function () { return !!(current && current.timer); },
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
let pendingAnchor = null;    // proposal id awaiting the response.done that spoke it
// Every forward to the bridge is chained, and tool dispatch awaits the chain.
// Without this the transcript POST and the confirm POST are independent fetches
// that can land reordered, and the gate evaluates against a ring that has not
// received the utterance it is about to be asked about.
let forwardChain = Promise.resolve();
let announcer = null;
let parseFailuresAnnounced = 0;

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
// anything that changes what they should do next.
function announce(text) {
  log("buddy", text, "buddy");
  if (announcer) announcer.announce(text);
  else { try { window.speechSynthesis.speak(new SpeechSynthesisUtterance(text)); } catch (e) {} }
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

  // A proposal is anchored to the response.done of the turn that SPEAKS it,
  // not to this tool call — the owner has not heard it yet.
  if (result && result.anchor_proposal_id) pendingAnchor = result.anchor_proposal_id;

  // Anything the owner must hear goes through the announcer, which does not
  // depend on the model choosing to verbalize it. Note the ORDER: the output
  // is delivered first (an unresolved function call hangs the conversation),
  // with the ordinary response suppressed, and only then is the scripted
  // response created. Announcing first would create a response against an
  // unresolved call and race the ordinary one.
  const mustSpeak = !!(result && result.must_speak && result.say);
  sendFunctionCallOutput(item.call_id, result, mustSpeak);
  if (mustSpeak) announce(result.say);
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
    pc.ontrack = (e) => { audioEl.srcObject = e.streams[0]; };

    dc = pc.createDataChannel("oai-events");
    announcer = createAnnouncer({
      send,
      // The non-model fallback. Not a nicety: it is what makes "a refusal
      // always speaks" structurally true rather than dependent on the model
      // choosing to comply, and it costs nothing and no dependency.
      speak: (text) => {
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
      },
      setTimer: (fn, ms) => window.setTimeout(fn, ms),
      clearTimer: (handle) => window.clearTimeout(handle),
      onLog: (kind, detail) => log("speak", kind + ": " + detail, "tool"),
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
        case "response.created":
          responseActive = true;
          if (announcer) announcer.onResponseCreated();
          break;
        case "response.done":
        case "response.cancelled": {
          responseActive = false;
          const output = (payload.response && payload.response.output) || [];
          const said = spokenText(output);
          if (said) log("buddy", said, "buddy");
          if (announcer) announcer.onResponseDone(said);

          // Conversation-item time for the buddy's own spoken turn. A proposal
          // is anchored HERE — the moment the owner has actually heard what
          // they would be approving — not at the tool call that minted it.
          const doneSeq = nextSeq();
          if (pendingAnchor && said) {
            const proposalId = pendingAnchor;
            pendingAnchor = null;
            forward("/anchor", { proposal_id: proposalId, seq: doneSeq });
          }

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

        case "error":
          // Was DOM-only, i.e. silent to the ear. An error the owner cannot
          // hear about is one they will keep talking into.
          log("error", JSON.stringify(payload), "err");
          announce("The voice service reported an error, so I may have missed that.");
          break;
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
  pendingAnchor = null;
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


def page(buddy: str, token: str) -> str:
    """Render the client page for one buddy + one run token."""
    return (
        _PAGE.replace("__ANNOUNCER__", ANNOUNCER_JS)
        .replace("__BUDDY__", html.escape(buddy))
        .replace("__TOKEN__", json.dumps(token))
    )
