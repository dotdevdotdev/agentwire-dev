"""The refusal announcer, asserted on the DATA CHANNEL (Slice 1, branch-only).

**Why this file exists separately, and why it runs node.**

The acceptance criterion for "every refusal must speak" cannot be met by a
Python test that inspects a return value. A tool result carrying a perfect
reason string is *green in exactly the scenario the requirement exists to
prevent*: the client declines to create a response, nothing is generated, and
the owner hears silence while the test passes. That fixture shape is the whole
defect class here, so the subject under test has to be the events that reach the
channel.

The announcer is therefore exercised as itself — the same ``ANNOUNCER_JS``
string the page embeds, not a reimplementation — under node, against a fake
``send``/``speak``/timer. What is asserted is what was emitted.

The two properties that matter, and both are about things the client CANNOT
observe:

1. **``responseActive`` is induced, not assumed.** The silent branch fires when
   a response is already in flight, so the test puts the announcer in that state
   and asserts a ``response.create`` still reaches the channel.
2. **The ``speechSynthesis`` fallback is armed by a timer, not triggered by a
   detected failure.** ``responseActive`` is a client-side mirror and is stale
   by construction; ``send()`` is fire-and-forget. So the announcement can be
   dropped SERVER-side with the client's own state reporting success, and no
   failure-detecting design can catch that. The fallback must fire on a
   default-on timer that only positive evidence disarms — that is the case
   tested here, with every client-visible signal saying success.
"""

import json
import shutil
import subprocess
import textwrap

import pytest

from agentwire.voice_layer import client

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to run the client's own JS"
)

# A deterministic fake clock + timer queue, so "the timer fired" is a decision
# the test makes rather than a race it waits on.
_HARNESS = """
const events = [];
const spoken = [];
const logs = [];
const anchored = [];
const notSpoken = [];
let timers = [];
let nextHandle = 1;
let channelOpen = true;
// The two states the browser voice can be in that the announcer must react to
// (#978 items 3 and 5): the owner talking when the timer comes due, and
// speechSynthesis reporting the utterance failed.
let ownerIsSpeaking = false;
let speakFails = false;
// THE REAL speak() IS ASYNC — onSpokenAloud runs from utterance.onend, an
// event that fires when the browser voice has finished TALKING. A harness
// whose speak() calls back synchronously has no window at all between "the
// fallback started speaking" and "it finished", which is the window review
// finding F4 lives in: this fixture's shape was the reason nothing saw it.
let speakDefers = false;
const pendingSpeech = [];
// The barge-in cut (#1041): how many times the announcer asked the harness to
// stop the browser voice's audio. The real dep is speechSynthesis.cancel(),
// which recalls audio already playing — the one thing speak() cannot undo.
let stopSpeakCalls = 0;

const announcer = createAnnouncer({
  send: (e) => { if (!channelOpen) return false; events.push(e); return true; },
  speak: (t, onDone, onFail) => {
    spoken.push(t);
    if (speakDefers) { pendingSpeech.push({ onDone, onFail }); return; }
    if (speakFails) { if (onFail) onFail(); return; }
    if (onDone) onDone();
  },
  stopSpeak: () => { stopSpeakCalls++; },
  onSpoken: (meta, how) => anchored.push({ meta: meta, how: how }),
  onNotSpoken: (meta) => notSpoken.push(meta),
  ownerSpeaking: () => ownerIsSpeaking,
  setTimer: (fn, ms) => { const h = nextHandle++; timers.push({ h, fn, ms }); return h; },
  clearTimer: (h) => { timers = timers.filter((t) => t.h !== h); },
  onLog: (kind, detail) => logs.push(kind + ": " + detail),
  fallbackMs: 6000,
});

function fireTimers() {
  const due = timers.slice();
  timers = [];
  due.forEach((t) => t.fn());
}
// Fire exactly ONE armed timer, by its position in the armed list.
//
// fireTimers() fires everything, which cannot express "this timer came due and
// that one did not" — and #997's deferral backstop is precisely a timer whose
// job is to cover the case where the speaking watchdog does NOT fire. Both are
// armed at the same budget, so they are indistinguishable by `ms`; position is
// the only handle a fake clock has on them.
function fireTimerAt(i) {
  const t = timers[i];
  if (!t) throw new Error("no timer armed at index " + i);
  timers = timers.filter((x) => x !== t);
  t.fn();
}
// The browser voice reaches the end of its utterance.
function finishSpeech() {
  const due = pendingSpeech.splice(0);
  due.forEach((s) => {
    if (speakFails) { if (s.onFail) s.onFail(); return; }
    if (s.onDone) s.onDone();
  });
}
function report() {
  return JSON.stringify({
    events, spoken, logs, anchored, notSpoken, stopSpeakCalls,
    armedTimers: timers.length,
    armedMs: timers.map((t) => t.ms),
    pending: announcer.pending(),
    armed: announcer.armed(),
    anchorPending: announcer.anchorPending(),
  });
}
"""


