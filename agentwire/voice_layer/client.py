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
"""

from __future__ import annotations

import html
import json

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
  <span class="tag">spike · read-only</span>
</header>
<div>
  <button id="start">Start talking</button>
  <button id="stop" class="stop" disabled>Stop</button>
  <span id="status">idle</span>
</div>
<div id="log"></div>
<script>
const TOKEN = __TOKEN__;
const CALLS_URL = "https://api.openai.com/v1/realtime/calls";

const $log = document.getElementById("log");
const $status = document.getElementById("status");
const $start = document.getElementById("start");
const $stop = document.getElementById("stop");

let pc = null, dc = null, micStream = null, audioEl = null;
let responseActive = false;

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

function send(event) {
  if (!dc || dc.readyState !== "open") return false;
  dc.send(JSON.stringify(event));
  return true;
}

// The server rejects a response.create while one is in flight, and VAD creates
// its own responses — this is what keeps ours from racing them.
function maybeCreateResponse() {
  if (responseActive) return false;
  return send({ type: "response.create" });
}

function sendFunctionCallOutput(callId, output) {
  send({
    type: "conversation.item.create",
    item: { type: "function_call_output", call_id: callId, output: JSON.stringify(output) },
  });
  maybeCreateResponse();
}

async function handleFunctionCall(item) {
  let args = {};
  try { args = item.arguments ? JSON.parse(item.arguments) : {}; }
  catch { sendFunctionCallOutput(item.call_id, { error: "malformed arguments JSON" }); return; }
  log("tool", item.name + " " + JSON.stringify(args), "tool");
  const result = await post("/tool", { name: item.name, arguments: args });
  sendFunctionCallOutput(item.call_id, result);
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
    dc.addEventListener("open", () => { setStatus("listening"); $stop.disabled = false; });
    dc.addEventListener("close", () => setStatus("closed"));
    dc.addEventListener("message", (e) => {
      let payload;
      try { payload = JSON.parse(e.data); } catch { return; }
      switch (payload.type) {
        case "response.created":
          responseActive = true;
          break;
        case "response.done":
        case "response.cancelled": {
          responseActive = false;
          const output = (payload.response && payload.response.output) || [];
          const said = spokenText(output);
          if (said) log("buddy", said, "buddy");
          const calls = output.filter((i) => i.type === "function_call");
          // Sequential, never concurrent — two dispatches would race their own
          // response.create against each other.
          (async () => { for (const c of calls) await handleFunctionCall(c); })();
          break;
        }
        case "conversation.item.input_audio_transcription.completed":
          if (payload.transcript) log("you", payload.transcript, "you");
          break;
        case "error":
          log("error", JSON.stringify(payload), "err");
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


def page(buddy: str, token: str) -> str:
    """Render the client page for one buddy + one run token."""
    return _PAGE.replace("__BUDDY__", html.escape(buddy)).replace(
        "__TOKEN__", json.dumps(token)
    )
