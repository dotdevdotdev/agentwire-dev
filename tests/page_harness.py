"""Run the REAL buddy page script under node, against a fake browser.

The page's factories (announcer, tracer, …) have their own node tests, but the
WIRING — the data-channel handler inside ``start()``, the batch dispatch, the
response.create decisions — only exists in the served page. This runs that page
byte-for-byte (the ``<script>`` body of :func:`agentwire.voice_layer.client.page`)
against fakes of exactly what it touches: DOM handles, ``window`` timers on a
VIRTUAL clock, ``RTCPeerConnection`` + data channel, ``fetch`` to the bridge,
``speechSynthesis``. No network, no real time — a 2-minute TTL is ``advance(120000)``.

The driver is JavaScript run after the page loads, with these helpers:

- ``await connect()`` — click Start, wire the fake WebRTC, open the channel,
  send ``session.created`` (the greeting then queues, as it does live).
- ``serverEvent(e)`` — deliver one Realtime server event on the data channel.
- ``await modelResponds({text, calls})`` — a whole model turn:
  ``response.created`` then ``response.done`` with that output.
- ``await ownerSays(text, itemId)`` — speech_started → committed →
  transcription.completed, as server VAD emits them.
- ``await advance(ms)`` — run virtual time, firing due timers.
- ``await drainTrace()`` — flush the page's tracer into ``bridge.traces``.
- ``toolHandler = (name, args) => result`` — the bridge's ``/tool`` answer.

and these records: ``wire`` (every event the page SENT on the data channel),
``bridge`` (``traces``, ``tools``, ``utterances``, ``anchors``), ``spokenFallback``.
The driver ends by assigning ``result``; :func:`run_page` returns it.
"""

from __future__ import annotations

import json
import re
import subprocess

from agentwire.voice_layer import client

_PRELUDE = r"""
let __now = 1700000000000;
Date.now = () => __now;
const __timers = [];
let __tid = 0;
function __setTimeout(fn, ms) { const id = ++__tid; __timers.push({ id, at: __now + (ms || 0), fn, every: 0 }); return id; }
function __setInterval(fn, ms) { const id = ++__tid; __timers.push({ id, at: __now + ms, fn, every: ms }); return id; }
function __clear(id) { const i = __timers.findIndex((t) => t.id === id); if (i >= 0) __timers.splice(i, 1); }
async function settle() { for (let i = 0; i < 25; i++) await new Promise((r) => setImmediate(r)); }
async function advance(ms) {
  const end = __now + ms;
  for (;;) {
    __timers.sort((a, b) => a.at - b.at);
    const t = __timers[0];
    if (!t || t.at > end) break;
    __now = t.at;
    if (t.every) t.at += t.every; else __timers.shift();
    t.fn();
    await settle();
  }
  __now = end;
  await settle();
}

function __el() {
  return { textContent: "", disabled: false, value: "", className: "", scrollTop: 0,
    scrollHeight: 0, children: [], append(...c) { this.children.push(...c); },
    addEventListener() {} };
}
const __els = {};
const __winHandlers = {};
const spokenFallback = [];
function __define(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
}
__define("document", {
  getElementById: (id) => (__els[id] = __els[id] || __el()),
  createElement: () => __el(),
});
__define("window", {
  addEventListener: (t, fn) => { (__winHandlers[t] = __winHandlers[t] || []).push(fn); },
  setTimeout: __setTimeout, clearTimeout: __clear,
  setInterval: __setInterval, clearInterval: __clear,
  speechSynthesis: {
    speak(u) { spokenFallback.push(u.text); if (u.onend) __setTimeout(() => u.onend(), 10); },
    cancel() {},
  },
});
__define("navigator", {
  userAgent: "page-harness",
  mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) },
});
__define("SpeechSynthesisUtterance", class { constructor(text) { this.text = text; } });
__define("Audio", class { constructor() { this.autoplay = false; this.srcObject = null; } });

const wire = [];
let __dc = null, __pc = null, __rid = 0;
class __FakeDC {
  constructor() { this.readyState = "connecting"; this.h = {}; }
  addEventListener(t, fn) { (this.h[t] = this.h[t] || []).push(fn); }
  send(s) { wire.push(JSON.parse(s)); }
  close() { this.readyState = "closed"; this.fire("close", {}); }
  fire(t, e) { (this.h[t] || []).forEach((f) => f(e)); }
}
__define("RTCPeerConnection", class {
  constructor() { __pc = this; this.connectionState = "new"; this.iceConnectionState = "new"; this.h = {}; }
  addTrack() {}
  createDataChannel() { __dc = new __FakeDC(); return __dc; }
  addEventListener(t, fn) { (this.h[t] = this.h[t] || []).push(fn); }
  async createOffer() { return { sdp: "offer" }; }
  async setLocalDescription() {}
  async setRemoteDescription() {}
  close() { this.connectionState = "closed"; }
});

const bridge = { traces: [], tools: [], utterances: [], anchors: [] };
let toolHandler = () => ({ success: true });
__define("fetch", async (url, opts) => {
  const reply = (o, status = 200) => ({
    ok: status < 400, status, json: async () => o, text: async () => JSON.stringify(o),
  });
  if (String(url).startsWith("https://")) return { ok: true, status: 200, text: async () => "answer" };
  const b = opts && opts.body ? JSON.parse(opts.body) : {};
  if (url === "/trace") { bridge.traces.push(...b.events); return reply({ success: true }); }
  if (url === "/mint") return reply({ success: true, seq_base: 1000000, voice: "marin", client_secret: "ek_harness" });
  if (url === "/tool") { bridge.tools.push(b); return reply(toolHandler(b.name, b.arguments || {})); }
  if (url === "/utterance") { bridge.utterances.push(b); return reply({ success: true }); }
  if (url === "/anchor") { bridge.anchors.push(b); return reply({ success: true, anchored: true }); }
  return reply({ success: false }, 404);
});
"""