def run_announcer(script: str) -> dict:
    """Run *script* against the real ANNOUNCER_JS and return the harness report."""
    program = "\n".join(
        [client.announcer_source(), _HARNESS, textwrap.dedent(script), "console.log(report());"]
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def creates(report: dict) -> list:
    return [e for e in report["events"] if e["type"] == "response.create"]


class TestTheRefusalReachesTheChannel:
    def test_a_refusal_emits_a_scripted_response_create(self):
        report = run_announcer("""
            announcer.announce("I didn't hear the confirmation phrase.");
        """)
        created = creates(report)
        assert len(created) == 1
        instructions = created[0]["response"]["instructions"]
        assert "Say exactly this" in instructions
        assert "I didn't hear the confirmation phrase." in instructions


class TestTheFallbackIsArmedNotTriggered:

    def test_it_speaks_when_the_create_is_dropped_server_side(self):
        """The case the client cannot observe, and the reason for the timer.

        Every client-visible signal here says success: ``send()`` returned true,
        no error was surfaced, ``responseActive`` was false. The server rejected
        the overlapping create and nothing was ever spoken. A design that routes
        the fallback through detecting failure leaks exactly this case.
        """
        report = run_announcer("""
            announcer.announce("I didn't hear the confirmation phrase.");
            // Server dropped it. Nothing tells the client. Time passes.
            fireTimers();
        """)
        assert report["spoken"] == ["I didn't hear the confirmation phrase."]


# The buddy's clock (#962): a fake bridge with real cursor semantics — fetch
# peeks from the cursor, ack advances it past everything unread — plus a fake
# timer queue and a shared `seen` map so a reconnect (a second notifier over
# the same page state) is a scenario the test can build.
_NOTIFIER_HARNESS = """
const announcedCalls = [];
const logs = [];
const seen = {};
const ackCalls = [];
let spool = [];
let cursor = 0;
let speakable = true;
let interruptable = false;
let ledger = null;
let timers = [];
let nextHandle = 1;

function fetchInbox() {
  return Promise.resolve({ success: true, messages: spool.slice(cursor) });
}
// Mirrors delivery.advance_cursor exactly, including both refusals: an id the
// spool no longer holds moves nothing, and the cursor never rewinds. A harness
// that acked more freely than the real bridge would prove the wrong thing.
function ackInbox(through) {
  ackCalls.push(through === undefined ? "<undefined>" : String(through));
  if (!through) return Promise.resolve({ success: true, acked: false });
  const idx = spool.findIndex((m) => m.id === through);
  if (idx < 0) return Promise.resolve({ success: true, acked: false });
  if (idx + 1 > cursor) cursor = idx + 1;
  return Promise.resolve({ success: true, acked: true, acked_through: through });
}
function makeNotifier(overrides) {
  return createInboxNotifier(Object.assign({
    fetchInbox, ackInbox,
    announce: (text, meta) => announcedCalls.push({ text, meta }),
    canSpeak: () => speakable,
    canInterrupt: () => interruptable,
    reRaise: ledger,
    setTimer: (fn, ms) => { const h = nextHandle++; timers.push({ h, fn, ms }); return h; },
    clearTimer: (h) => { timers = timers.filter((t) => t.h !== h); },
    onLog: (kind, detail) => logs.push(kind + ": " + detail),
    pollMs: 5000,
    seen,
  }, overrides || {}));
}
let notifier = makeNotifier();
function report() {
  return JSON.stringify({
    announced: announcedCalls, logs, cursor, seen, ackCalls,
    armedTimers: timers.length,
  });
}
"""


def run_notifier(script: str) -> dict:
    program = "\n".join(
        [
            client.notifier_source(),
            client.reraise_source(),
            _NOTIFIER_HARNESS,
            textwrap.dedent(script),
            "console.log(report());",
        ]
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestTheBuddyClock:
    """#962. The notifier is the buddy's one clock: it polls the spool and
    volunteers replies — through the injected announce(), never its own
    speaking path."""


    def test_ack_happens_only_after_it_was_spoken(self):
        """The cohort-teardown lesson: collect the report, THEN kill the
        child. Acking on read marks delivered a report the owner never
        heard."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "minecraft", kind: "done", text: "done" }];
            await notifier.pollOnce();
            logs.push("cursor after announce: " + cursor);
            await notifier.noticeSpoken(announcedCalls[0].meta);
        """)
        assert "cursor after announce: 0" in report["logs"]
        assert report["cursor"] == 1
        assert report["seen"] == {"m1": True}
        # …and the ack named the message, rather than sweeping to the tail.
        assert report["ackCalls"] == ["m1"]


    def test_a_spoken_reply_is_never_replayed_across_a_reconnect(self):
        report = run_notifier("""
            spool = [{ id: "m1", from: "minecraft", kind: "done", text: "done" }];
            await notifier.pollOnce();
            await notifier.noticeSpoken(announcedCalls[0].meta);
            cursor = 0;                  // even if the spool is re-read whole
            notifier = makeNotifier();   // the reconnect
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1


_CONFIRM_GATE_HARNESS = """
let clock = 0;
const gate = createConfirmGate({ now: () => clock, ttlMs: 120000 });
function report() { return JSON.stringify({ outstanding: gate.outstanding() }); }
"""


def run_confirm_gate(script: str) -> dict:
    program = "\n".join(
        [
            client.confirm_gate_source(),
            _CONFIRM_GATE_HARNESS,
            textwrap.dedent(script),
            "console.log(report());",
        ]
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr.strip()}")
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestTheConfirmGate:
    """D2 (#962 never-barge-in): a spoken proposal awaiting the owner's
    confirm word closes the volunteering gate — and BOTH halves are priced:
    the block is real, and it expires with the proposal's TTL so an
    unanswered proposal cannot silence the buddy forever."""


    def test_an_anchored_proposal_closes_the_gate(self):
        assert run_confirm_gate("gate.anchored();")["outstanding"] is True


class TestThePageEmbedsTheRealThing:


    def test_the_whole_client_script_parses(self, tmp_path):
        """There is no JS lint in CI, so this is the syntax check — and it
        covers the ENTIRE page script, not just the announcer, because the
        announcer is spliced in and a splice can break its host."""
        page = client.page("buddy", "tok")
        body = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        path = tmp_path / "client.mjs"
        path.write_text(body, encoding="utf-8")
        result = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr


class TestTheInterruptTier:
    """#967 reconciled with #962. The gate is two-tier: the full gate clears
    everything; the interrupt gate clears ONLY escalation-kind messages. The
    two legs that stay unconditional for BOTH tiers: never while the owner is
    speaking, never inside a confirm handshake. The tier is a mechanism check
    on the message KIND — the fleet's own already-made judgment — never on
    how urgent the model feels."""

    def test_an_escalation_speaks_when_only_the_interrupt_gate_is_open(self):
        report = run_notifier("""
            spool = [{ id: "m1", from: "watchdog", kind: "escalation",
                       text: "a done report dead-lettered" }];
            speakable = false;       // the buddy is mid-chatter
            interruptable = true;    // but the owner is not speaking
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1
        assert "dead-lettered" in report["announced"][0]["text"]

    def test_an_ordinary_message_does_not_and_is_not_lost(self):
        """Both halves: the non-escalation waits, and the SAME message is
        volunteered once the full gate opens — a wrongly-silent tier is a
        silent loop, not a safe failure."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "minecraft", kind: "done", text: "done" }];
            speakable = false;
            interruptable = true;
            await notifier.pollOnce();
            logs.push("held: " + announcedCalls.length);
            speakable = true;
            await notifier.pollOnce();
        """)
        assert "held: 0" in report["logs"]
        assert len(report["announced"]) == 1


# =============================================================================
# The outcome router: the write outcome's two signals, behaviorally
# =============================================================================


# =============================================================================
# #978 wave 2 — the announcer/timer races and the silent-failure retry holes
# =============================================================================


# =============================================================================
# #993 review — F2 to F6
# =============================================================================


class TestBargeInCutsTheFallbackVoice:
    """#1041. The owner speaking over the BROWSER voice must stop its audio.

    Native barge-in covers model audio only — the server truncates the response
    and the WebRTC stream stops — while speechSynthesis is outside that path
    and used to play straight through. The inconsistency the owner reported
    correlated exactly with WHAT was playing: model replies cut off, announced
    notices talked through them. bargeIn() is the missing half.
    """

    def test_the_cut_is_synchronous_and_settles_not_spoken(self):
        """The bound on "owner speaks -> audio stops" is one event tick: no
        timer stands between bargeIn() and the stop, so the residual latency
        is VAD detection alone, never the utterance's remaining length."""
        report = run_announcer("""
            speakDefers = true;
            announcer.announce("worker-a got back to you: done.", { inboxIds: ["m1"] });
            fireTimers();                       // fallback fires; audio starts
            const cut = announcer.bargeIn();    // owner starts speaking
            logs.push("cut=" + cut);
        """)
        assert report["stopSpeakCalls"] == 1
        assert any("cut=1" in line for line in report["logs"])
        # Settled NOT SPOKEN — the ids are released so the notice is said
        # again at the next quiet tick: announced late, never lost.
        assert report["notSpoken"] == [{"inboxIds": ["m1"]}]
        assert report["anchored"] == []
        assert report["pending"] == 0