_HELPERS = r"""
function serverEvent(e) { __dc.fire("message", { data: JSON.stringify(e) }); }
async function connect() {
  const started = start();
  await settle();
  __pc.ontrack({ streams: [{}] });
  __dc.readyState = "open";
  __dc.fire("open", {});
  serverEvent({ type: "session.created" });
  await started;
  await settle();
}
async function modelResponds({ text = "", calls = [] } = {}) {
  const id = "resp_" + (++__rid);
  serverEvent({ type: "response.created", response: { id, status: "in_progress", output: [] } });
  const output = [];
  if (text) output.push({ type: "message", content: [{ type: "output_audio", transcript: text }] });
  calls.forEach((c, i) => output.push({
    type: "function_call", name: c.name, call_id: c.call_id || ("call_" + id + "_" + i),
    arguments: JSON.stringify(c.args || {}),
  }));
  serverEvent({ type: "response.done", response: { id, status: "completed", output } });
  await settle();
}
async function ownerSays(text, itemId) {
  serverEvent({ type: "input_audio_buffer.speech_started", item_id: itemId });
  serverEvent({ type: "input_audio_buffer.committed", item_id: itemId });
  serverEvent({ type: "conversation.item.input_audio_transcription.completed", item_id: itemId, transcript: text });
  await settle();
}
async function drainTrace() {
  for (let i = 0; i < 200 && tracer.pending(); i++) { tracer.flush(); await settle(); }
}
// The last scripted (announcer) or plain create the page sent, and since when.
function creates() { return wire.filter((e) => e.type === "response.create"); }
let result = null;
"""


def page_script() -> str:
    """The ``<script>`` body the bridge serves, verbatim."""
    html = client.page("buddy", "tok", voice="marin")
    match = re.search(r"<script>([\s\S]*)</script>", html)
    assert match, "the page has no <script> — the harness is stale"
    return match.group(1)


def run_page(driver: str, *, timeout: float = 10) -> dict:
    """Run *driver* against the real page; return what it assigned to ``result``."""
    program = "\n".join([
        _PRELUDE,
        page_script(),
        _HELPERS,
        "(async () => {",
        driver,
        "console.log('__RESULT__' + JSON.stringify(result));",
        "})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });",
    ])
    out = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=timeout)
    assert out.returncode == 0, out.stderr[-3000:]
    lines = [line for line in out.stdout.splitlines() if line.startswith("__RESULT__")]
    assert lines, f"driver never reported a result:\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}"
    return json.loads(lines[-1][len("__RESULT__"):])
