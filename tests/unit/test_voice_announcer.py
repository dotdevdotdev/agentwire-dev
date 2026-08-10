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

from agentwire.voice_layer import client, confirm, transcript, write_tools

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

const announcer = createAnnouncer({
  send: (e) => { if (!channelOpen) return false; events.push(e); return true; },
  speak: (t, onDone, onFail) => {
    spoken.push(t);
    if (speakDefers) { pendingSpeech.push({ onDone, onFail }); return; }
    if (speakFails) { if (onFail) onFail(); return; }
    if (onDone) onDone();
  },
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
    events, spoken, logs, anchored, notSpoken,
    armedTimers: timers.length,
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


def cancels(report: dict) -> list:
    return [e for e in report["events"] if e["type"] == "response.cancel"]


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

    def test_it_still_emits_one_while_a_response_is_already_active(self):
        """The confirmed silent branch, INDUCED rather than assumed.

        ``maybeCreateResponse`` declines while a response is in flight, and a
        timing refusal fires exactly when VAD is producing its own responses —
        so this is the likely path, not the unlucky one. Asserting the returned
        reason string here would pass while the owner heard nothing.
        """
        report = run_announcer("""
            announcer.onResponseCreated();          // a VAD response is in flight
            announcer.announce("Give me a second — I'm still catching up.");
        """)
        assert len(cancels(report)) == 1, "must cancel, not decline"
        created = creates(report)
        assert len(created) == 1
        assert "still catching up" in created[0]["response"]["instructions"]

    def test_the_announcement_is_not_swallowed_by_a_stale_active_flag(self):
        report = run_announcer("""
            announcer.onResponseCreated();
            announcer.onResponseCreated();          // mirror drifts further
            announcer.announce("That was a different code word.");
        """)
        assert len(creates(report)) == 1


class TestTheAnchorFollowsEvidenceOfSpeech:
    """BLOCKING 2. The proposal anchor may key on nothing weaker than
    "this text was actually spoken"."""

    def test_a_confirmed_model_turn_anchors_the_proposal(self):
        report = run_announcer("""
            announcer.announce("I will ask the orchestrator to restart the portal. Say confirm tango.",
                               { anchor: "a1b2c3" });
            announcer.onResponseDone("I will ask the orchestrator to restart the portal. Say confirm tango.");
        """)
        assert report["anchored"] == [{"meta": {"anchor": "a1b2c3"}, "how": "model"}]

    def test_a_cancelled_response_never_anchors(self):
        """Our OWN cancel produces one of these, and it can carry partial audio
        that said something else entirely."""
        report = run_announcer("""
            announcer.onResponseCreated();
            announcer.announce("Say confirm tango to approve.", { anchor: "a1b2c3" });
            announcer.onResponseCancelled();   // the turn we cancelled
        """)
        assert report["anchored"] == []

    def test_a_response_saying_something_else_never_anchors(self):
        report = run_announcer("""
            announcer.announce("Say confirm tango to approve.", { anchor: "a1b2c3" });
            announcer.onResponseDone("Sure, what would you like next?");
        """)
        assert report["anchored"] == []

    def test_the_fallback_voice_does_anchor_because_the_owner_heard_it(self):
        """The corner where the two safety mechanisms defeated each other.

        A speechSynthesis utterance produces no response.done, so a
        fallback-spoken proposal used to be anchored by nothing — the owner
        heard it, said the nonce, and got not_announced until the TTL.
        not_announced is never SILENT; it can be PERSISTENTLY WRONG, and what
        made it wrong was the fallback firing, which is the mechanism added to
        GUARANTEE speech.
        """
        report = run_announcer("""
            announcer.announce("Say confirm tango to approve.", { anchor: "a1b2c3" });
            fireTimers();   // model never said it; browser voice does
        """)
        assert report["spoken"] == ["Say confirm tango to approve."]
        assert report["anchored"] == [{"meta": {"anchor": "a1b2c3"}, "how": "fallback"}]

    def test_an_announcement_with_no_anchor_carries_no_anchor(self):
        """An ordinary refusal reports that it was spoken — the announcer does
        not know what an anchor is — and carries no meta, which is what the
        client's ``onSpoken`` guard keys on."""
        report = run_announcer("""
            announcer.announce("I didn't hear the confirmation phrase.");
            fireTimers();
        """)
        assert [a["meta"] for a in report["anchored"]] == [None]
        # And the client refuses to anchor on a null meta.
        page = client.page("buddy", "tok")
        assert "if (!meta || !meta.anchor) return;" in page


class TestTheFallbackIsArmedNotTriggered:
    def test_the_timer_is_armed_the_moment_a_refusal_is_announced(self):
        report = run_announcer("""
            announcer.announce("That request timed out.");
        """)
        assert report["armed"] is True
        assert report["armedTimers"] == 1

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

    def test_the_not_announced_recursion_cannot_go_silent(self):
        """A ``not_announced`` that fails to announce is the recursion §3.4
        exists to prevent. That is the one place the timer-armed fallback has to
        be unconditional.

        This outcome fires precisely WHILE the buddy's proposal turn is still in
        flight — i.e. with ``responseActive`` true, the branch that used to
        swallow announcements. If "I haven't finished saying it yet" is itself
        swallowed by the response it describes, the owner hears nothing, waits,
        and both parties wait for each other.

        So: induce the exact state, drop the create server-side, and require
        speech anyway.
        """
        report = run_announcer("""
            announcer.onResponseCreated();   // the proposal turn is speaking
            announcer.announce("Hang on — I haven't finished telling you what I'd send yet.");
            // Server rejects the overlapping create. Client sees success.
            fireTimers();
        """)
        assert len(cancels(report)) == 1
        assert len(creates(report)) == 1
        assert report["spoken"] == [
            "Hang on — I haven't finished telling you what I'd send yet."
        ]

    def test_no_reason_is_special_cased_out_of_the_fallback(self):
        """Every outcome the spine can produce arms the timer identically."""
        from agentwire.voice_layer import confirm as confirm_mod

        for reason, line in confirm_mod.SPOKEN.items():
            report = run_announcer(f"""
                announcer.onResponseCreated();
                announcer.announce({json.dumps(line)});
                fireTimers();
            """)
            assert report["spoken"] == [line], reason

    def test_a_response_that_says_something_else_does_not_disarm_it(self):
        report = run_announcer("""
            announcer.announce("That was a different code word, ask me again.");
            announcer.onResponseDone("Sure, what would you like me to do next?");
            fireTimers();
        """)
        assert report["spoken"] == ["That was a different code word, ask me again."]

    def test_only_positive_evidence_disarms_it(self):
        report = run_announcer("""
            const reason = "I didn't hear the confirmation phrase, so I haven't sent anything.";
            announcer.announce(reason);
            announcer.onResponseDone(reason);
            fireTimers();
        """)
        assert report["spoken"] == []
        assert report["armed"] is False
        assert report["pending"] == 0

    def test_a_close_paraphrase_counts_as_spoken(self):
        """"Say exactly" is prompt compliance, and prompt compliance is not a
        mechanism — so verification is overlap, not equality. A paraphrase that
        carried the reason DID reach the owner's ear."""
        report = run_announcer("""
            announcer.announce("I didn't hear the confirmation phrase, so I haven't sent anything.");
            announcer.onResponseDone("I didn't hear the confirmation phrase so I haven't sent anything yet.");
            fireTimers();
        """)
        assert report["spoken"] == []

    def test_it_speaks_when_the_data_channel_is_closed(self):
        report = run_announcer("""
            channelOpen = false;
            announcer.announce("I lost the connection.");
            fireTimers();
        """)
        assert report["spoken"] == ["I lost the connection."]

    def test_a_cancel_that_errors_does_not_stop_the_announcement(self):
        """``response.cancel`` against an already-finished response errors.
        Ignore it — the create still goes, and the timer covers everything."""
        report = run_announcer("""
            announcer.onResponseCreated();
            announcer.onResponseDone("some unrelated answer");   // it already finished
            announcer.announce("That expired before you confirmed it.");
            fireTimers();
        """)
        assert len(creates(report)) == 1
        assert report["spoken"] == ["That expired before you confirmed it."]

    def test_the_fallback_utters_the_fallback_text_not_the_say_text(self):
        """#950 defect 4. speechSynthesis is outside WebRTC echo cancellation,
        so whatever this channel utters can re-enter the mic and land in the
        USER transcript. A proposal's `say` carries the nonce; its fallback
        variant must not — and the announcer must route the right text to the
        right channel."""
        report = run_announcer("""
            announcer.announce("I'm ready to send it. To approve, say confirm tango.",
                               { anchor: "a1b2c3" },
                               "I'm ready to send it. Ask me for the code word.");
            fireTimers();
        """)
        assert report["spoken"] == ["I'm ready to send it. Ask me for the code word."]
        assert report["anchored"] == [{"meta": {"anchor": "a1b2c3"}, "how": "fallback"}]

    def test_the_disarm_still_verifies_against_the_say_text(self):
        """The transcript check verifies what the MODEL was scripted to say,
        regardless of the fallback variant riding along."""
        report = run_announcer("""
            const say = "I'm ready to send it. To approve, say confirm tango.";
            announcer.announce(say, { anchor: "a1b2c3" }, "different fallback words");
            announcer.onResponseDone(say);
            fireTimers();
        """)
        assert report["spoken"] == []
        assert report["anchored"] == [{"meta": {"anchor": "a1b2c3"}, "how": "model"}]

    def test_a_response_created_after_the_announce_defers_the_timer_once(self):
        """#950 defect 1's residual: at the timeout, a response that began
        AFTER our announce may be the model still mid-audio on this very text.
        One bounded deferral — it can delay speech, never suppress it."""
        report = run_announcer("""
            announcer.announce("a long announcement still being spoken");
            announcer.onResponseCreated();   // plausibly our scripted turn
            fireTimers();                    // defers, does not speak
        """)
        assert report["spoken"] == []
        assert report["armedTimers"] == 1, "must re-arm, not give up"

        report = run_announcer("""
            announcer.announce("a long announcement still being spoken");
            announcer.onResponseCreated();
            fireTimers();                    // the one deferral
            fireTimers();                    // second firing MUST speak
        """)
        assert report["spoken"] == ["a long announcement still being spoken"]

    def test_a_response_in_flight_before_the_announce_never_defers(self):
        """The not_announced recursion's exact state. A pre-existing response
        is not evidence our text is being spoken, and deferring on it would
        delay the one announcement that must be prompt."""
        report = run_announcer("""
            announcer.onResponseCreated();   // in flight BEFORE the announce
            announcer.announce("Hang on — I haven't finished telling you yet.");
            fireTimers();
        """)
        assert report["spoken"] == ["Hang on — I haven't finished telling you yet."]

    def test_a_finished_or_cancelled_response_stops_deferring(self):
        """Once the in-flight response ended without carrying the text, there
        is no audio left to wait for — the next firing speaks."""
        report = run_announcer("""
            announcer.announce("the reason");
            announcer.onResponseCreated();
            announcer.onResponseDone("something else entirely");
            fireTimers();
        """)
        assert report["spoken"] == ["the reason"]

    def test_the_cancel_is_sent_only_when_a_response_is_active(self):
        """#950 defect 2's first edge: an unconditional cancel with nothing
        active errors server-side, and an error handler that speaks turns that
        into a loop. Idle → no cancel; active → cancel."""
        idle = run_announcer("""
            announcer.announce("a refusal");
        """)
        assert len(cancels(idle)) == 0
        active = run_announcer("""
            announcer.onResponseCreated();
            announcer.announce("a refusal");
        """)
        assert len(cancels(active)) == 1

    def test_queued_refusals_all_get_spoken(self):
        report = run_announcer("""
            announcer.announce("first reason");
            announcer.announce("second reason");
            fireTimers();
            fireTimers();
        """)
        assert report["spoken"] == ["first reason", "second reason"]


class TestOneAnnouncementLogsOnce:
    """#957. Every scripted announcement logged twice — announce() logged the
    scripted text, then response.done logged the model's ASR of having said it
    — rendering identically to the #950 double-speak defect on every utterance.

    Fix is kind-splitting, not suppression: the response.done transcript is
    classified by the announcer's OWN disarm verdict (onResponseDone returns
    true only when this transcript is the model speaking the current scripted
    announcement) and logged under a distinct "heard" kind. Everything else —
    including the model re-speaking an announcement the FALLBACK already
    uttered, the genuine double-speak — stays a plain buddy line, so the #950
    signature remains visible.
    """

    def test_a_matching_done_classifies_as_our_script(self):
        """The ASR transcript loses punctuation (the em-dash evidence in the
        issue) — classification is the same overlap test as the disarm."""
        report = run_announcer("""
            announcer.announce("Queued it — it'll land when the box is free.");
            logs.push("ours: " + announcer.onResponseDone("Queued it  it'll land when the box is free"));
        """)
        assert "ours: true" in report["logs"]

    def test_an_unrelated_done_does_not(self):
        report = run_announcer("""
            announcer.announce("Say confirm tango to approve.");
            logs.push("ours: " + announcer.onResponseDone("Sure, what next?"));
        """)
        assert "ours: false" in report["logs"]

    def test_a_done_with_nothing_current_does_not(self):
        report = run_announcer("""
            logs.push("ours: " + announcer.onResponseDone("I'm ready to send it."));
        """)
        assert "ours: false" in report["logs"]

    def test_a_genuine_double_speak_still_reads_as_two(self):
        """THE requirement (#957 acceptance): model audio AND browser fallback
        both uttering the announcement must remain distinguishable from normal
        operation. The fallback fired first (clearing `current`), then the
        model's transcript of the same text arrived — that transcript must NOT
        classify as the scripted announcement, so the page logs it as a second
        plain buddy line and the #950 signature stays visible. Collapsing it
        would trade a false positive for a false negative on a closed
        severity-1 defect."""
        report = run_announcer("""
            announcer.announce("Say confirm tango to approve.");
            fireTimers();   // fallback speaks — the first voice
            logs.push("ours: " + announcer.onResponseDone("Say confirm tango to approve."));
        """)
        assert report["spoken"] == ["Say confirm tango to approve."]
        assert "ours: false" in report["logs"]

    def test_the_page_logs_the_transcript_under_the_verdict_driven_kind(self):
        """The wiring: the response.done log site keys the kind off the
        announcer's verdict, and the scripted announce() log keeps the plain
        buddy kind — two visibly different kinds for one utterance."""
        page = client.page("buddy", "tok")
        assert "announcer.onResponseDone(said) === true" in page
        assert 'log(saidOurScript ? "heard" : "buddy", said, saidOurScript ? "heard" : "buddy");' in page
        # The scripted-text log site is unchanged — kind "buddy".
        assert 'log("buddy", text, "buddy");' in page
        # And the heard kind is actually styled distinctly, not just named.
        assert ".heard" in page


class TestTheGreeting:
    """#963. On connect the buddy speaks first — and the greeting doubles as
    the health check for the fail-closed write path (#950): heard greeting =
    model audio works = approvals can work. So the greeting must be spoken by
    the MODEL; the browser fallback proving "a voice works" would prove exactly
    the wrong voice. Resolution of that tension with the announcer's
    default-fallback design: the fallback stays armed (silence is still
    unacceptable) but its text is a WARNING that model audio is dead, riding
    the existing fallbackText channel — the failure is surfaced, not papered
    over."""

    def test_a_model_spoken_greeting_disarms_and_reports_model(self):
        report = run_announcer("""
            announcer.announce("Hey, I'm listening. What's on your mind?",
                               { greeting: true },
                               "warning text");
            announcer.onResponseDone("Hey I'm listening, what's on your mind?");
            fireTimers();
        """)
        assert report["spoken"] == []
        assert report["anchored"] == [{"meta": {"greeting": True}, "how": "model"}]

    def test_dead_model_audio_surfaces_the_warning_not_the_greeting(self):
        """THE non-cosmetic case: model audio dead. The browser voice must NOT
        utter the greeting (that confirms the wrong voice while the write path
        is silently unusable) — it utters the warning that names the failure."""
        report = run_announcer("""
            announcer.announce("Hey, I'm listening. What's on your mind?",
                               { greeting: true },
                               "Heads up, my main voice is not working.");
            fireTimers();
        """)
        assert report["spoken"] == ["Heads up, my main voice is not working."]
        assert report["anchored"] == [
            {"meta": {"greeting": True}, "how": "fallback"}
        ]

    def test_cancel_withdraws_a_queued_greeting_entirely(self):
        """The owner speaking first cancels the greeting, not queues behind it.
        A withdrawn item must never be spoken by either voice and never reach
        onSpoken."""
        report = run_announcer("""
            announcer.onResponseCreated();  // something else is speaking
            announcer.announce("first item", null);
            announcer.announce("the greeting", { greeting: true }, "warning");
            announcer.cancel(function (m) { return m && m.greeting; });
            announcer.onResponseDone("first item");
            fireTimers();
        """)
        assert report["spoken"] == []
        assert report["pending"] == 0
        # only the first item's spoken evidence — the greeting never reports.
        assert report["anchored"] == [{"meta": None, "how": "model"}]

    def test_cancel_of_the_current_greeting_disarms_its_fallback(self):
        """Cancelling mid-flight: the timer must be disarmed (or the fallback
        speaks a greeting the owner already talked over), and a later
        response.done carrying the greeting text must not count as spoken."""
        report = run_announcer("""
            announcer.announce("the greeting", { greeting: true }, "warning");
            announcer.onResponseCreated();     // model begins speaking it
            announcer.cancel(function (m) { return m && m.greeting; });
            announcer.onResponseDone("the greeting");   // partial audio's ASR
            fireTimers();
        """)
        assert report["spoken"] == []
        assert report["anchored"] == []
        assert report["armedTimers"] == 0

    def test_cancel_leaves_unrelated_announcements_alone(self):
        report = run_announcer("""
            announcer.announce("a refusal that must still speak");
            announcer.cancel(function (m) { return m && m.greeting; });
            fireTimers();
        """)
        assert report["spoken"] == ["a refusal that must still speak"]

    def test_the_page_greets_only_when_genuinely_ready(self):
        """Channel-open is not readiness: the greet site requires the server's
        session.created AND the audio track being attached, and fires once."""
        page = client.page("buddy", "tok")
        greet_body = page.split("function maybeGreet() {", 1)[1].split("\n}", 1)[0]
        assert "if (greeted || !sessionReady || !audioAttached) return;" in greet_body
        assert "greeted = true;" in greet_body
        assert 'case "session.created":' in page
        # both readiness legs re-check, whichever lands last.
        assert page.count("maybeGreet()") >= 2

    def test_the_page_never_regreets_on_reconnect(self):
        """`greeted` is page-lifetime: stop() resets session state but must
        NOT reset it — a dropped and re-established connection stays quiet."""
        page = client.page("buddy", "tok")
        stop_body = page.split("function stop() {", 1)[1].split("\n}", 1)[0]
        assert "greeted = false" not in stop_body
        assert "greeted = true" not in stop_body

    def test_the_owner_speaking_cancels_the_greeting_on_the_page(self):
        """speech_started both suppresses a not-yet-fired greeting and
        withdraws a queued/current one."""
        page = client.page("buddy", "tok")
        started = page.split('case "input_audio_buffer.speech_started":', 1)[1]
        started = started.split("break;", 1)[0]
        assert "greeted = true;" in started
        assert "announcer.cancel(" in started

    def test_the_greeting_rides_announce_not_a_new_speaking_path(self):
        """#950's constraint, pinned: response.create appears exactly twice in
        the page — the announcer's pump and maybeCreateResponse. The greeting
        (and everything else new) adds no third."""
        page = client.page("buddy", "tok")
        assert page.count('type: "response.create"') == 2
        assert "announce(GREETING" in page

    def test_the_greeting_literals_are_speakable(self):
        page = client.page("buddy", "tok")
        import re

        greeting = re.search(r'const GREETING = "([^"]+)";', page)
        warning = re.search(r'const MODEL_AUDIO_DEAD = "([^"]+)";', page)
        assert greeting and warning
        for line in (greeting.group(1), warning.group(1)):
            assert "`" not in line and "_" not in line, line
        # the warning names the consequence — the owner cannot see a screen.
        assert "approve" in warning.group(1).lower()


# The buddy's clock (#962): a fake bridge with real cursor semantics — fetch
# peeks from the cursor, ack advances it past everything unread — plus a fake
# timer queue and a shared `seen` map so a reconnect (a second notifier over
# the same page state) is a scenario the test can build.
_NOTIFIER_HARNESS = """
const announcedCalls = [];
const logs = [];
const seen = {};
const strays = [];
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
function ackInbox() {
  const msgs = spool.slice(cursor);
  cursor = spool.length;
  return Promise.resolve({ success: true, messages: msgs });
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
    strays,
  }, overrides || {}));
}
let notifier = makeNotifier();
function report() {
  return JSON.stringify({
    announced: announcedCalls, logs, cursor, seen, armedTimers: timers.length,
    strays: strays.length,
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

    def test_three_replies_are_one_utterance(self):
        report = run_notifier("""
            spool = [
              { id: "m1", from: "minecraft", kind: "done", text: "finished; 4 options" },
              { id: "m2", from: "billing", kind: "note", text: "deploy went out" },
              { id: "m3", from: "docs", kind: "done", text: "draft ready" },
            ];
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1
        text = report["announced"][0]["text"]
        for who in ("minecraft", "billing", "docs"):
            assert who in text
        assert report["announced"][0]["meta"]["inboxIds"] == ["m1", "m2", "m3"]

    def test_it_never_barges_in_and_loses_nothing_by_waiting(self):
        """Both halves of the gate: blocked while the owner is busy, and the
        very same reply is volunteered on the next tick — a wrongly-silent
        clock is a silent loop, not a safe failure."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "minecraft", kind: "done", text: "done" }];
            speakable = false;
            await notifier.pollOnce();
            logs.push("blocked: " + announcedCalls.length);
            speakable = true;
            await notifier.pollOnce();
        """)
        assert "blocked: 0" in report["logs"]
        assert len(report["announced"]) == 1

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

    def test_a_pending_notice_is_not_reannounced_by_the_next_tick(self):
        report = run_notifier("""
            spool = [{ id: "m1", from: "minecraft", kind: "done", text: "done" }];
            await notifier.pollOnce();
            await notifier.pollOnce();   // still unacked — must not repeat
        """)
        assert len(report["announced"]) == 1

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

    def test_an_announced_but_unheard_reply_is_retried_after_reconnect(self):
        """The other half: announced is not heard. A session that died before
        speaking must not count the notice delivered — the new notifier says
        it again."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "minecraft", kind: "done", text: "done" }];
            await notifier.pollOnce();   // announced… and the session dies
            notifier = makeNotifier();   // never spoken, never acked
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 2

    def test_a_reply_landing_between_speak_and_ack_is_not_silently_acked(self):
        """ack advances the cursor past EVERYTHING unread, including a message
        that arrived after the peek. That message was never spoken — it must
        surface on a later tick, not vanish behind the cursor."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "minecraft", kind: "done", text: "done" }];
            await notifier.pollOnce();
            spool.push({ id: "m2", from: "billing", kind: "note", text: "late arrival" });
            await notifier.noticeSpoken(announcedCalls[0].meta);
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 2
        assert "billing" in report["announced"][1]["text"]
        assert report["announced"][1]["meta"]["inboxIds"] == ["m2"]

    def test_a_stray_reply_survives_stop_before_the_next_tick(self):
        """The wave-2 D1 construction: a reply lands between the peek and the
        ack, the cursor advances past it, and the owner clicks Stop before the
        next gated tick. The stray's home must outlive the notifier — the
        spool will never return that message again (there is no ack-by-id),
        so a per-notifier array is its grave."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "minecraft", kind: "done", text: "done" }];
            await notifier.pollOnce();
            spool.push({ id: "m2", from: "billing", kind: "note", text: "late arrival" });
            await notifier.noticeSpoken(announcedCalls[0].meta);
            notifier.stop();             // owner clicks Stop with the stray pending
            notifier = makeNotifier();   // the reconnect
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 2
        assert "billing" in report["announced"][1]["text"]
        assert report["announced"][1]["meta"]["inboxIds"] == ["m2"]

    def test_an_empty_inbox_is_silence(self):
        """The recipient never replying produces silence — no follow-up, no
        apology, no chatter."""
        report = run_notifier("""
            await notifier.pollOnce();
            await notifier.pollOnce();
        """)
        assert report["announced"] == []

    def test_a_failed_poll_is_logged_not_spoken_and_not_fatal(self):
        report = run_notifier("""
            notifier = makeNotifier({
              fetchInbox: () => Promise.resolve({ success: false, error: "bridge down" }),
            });
            await notifier.pollOnce();
        """)
        assert report["announced"] == []
        assert any("bridge down" in line for line in report["logs"])

    def test_start_arms_the_loop_and_stop_disarms_it(self):
        report = run_notifier("""
            notifier.start();
            await new Promise((r) => setImmediate(r));
            logs.push("armed after start: " + timers.length);
            notifier.stop();
            logs.push("armed after stop: " + timers.length);
        """)
        assert "armed after start: 1" in report["logs"]
        assert "armed after stop: 0" in report["logs"]

    def test_the_page_wires_the_notifier_through_announce_only(self):
        """No second speaking path (#950): the notifier's announce dep IS the
        page's announce, the poll interval is a named constant, and the
        response.create count is unchanged."""
        page = client.page("buddy", "tok")
        assert client.notifier_source().strip() in page
        assert "const INBOX_POLL_MS" in page
        assert "createInboxNotifier({" in page
        assert "announce: announce," in page
        assert page.count('type: "response.create"') == 2

    def test_the_page_gates_on_owner_and_response_state(self):
        page = client.page("buddy", "tok")
        assert (
            "canSpeak: () => !ownerSpeaking && !responseActive"
            " && !!announcer && announcer.pending() === 0"
            " && !confirmGate.outstanding()," in page
        )
        started = page.split('case "input_audio_buffer.speech_started":', 1)[1]
        assert "ownerSpeaking = true;" in started.split("break;", 1)[0]
        committed = page.split('case "input_audio_buffer.committed":', 1)[1]
        assert "ownerSpeaking = false;" in committed.split("break;", 1)[0]

    def test_the_page_polls_the_inbox_tool_and_acks_via_on_spoken(self):
        page = client.page("buddy", "tok")
        assert (
            'fetchInbox: () => post("/tool", { name: "buddy_inbox",'
            " arguments: { ack: false } })," in page
        )
        assert (
            'ackInbox: () => post("/tool", { name: "buddy_inbox",'
            " arguments: { ack: true } })," in page
        )
        # onSpoken routes a spoken notice back to the notifier for the ack.
        onspoken_body = page.split("function onSpoken(meta, how)", 1)[1]
        assert "meta.inboxIds" in onspoken_body.split("function send", 1)[0]

    def test_heard_replies_survive_stop_but_the_notifier_does_not(self):
        page = client.page("buddy", "tok")
        stop_body = page.split("function stop() {", 1)[1].split("\n}", 1)[0]
        assert "inboxNotifier.stop();" in stop_body
        assert "heardReplies =" not in stop_body
        assert "heardReplies[" not in stop_body

    def test_strays_get_the_same_page_lifetime_home_as_heard_replies(self):
        """D1: the page owns the strays array and passes it in like `seen`;
        stop() must not touch it — a stray is cursor-past, so this array is
        the only route left to the owner's ear."""
        page = client.page("buddy", "tok")
        assert "const strayReplies = [];" in page
        wiring = page.split("createInboxNotifier({", 1)[1].split("});", 1)[0]
        assert "strays: strayReplies," in wiring
        # Pin the operations, not the mentions: stop() must not reassign or
        # mutate the array (a comment naming it is fine).
        stop_body = page.split("function stop() {", 1)[1].split("\n}", 1)[0]
        assert "strayReplies =" not in stop_body
        assert "strayReplies." not in stop_body


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

    def test_no_proposal_means_the_gate_is_open(self):
        assert run_confirm_gate("")["outstanding"] is False

    def test_an_anchored_proposal_closes_the_gate(self):
        assert run_confirm_gate("gate.anchored();")["outstanding"] is True

    def test_a_resolved_proposal_reopens_it(self):
        report = run_confirm_gate("""
            gate.anchored();
            gate.resolved();
        """)
        assert report["outstanding"] is False

    def test_the_ttl_bounds_the_false_reject(self):
        """The wrongful-no half: an owner who never answers must not mute
        volunteering forever. The gate expires on the proposal's own TTL."""
        report = run_confirm_gate("""
            gate.anchored();
            clock = 119999;
            if (!gate.outstanding()) throw new Error("expired early");
            clock = 120000;
        """)
        assert report["outstanding"] is False

    def test_a_new_proposal_restarts_the_clock(self):
        report = run_confirm_gate("""
            gate.anchored();
            clock = 100000;
            gate.anchored();
            clock = 200000;   // 100s after the second anchor, 200s after the first
        """)
        assert report["outstanding"] is True

    def test_the_page_wires_the_gate_to_the_anchor_and_the_outcome(self):
        """The gate's edges on the page: closed when the proposal is SPOKEN
        (the onSpoken anchor branch), reopened by the outcome router — which
        keys on the payload's confirm_terminal, never on tool names, so a
        second declared write (#966) reopens it too. The behavioral half runs
        under node in TestTheOutcomeRouter."""
        page = client.page("buddy", "tok")
        assert client.confirm_gate_source().strip() in page
        assert client.outcome_router_source().strip() in page
        # ttl mirrors confirm.PROPOSAL_TTL_S — the false-reject bound.
        assert "ttlMs: 120000" in page
        anchor_branch = page.split("if (!meta || !meta.anchor) return;", 1)[1]
        assert "confirmGate.anchored();" in anchor_branch.split("}", 1)[0]
        wiring = page.split("createOutcomeRouter({", 1)[1].split("});", 1)[0]
        assert "gate: confirmGate," in wiring
        assert "ledger: reRaiseLedger," in wiring
        dispatch = page.split("async function handleFunctionCall(item)", 1)[1]
        dispatch = dispatch.split("function spokenText", 1)[0]
        assert "outcomeRouter.route(item.name, result);" in dispatch
        # The router is the ONLY dispatcher of the outcome — a second inline
        # gate/ledger call here would reintroduce the name-keyed path.
        assert "confirmGate.resolved" not in dispatch
        assert "reRaiseLedger.actedOn" not in dispatch

    def test_the_gate_survives_stop_because_the_proposal_does(self):
        """The spine lives in the bridge, not the page: a reconnect inside
        the TTL can still be answered, so stop() leaves the gate alone and
        the TTL is what reopens it."""
        page = client.page("buddy", "tok")
        # Operations, not mentions: stop() must not resolve or re-anchor.
        stop_body = page.split("function stop() {", 1)[1].split("\n}", 1)[0]
        assert "confirmGate.resolved" not in stop_body
        assert "confirmGate.anchored" not in stop_body


class TestThePageEmbedsTheRealThing:
    def test_the_page_contains_the_announcer_verbatim(self):
        """The tests above run ``announcer_source()``; the page must embed the
        same string, or this file is testing a copy."""
        page = client.page("buddy", "tok")
        assert client.announcer_source().strip() in page

    def test_the_page_has_no_unsubstituted_placeholders(self):
        page = client.page("buddy", "tok")
        for marker in ("__ANNOUNCER__", "__BUDDY__", "__TOKEN__"):
            assert marker not in page

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

    def test_the_client_forwards_speech_started_not_only_the_commit(self):
        """The ordering foundation. Asserted on the page source because the
        event wiring is what the bridge depends on."""
        page = client.page("buddy", "tok")
        assert "input_audio_buffer.speech_started" in page
        assert "speech_started_seq" in page
        assert "input_audio_buffer.committed" in page
        assert "conversation.item.input_audio_transcription.completed" in page

    def test_the_client_awaits_forwards_before_dispatching_tools(self):
        """Two independent fetches, otherwise: the gate can be asked about an
        utterance the ring has not received yet."""
        page = client.page("buddy", "tok")
        assert "await forwardChain" in page

    def test_the_output_is_delivered_before_the_scripted_response(self):
        """Ordering, and it is not cosmetic.

        Announcing first creates a response against an UNRESOLVED function
        call, and races the ordinary ``maybeCreateResponse`` at the same time.
        The output must land (with the ordinary response suppressed) and only
        then does the announcer create its scripted one.
        """
        page = client.page("buddy", "tok")
        send_at = page.index("sendFunctionCallOutput(item.call_id, result, mustSpeak)")
        announce_at = page.index("if (mustSpeak) {")
        assert send_at < announce_at
        assert "if (!suppressResponse) maybeCreateResponse();" in page

    def test_the_anchor_is_never_driven_by_a_bare_response_done(self):
        """BLOCKING 2. The anchor must key on evidence the PROPOSAL was spoken.

        Binding it to "the next response.done carrying any text" let the
        announcer's own ``response.cancel`` steal it — anchoring a proposal to a
        turn that said something else, BEFORE the proposal was spoken. That is
        the barge-in hole, reintroduced one layer below where the clock fix
        closed it.
        """
        page = client.page("buddy", "tok")
        assert "pendingAnchor" not in page, "the stealable anchor is gone"
        # response.cancelled is its own case and never reaches onResponseDone.
        cancelled_at = page.index('case "response.cancelled":')
        done_at = page.index('case "response.done": {')
        assert cancelled_at < done_at
        assert "announcer.onResponseCancelled()" in page
        # The only anchor forward is inside onSpoken.
        assert page.count('forward("/anchor"') == 1
        onspoken_at = page.index("function onSpoken(meta, how)")
        anchor_at = page.index('forward("/anchor"')
        assert onspoken_at < anchor_at

    def test_every_client_side_spoken_literal_is_asserted(self):
        """The category no test was exercising.

        Two digit-era strings shipped on the spoken path because they lived in
        prompt strings rather than in logic — and grepping for "digit" would
        only ever have found the one that used the word. The client's own
        ``announce()`` literals are the same category: a wrong instruction there
        is indistinguishable from a right one at review time.

        So the set is pinned. Adding one is fine; adding one without deciding
        what it says is what this catches.
        """
        import re

        page = client.page("buddy", "tok")
        spoken = set(re.findall(r'announce\("([^"]+)"', page))
        expected = {
            "I couldn't reach my own tools just then, so I did nothing.",
            "I lost the connection to the voice service, so I couldn't finish that.",
            "I'm getting garbled data from the voice service — I may miss things.",
            "I'm having trouble hearing you — the local bridge didn't answer.",
            "Something went wrong handling that, so I did nothing.",
            "The voice service reported an error, so I may have missed that.",
        }
        assert spoken == expected, spoken ^ expected

        for line in spoken:
            # Speakable: a whole sentence, no markup, no identifiers.
            assert line[0].isupper(), line
            assert line.rstrip().endswith("."), line
            assert "`" not in line and "_" not in line, line
            # And it must say what happened to the request, not just that
            # something broke — the owner cannot see a screen.
            assert any(
                cue in line.lower()
                for cue in ("did nothing", "couldn't finish", "may miss",
                            "didn't answer", "may have missed")
            ), line

    def test_no_spoken_literal_carries_stale_nonce_wording(self):
        """The digit-era lesson, applied to every spoken surface at once."""
        from agentwire.voice_layer import confirm as confirm_mod
        from agentwire.voice_layer import instructions

        surfaces = [
            client.page("buddy", "tok"),
            instructions.build_instructions(),
            " ".join(confirm_mod.SPOKEN.values()),
        ]
        for text in surfaces:
            lowered = text.lower()
            assert "two digits" not in lowered
            assert "confirm four seven" not in lowered

    def test_the_error_handler_cannot_feed_itself(self):
        """#950 defect 2, both edges, pinned on the page source: the error our
        own best-effort cancel generates is never announced, and only one
        error notice may be pending at a time. Plus defect 3: no cancel()
        before speak() — it killed the previous utterance mid-word."""
        page = client.page("buddy", "tok")
        assert 'if (code === "response_cancel_not_active") break;' in page
        assert "errorNoticePending" in page
        assert "window.speechSynthesis.cancel()" not in page

    def test_stop_resets_the_error_notice_gate(self):
        """The reset must live INSIDE stop(), pinned — a notice pending when a
        session died was never going to be spoken by it, and carrying the flag
        into the next session suppresses that session's FIRST error notice,
        silently. Unpinned, a refactor drops the reset and nothing notices:
        the exact unexercised-protection shape the quoted-frame guard had."""
        page = client.page("buddy", "tok")
        stop_body = page.split("function stop() {", 1)[1].split("\n}", 1)[0]
        assert "errorNoticePending = false;" in stop_body

    def test_the_proposal_say_field_is_speech_not_a_directive(self):
        """#950 root cause: one field carrying two kinds of value. `say` must
        now be literal first-person speech that the disarm check can match,
        and the model-facing directive must live in a key the announcer never
        reads."""
        from unittest.mock import patch

        from agentwire import inbox
        from agentwire.voice_layer import confirm as confirm_mod
        from agentwire.voice_layer import transcript, write_tools

        spine = confirm_mod.ConfirmSpine(transcript.TranscriptRing(), wait_s=0.0)
        with patch.object(inbox, "live_sessions", lambda: {"orchestrator"}):
            result = write_tools.propose_session_message(
                {"session": "orchestrator", "message": "restart it",
                 "_buddy": "buddy"},
                spine,
            )
        for directive in ("tell the owner", "do not call", "spell it out"):
            assert directive not in result["say"].lower()
            assert directive not in result["fallback_say"].lower()
        # And the model speaking `say` verbatim genuinely disarms the timer —
        # the mechanism the directive-in-say defect broke.
        report = run_announcer(f"""
            const say = {json.dumps(result["say"])};
            announcer.announce(say, null, {json.dumps(result["fallback_say"])});
            announcer.onResponseDone(say);
            fireTimers();
        """)
        assert report["spoken"] == []

    def test_the_client_has_no_silent_catch(self):
        """The four silent paths §3.5 names. ``catch { return; }`` was the
        JSON-parse drop; a bare swallow must not come back."""
        page = client.page("buddy", "tok")
        assert "catch { return; }" not in page


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

    def test_the_interrupt_class_is_exactly_escalation(self):
        """The wave-3 surviving mutant: widening the tier to include
        kind:request passed the whole suite, because the waits test used
        kind:done. This pins the CLASS boundary, not one member of it —
        every non-escalation kind the msg channel ships (done, note,
        request, ingest) must WAIT for the full gate, and each is
        volunteered once it opens. `request` matters most: it is the kind
        every buddy write emits, and the commonest actionable one."""
        for kind in ("done", "note", "request", "ingest"):
            report = run_notifier(f"""
                spool = [{{ id: "m1", from: "reviewer", kind: "{kind}",
                           text: "need a call" }}];
                speakable = false;
                interruptable = true;
                await notifier.pollOnce();
                logs.push("held: " + announcedCalls.length);
                speakable = true;
                await notifier.pollOnce();
            """)
            assert "held: 0" in report["logs"], f"kind {kind} rode the interrupt tier"
            assert len(report["announced"]) == 1, f"kind {kind} was lost by waiting"

    def test_a_mixed_batch_under_interrupt_takes_only_the_escalation(self):
        """And the skipped ordinary message is NOT buried by the ack: acking
        the spoken escalation advances the cursor past everything, so the
        done-report must come back through the strays path on the next full
        tick — the same never-lose-one property the peek/ack race has."""
        report = run_notifier("""
            spool = [
              { id: "m1", from: "minecraft", kind: "done", text: "done" },
              { id: "m2", from: "watchdog", kind: "escalation", text: "auth expired" },
            ];
            speakable = false;
            interruptable = true;
            await notifier.pollOnce();
            logs.push("interrupt took: " + announcedCalls.length);
            await notifier.noticeSpoken(announcedCalls[0].meta);
            speakable = true;
            await notifier.pollOnce();
        """)
        assert "interrupt took: 1" in report["logs"]
        assert len(report["announced"]) == 2
        assert report["announced"][0]["meta"]["inboxIds"] == ["m2"]
        assert report["announced"][1]["meta"]["inboxIds"] == ["m1"]

    def test_the_owner_speaking_blocks_even_an_escalation(self):
        """The unconditional leg. Nothing — including the alarm — speaks over
        the owner; both gates report closed and the escalation waits."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "watchdog", kind: "escalation", text: "parked" }];
            speakable = false;
            interruptable = false;   // ownerSpeaking or confirm outstanding
            await notifier.pollOnce();
            interruptable = true;
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1

    def test_an_escalation_stray_is_taken_and_an_ordinary_stray_stays(self):
        """Strays are cursor-past — the array is their only route back. The
        interrupt tick must pull only what it may speak and leave the rest
        IN the array, not drop them on the floor."""
        report = run_notifier("""
            strays.push({ id: "s1", from: "minecraft", kind: "note", text: "fyi" });
            strays.push({ id: "s2", from: "watchdog", kind: "escalation", text: "blocked pane" });
            speakable = false;
            interruptable = true;
            await notifier.pollOnce();
            logs.push("strays left: " + strays.length);
            speakable = true;
            await notifier.pollOnce();
        """)
        assert "strays left: 1" in report["logs"]
        assert len(report["announced"]) == 2
        assert report["announced"][0]["meta"]["inboxIds"] == ["s2"]
        assert report["announced"][1]["meta"]["inboxIds"] == ["s1"]

    def test_an_escalation_notice_names_itself_as_one(self):
        report = run_notifier("""
            spool = [{ id: "m1", from: "watchdog", kind: "escalation", text: "auth expired" }];
            await notifier.pollOnce();
        """)
        text = report["announced"][0]["text"]
        assert text.startswith("Heads up")
        assert "escalated" in text

    def test_the_meta_carries_the_message_kinds_for_the_ledger(self):
        """The page registers re-raise items from onSpoken, which sees only
        the meta — so the meta must carry id/from/kind/text."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "reviewer", kind: "request", text: "need a call on the API shape" }];
            await notifier.pollOnce();
        """)
        msgs = report["announced"][0]["meta"]["inboxMsgs"]
        assert msgs == [{"id": "m1", "from": "reviewer", "kind": "request",
                         "text": "need a call on the API shape"}]

    def test_a_notifier_without_the_new_deps_behaves_as_before(self):
        """The deps are optional: absent canInterrupt means escalations wait
        like everything else — no tier appears by accident."""
        report = run_notifier("""
            notifier = makeNotifier({ canInterrupt: undefined, reRaise: undefined });
            spool = [{ id: "m1", from: "watchdog", kind: "escalation", text: "parked" }];
            speakable = false;
            await notifier.pollOnce();
        """)
        assert report["announced"] == []


_RERAISE_HARNESS = """
let clock = 0;
const logs = [];
const ledger = createReRaiseLedger({
  now: () => clock,
  dueMs: 120000,
  onLog: (kind, detail) => logs.push(kind + ": " + detail),
});
const texts = [];
// The happy path: the composed reminder gets spoken, which is what spends it.
function tick() {
  const t = ledger.dueText();
  if (t) { texts.push(t.text); ledger.spoken(t.ids); }
  return t;
}
function report() {
  return JSON.stringify({ texts, logs, pending: ledger.pending() });
}
"""


def run_reraise(script: str) -> dict:
    program = "\n".join(
        [
            client.reraise_source(),
            _RERAISE_HARNESS,
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


class TestTheReRaiseLedger:
    """#967. Insistence is about the second attempt: something the owner was
    told and did not act on is raised again, once; something they acted on is
    not. Distinguishable in a test, not by taste."""

    def test_not_acted_on_is_raised_again_after_the_due_window(self):
        report = run_reraise("""
            ledger.register("m1", { from: "reviewer", text: "need a call on the API" });
            clock = 119999;
            tick();
            clock = 120000;
            tick();
        """)
        assert len(report["texts"]) == 1
        assert "reviewer" in report["texts"][0]
        assert "Still open" in report["texts"][0]

    def test_acted_on_is_never_raised_again(self):
        report = run_reraise("""
            ledger.register("m1", { from: "reviewer", text: "need a call" });
            ledger.actedOn("reviewer");
            clock = 999999;
            tick();
        """)
        assert report["texts"] == []
        assert report["pending"] == 0

    def test_the_second_mention_is_also_the_last(self):
        """Twice is a peer; a third time is a nag — and in a screenless
        channel an unbounded reminder loop has no off switch."""
        report = run_reraise("""
            ledger.register("m1", { from: "reviewer", text: "need a call" });
            clock = 500000;
            tick();
            clock = 900000;
            tick();
            tick();
        """)
        assert len(report["texts"]) == 1

    def test_acting_on_one_session_leaves_another_session_pending(self):
        report = run_reraise("""
            ledger.register("m1", { from: "reviewer", text: "call on the API" });
            ledger.register("m2", { from: "billing", text: "rotate the key" });
            ledger.actedOn("reviewer");
            clock = 500000;
            tick();
        """)
        assert len(report["texts"]) == 1
        assert "billing" in report["texts"][0]
        assert "reviewer" not in report["texts"][0]

    def test_registering_the_same_id_twice_does_not_double_the_reminder(self):
        """A retried announcement after a reconnect re-registers; the clock
        must not restart and the mention count must not double."""
        report = run_reraise("""
            ledger.register("m1", { from: "reviewer", text: "need a call" });
            clock = 100000;
            ledger.register("m1", { from: "reviewer", text: "need a call" });
            clock = 120000;   // due from the FIRST registration
            tick();
            clock = 900000;
            tick();
        """)
        assert len(report["texts"]) == 1

    def test_two_due_items_are_one_utterance(self):
        report = run_reraise("""
            ledger.register("m1", { from: "reviewer", text: "call on the API" });
            ledger.register("m2", { from: "billing", text: "rotate the key" });
            clock = 500000;
            tick();
        """)
        assert len(report["texts"]) == 1
        assert "reviewer" in report["texts"][0] and "billing" in report["texts"][0]

    def test_a_long_body_is_trimmed_for_speech(self):
        report = run_reraise("""
            ledger.register("m1", { from: "reviewer", text: "x".repeat(500) });
            clock = 500000;
            tick();
        """)
        assert len(report["texts"][0]) < 300

    def test_an_unspoken_reminder_comes_due_again(self):
        """The wave-3 D3 observation, fixed: composing the reminder spends
        nothing. If the announcement dies before it is spoken (stop(), a
        cancelled announce), the same reminder comes due again; only
        spoken(ids) — the page's onSpoken evidence — closes it. Consuming at
        compose time was the ack-before-spoken shape that was a real defect
        in #962 D1."""
        report = run_reraise("""
            ledger.register("m1", { from: "reviewer", text: "need a call" });
            clock = 500000;
            const first = ledger.dueText();          // composed, never spoken
            const second = ledger.dueText();         // still due — not spent
            texts.push(second.text);
            ledger.spoken(second.ids);               // NOW it is spent
            if (ledger.dueText() !== null) texts.push("BUG: third mention");
            logs.push("same: " + (first.text === second.text));
        """)
        assert report["texts"] == [report["texts"][0]]  # exactly one close
        assert "same: true" in report["logs"]
        assert report["pending"] == 0

    def test_the_reminder_is_speakable_and_names_the_close(self):
        """The owner cannot skim speech: the reminder must say it is the
        second and last mention, so they know the buddy will now drop it."""
        report = run_reraise("""
            ledger.register("m1", { from: "reviewer", text: "need a call" });
            clock = 500000;
            tick();
        """)
        text = report["texts"][0]
        assert "Second mention" in text
        assert "`" not in text and "_" not in text


class TestReRaiseThroughTheNotifier:
    """The re-raise's clock is the notifier's own tick — no second timer, no
    second speaking path. A reminder fires only on a QUIET full-gate tick:
    fresh news outranks it, the interrupt tier never carries it."""

    def test_a_quiet_full_gate_tick_speaks_the_due_reminder(self):
        report = run_notifier("""
            let clock = 0;
            ledger = createReRaiseLedger({ now: () => clock, dueMs: 120000 });
            notifier = makeNotifier({ reRaise: ledger });
            ledger.register("m1", { from: "reviewer", text: "need a call" });
            clock = 500000;
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1
        assert "Still open" in report["announced"][0]["text"]
        assert report["announced"][0]["meta"] == {"reRaise": True,
                                                  "reRaiseIds": ["m1"]}

    def test_fresh_news_outranks_the_reminder(self):
        report = run_notifier("""
            let clock = 0;
            ledger = createReRaiseLedger({ now: () => clock, dueMs: 120000 });
            notifier = makeNotifier({ reRaise: ledger });
            ledger.register("m1", { from: "reviewer", text: "need a call" });
            clock = 500000;
            spool = [{ id: "m2", from: "minecraft", kind: "done", text: "done" }];
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1
        assert "minecraft" in report["announced"][0]["text"]
        assert "Still open" not in report["announced"][0]["text"]

    def test_the_interrupt_tier_never_carries_a_reminder(self):
        """A reminder is politeness, not an alarm — the relaxed gate must
        not leak it past the buddy's own chatter."""
        report = run_notifier("""
            let clock = 0;
            ledger = createReRaiseLedger({ now: () => clock, dueMs: 120000 });
            notifier = makeNotifier({ reRaise: ledger });
            ledger.register("m1", { from: "reviewer", text: "need a call" });
            clock = 500000;
            speakable = false;
            interruptable = true;
            await notifier.pollOnce();
        """)
        assert report["announced"] == []

    def test_a_blocked_tick_does_not_burn_the_reminder(self):
        """Both halves: blocked is silent, and the SAME reminder still fires
        on the next open tick — dueText marks nothing; only spoken evidence
        (the page's onSpoken) spends the second mention."""
        report = run_notifier("""
            let clock = 0;
            ledger = createReRaiseLedger({ now: () => clock, dueMs: 120000 });
            notifier = makeNotifier({ reRaise: ledger });
            ledger.register("m1", { from: "reviewer", text: "need a call" });
            clock = 500000;
            speakable = false;
            await notifier.pollOnce();
            speakable = true;
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1


class TestThePersonaAndInterruptWiring:
    """The page-source pins for #967: the tier, the ledger, and the pinned
    speaking-path count."""

    def test_the_page_wires_the_interrupt_gate_without_the_chatter_leg(self):
        """canInterrupt keeps the unconditional legs of #962 — owner not
        speaking, no confirm handshake — and drops responseActive /
        announcer.pending, which is what lets an escalation pre-empt the
        buddy's own speech via the announcer's existing cancel.

        Asserted as the legs it HAS and the legs it must NOT have, rather than
        as the whole line: #978 item 2 adds a third leg to the handshake side
        (a proposal still in the announcer's pipe is a handshake the gate's own
        TTL cannot yet see), and pinning the literal made that indistinguishable
        from reinstating the chatter leg.
        """
        page = client.page("buddy", "tok")
        wiring = page.split("canInterrupt: ", 1)[1].split("\n", 1)[0]
        assert "!ownerSpeaking" in wiring
        assert "!confirmGate.outstanding()" in wiring
        # The chatter leg, still gone: an escalation does not wait for the
        # buddy's own in-flight response or its queue depth.
        assert "responseActive" not in wiring
        assert "announcer.pending()" not in wiring
        # The FULL gate is unchanged — #962's rule survives verbatim.
        assert (
            "canSpeak: () => !ownerSpeaking && !responseActive"
            " && !!announcer && announcer.pending() === 0"
            " && !confirmGate.outstanding()," in page
        )

    def test_the_interrupt_tier_adds_no_speaking_path(self):
        """#950's pin: still exactly two response.create sites. The escalation
        tier and the re-raise both ride announce()."""
        page = client.page("buddy", "tok")
        assert page.count('type: "response.create"') == 2

    def test_the_page_embeds_the_ledger_verbatim_and_wires_it(self):
        page = client.page("buddy", "tok")
        assert client.reraise_source().strip() in page
        assert "const RERAISE_DUE_MS" in page
        assert "createReRaiseLedger({" in page
        wiring = page.split("createInboxNotifier({", 1)[1].split("});", 1)[0]
        assert "reRaise: reRaiseLedger," in wiring

    def test_only_asking_kinds_enter_the_ledger_and_only_once_heard(self):
        """register lives in onSpoken's inboxIds branch — the moment there is
        evidence the owner heard the notice — and takes only request and
        escalation. A done/note is news; re-raising news is chatter."""
        page = client.page("buddy", "tok")
        onspoken = page.split("function onSpoken(meta, how)", 1)[1].split("function send(", 1)[0]
        register_at = onspoken.split("reRaiseLedger.register", 1)[0]
        assert '"request"' in register_at and '"escalation"' in register_at
        # And nowhere else on the page registers.
        assert page.count("reRaiseLedger.register(") == 1

    def test_the_reminder_is_spent_only_from_on_spoken(self):
        """The commit side of mark-on-spoken: exactly one spoken() call site
        on the page, inside onSpoken's reRaise branch — the announce path
        never spends the mention it is still trying to deliver."""
        page = client.page("buddy", "tok")
        assert page.count("reRaiseLedger.spoken(") == 1
        onspoken = page.split("function onSpoken(meta, how)", 1)[1].split(
            "function send(", 1)[0]
        assert "reRaiseLedger.spoken(meta.reRaiseIds || [])" in onspoken

    def test_the_ledger_survives_stop(self):
        """Page-lifetime, like heardReplies: stop() must not touch it, or a
        reconnect wipes the peer's memory of its own words. Same for the
        router, whose proposal-target memory the actedOn leg depends on."""
        page = client.page("buddy", "tok")
        stop_body = page.split("function stop() {", 1)[1].split("\n}", 1)[0]
        assert "reRaiseLedger." not in stop_body
        assert "outcomeRouter" not in stop_body


# =============================================================================
# The outcome router: the write outcome's two signals, behaviorally
# =============================================================================

_ROUTER_HARNESS = """
const resolvedCalls = [];
const actedOn = [];
const router = createOutcomeRouter({
  gate: { resolved: () => resolvedCalls.push(1) },
  ledger: { actedOn: (s) => actedOn.push(s) },
});
function report() {
  return JSON.stringify({ resolved: resolvedCalls.length, actedOn });
}
"""


def run_outcome_router(script: str) -> dict:
    program = "\n".join(
        [
            client.outcome_router_source(),
            _ROUTER_HARNESS,
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


def _second_write_tools():
    """A SECOND gated write, declared from the OUTSIDE via #966's WriteSpec —
    the write the hard-coded-name gate leg could never have reopened on. Built
    through the real ``gated_triple`` + a real ``ConfirmSpine`` so the payloads
    the router is fed are the generalisation's actual output, not a fixture's
    idea of it."""
    spec = write_tools.WriteSpec(
        name="beacon_flare",
        action="lighting the beacon",
        params_schema={"type": "object", "properties": {}, "additionalProperties": False},
        freeze=lambda args: write_tools.FrozenWrite(
            session="watchtower",
            instruction="light it",
            argv_prefix=("echo", "beacon"),
            append_body=False,
        ),
        announce_template="Light the beacon at {session}? Say {phrase}.",
        fallback_template="Light the beacon at {session}?",
    )
    triple = write_tools.gated_triple(spec)
    handlers = {name: fn for name, _desc, _schema, fn in triple}
    spine = confirm.ConfirmSpine(transcript.TranscriptRing(), wait_s=0.0)
    return handlers, spine


class TestTheOutcomeRouter:
    """#966's composition seam: the gate leg used to reopen on hard-coded tool
    names, so the FIRST second gated write would have left the gate closed for
    its full TTL — the buddy silently mute, nothing on screen to say why. The
    router keys on the payload's own confirm_terminal instead, and these tests
    drive it with payloads produced by an actual second write."""

    def test_a_second_gated_writes_cancel_reopens_the_gate(self):
        handlers, spine = _second_write_tools()
        proposal = handlers["propose_beacon_flare"]({}, spine)
        outcome = handlers["cancel_beacon_flare"](
            {"confirm_token": proposal["confirm_token"]}, spine
        )
        # The generalisation's promise, checked from outside: the terminal
        # signal is in the payload, no client-side name list required.
        assert outcome["confirm_terminal"] is True
        report = run_outcome_router(f"""
            router.route("cancel_beacon_flare", {json.dumps(outcome)});
        """)
        assert report["resolved"] == 1
        # Terminal is NOT acted-on: a cancelled beacon retires no reminders.
        assert report["actedOn"] == []

    def test_a_second_writes_wait_outcome_keeps_the_gate_closed(self):
        """The other edge: a confirm attempted before the proposal was spoken
        is a WAIT outcome — the proposal is still live, so the gate must stay
        closed rather than reopen volunteering mid-handshake."""
        handlers, spine = _second_write_tools()
        proposal = handlers["propose_beacon_flare"]({}, spine)
        outcome = handlers["send_beacon_flare"](
            {"confirm_token": proposal["confirm_token"]}, spine
        )
        assert outcome["reason"] in confirm.WAIT_OUTCOMES
        assert outcome["confirm_terminal"] is False
        report = run_outcome_router(f"""
            router.route("send_beacon_flare", {json.dumps(outcome)});
        """)
        assert report["resolved"] == 0
        assert report["actedOn"] == []

    def test_a_queued_send_retires_the_acted_sessions_reminder(self):
        """#967's acted-on leg, against the real approved-verdict payload
        shape: the payload's own acted_session — frozen at propose time —
        names what to retire. No name check, no proposal memory."""
        approved = confirm.Verdict(
            approved=True,
            reason="approved",
            utterance="confirm juniper",
            acted_session="reviewer",
        ).to_dict()
        assert approved["success"] is True and approved["confirm_terminal"] is True
        report = run_outcome_router(f"""
            router.route("send_session_message", {json.dumps(approved)});
        """)
        assert report["resolved"] == 1
        assert report["actedOn"] == ["reviewer"]

    def test_interleaved_proposals_retire_the_confirmed_one(self):
        """The case the old last-proposal correlation got wrong: propose to
        alpha, propose to beta, then confirm ALPHA's write. The client-side
        guess remembered only beta, so it retired beta's reminders — the
        false-accept (beta's re-raise silently never happens) AND the
        false-reject (alpha keeps nagging about something done) in one move.
        The payload's frozen acted_session cannot make that mistake."""
        approved_alpha = confirm.Verdict(
            approved=True, reason="approved", acted_session="alpha"
        ).to_dict()
        report = run_outcome_router(f"""
            router.route("propose_session_message",
                {{ success: true, session: "alpha" }});
            router.route("propose_session_message",
                {{ success: true, session: "beta" }});
            router.route("send_session_message", {json.dumps(approved_alpha)});
        """)
        assert report["resolved"] == 1
        assert report["actedOn"] == ["alpha"]

    def test_a_session_message_cancel_reopens_but_never_retires(self):
        """The priced false-accept: a cancel is terminal but is NOT acting —
        retiring on it silently loses the re-raise the ledger exists for.
        The cancel payload carries no acted_session, so even with a prior
        proposal in flight nothing retires."""
        denied = confirm.Verdict(approved=False, reason="denied").to_dict()
        assert denied["confirm_terminal"] is True
        assert "acted_session" not in denied
        report = run_outcome_router(f"""
            router.route("propose_session_message",
                {{ success: true, session: "reviewer" }});
            router.route("cancel_session_message", {json.dumps(denied)});
        """)
        assert report["resolved"] == 1
        assert report["actedOn"] == []

    def test_a_second_gated_writes_approval_retires_its_own_session(self):
        """The genericisation's payoff: a write declared from the outside
        (#966) retires its OWN frozen session's reminders with no client-side
        knowledge of its name — the same property the gate leg already has."""
        approved = confirm.Verdict(
            approved=True,
            reason="approved",
            success_say="Beacon lit.",
            acted_session="watchtower",
        ).to_dict()
        report = run_outcome_router(f"""
            router.route("send_beacon_flare", {json.dumps(approved)});
        """)
        assert report["resolved"] == 1
        assert report["actedOn"] == ["watchtower"]

    def test_a_payloadless_result_routes_nowhere(self):
        report = run_outcome_router("""
            router.route("send_session_message", null);
            router.route("buddy_inbox", { success: true, messages: [] });
        """)
        assert report["resolved"] == 0
        assert report["actedOn"] == []


# =============================================================================
# #978 wave 2 — the announcer/timer races and the silent-failure retry holes
# =============================================================================


class TestAnEscalationCannotSpeakInsideTheHandshake:
    """#978 item 2. The confirm gate closes at ``anchored()`` — i.e. once the
    proposal has been SPOKEN. Between the tool result coming back and that
    moment, the proposal announcement is queued or mid-flight and the gate is
    still open, so an escalation ticking right then passed ``canInterrupt``,
    queued behind the proposal, and ``pump()`` promoted it the instant
    anchoring closed the gate. The buddy speaks an alarm exactly between "say
    confirm tango" and the owner's answer.

    The announcer is the only thing that knows a proposal is in the pipe, so
    the missing leg is asked of it.
    """

    def test_a_queued_proposal_announcement_reports_an_anchor_pending(self):
        report = run_announcer("""
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
        """)
        assert report["anchorPending"] is True

    def test_it_stays_pending_while_the_proposal_waits_behind_another_item(self):
        """Queued, not merely current: an escalation promoted ahead of a
        proposal still lands inside the window the guard exists to protect."""
        report = run_announcer("""
            announcer.announce("The voice service reported an error.", { errorNotice: true });
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
        """)
        assert report["pending"] == 2
        assert report["anchorPending"] is True

    def test_it_clears_once_the_proposal_has_actually_been_spoken(self):
        """The false-reject half. The leg must open again the moment the
        anchor fires, or the gate's own TTL is no longer the bound on how long
        the buddy can be muted — a second, unbounded mute would sit in front
        of it."""
        report = run_announcer("""
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
            announcer.onResponseDone("To send it, say confirm tango.");
        """)
        assert report["anchorPending"] is False
        assert [a["how"] for a in report["anchored"]] == ["model"]

    def test_an_ordinary_announcement_never_reports_one(self):
        report = run_announcer("""
            announcer.announce("Two updates came in.", { inboxIds: ["m1"] });
        """)
        assert report["anchorPending"] is False

    def test_the_page_wires_the_leg_into_can_interrupt(self):
        page = client.page("buddy", "tok")
        wiring = page.split("canInterrupt: ", 1)[1].split("\n", 1)[0]
        assert "announcer.anchorPending()" in wiring
        # The two legs #967 made unconditional must still be there — this is an
        # addition, never a replacement.
        assert "!ownerSpeaking" in wiring
        assert "confirmGate.outstanding()" in wiring


class TestTheFallbackDoesNotSpeakOverTheOwner:
    """#978 item 3. "Never while the owner is speaking" held at GATE time and
    nowhere else: the timer fires 6-12s later and spoke unconditionally.

    Both halves are priced, and the false-reject half is why this is a bounded
    deferral rather than a condition. A fallback that waits for silence
    forever is a refusal the owner never hears — the exact failure the
    default-on timer exists to make impossible. So: defer while they are
    talking, up to a fixed count, then speak anyway. Talking over the owner
    once beats never telling them.
    """

    def test_it_defers_while_the_owner_is_talking(self):
        report = run_announcer("""
            announcer.announce("I didn't hear the confirmation phrase.");
            ownerIsSpeaking = true;
            fireTimers();
        """)
        assert report["spoken"] == []
        assert report["armed"] is True, "must re-arm, never simply drop"

    def test_it_speaks_the_moment_they_stop(self):
        report = run_announcer("""
            announcer.announce("I didn't hear the confirmation phrase.");
            ownerIsSpeaking = true;
            fireTimers();
            ownerIsSpeaking = false;
            fireTimers();
        """)
        assert report["spoken"] == ["I didn't hear the confirmation phrase."]

    def test_an_owner_who_never_stops_is_still_told(self):
        """The bound. Without it a long monologue silently swallows a refusal
        — and a refusal the owner never hears is the one outcome this whole
        mechanism exists to rule out."""
        report = run_announcer("""
            announcer.announce("I didn't hear the confirmation phrase.");
            ownerIsSpeaking = true;
            for (let i = 0; i < 12; i++) fireTimers();
        """)
        assert report["spoken"] == ["I didn't hear the confirmation phrase."]

    def test_the_owner_deferral_is_counted_separately_from_the_response_one(self):
        """Two deferrals for two different reasons must not share a budget:
        an announcement that already deferred once behind our own in-flight
        audio still deserves its full owner-speaking grace, and vice versa."""
        report = run_announcer("""
            announcer.announce("I didn't hear the confirmation phrase.");
            announcer.onResponseCreated();   // our audio may be mid-flight
            fireTimers();                    // -> the sawCreate deferral
            ownerIsSpeaking = true;
            fireTimers();                    // -> an owner deferral, not a refusal to defer
        """)
        assert report["spoken"] == []
        assert report["armed"] is True

    def test_the_page_gives_the_announcer_the_owner_speaking_signal(self):
        """The injected deps did not expose it, which is why the timer could
        not check it at all."""
        page = client.page("buddy", "tok")
        deps = page.split("announcer = createAnnouncer({", 1)[1].split("});", 1)[0]
        assert "ownerSpeaking:" in deps


class TestStopTakesTheArmedTimerWithIt:
    """#978 item 4. ``stop()`` nulled the announcer, but the armed
    ``setTimeout`` closure survived it: 6s into "idle" the browser voice
    speaks, and ``onSpoken(meta, "fallback")`` anchors the proposal on the
    bridge — closing the NEXT session's volunteering gate for up to 120s over
    a proposal nobody is answering. d45601b covered page-lifetime strays;
    ``disarm`` was only ever reachable from ``onResponseDone``/``cancel``.
    """

    def test_teardown_disarms_the_current_item(self):
        report = run_announcer("""
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
            announcer.teardown();
            fireTimers();
        """)
        assert report["spoken"] == []
        assert report["armedTimers"] == 0

    def test_teardown_never_reports_anything_as_spoken(self):
        """A torn-down item was NOT heard, and the anchor is the one thing
        that must never be told otherwise."""
        report = run_announcer("""
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
            announcer.teardown();
            fireTimers();
        """)
        assert report["anchored"] == []

    def test_teardown_drops_the_queue_too(self):
        report = run_announcer("""
            announcer.announce("first", { anchor: "p1" });
            announcer.announce("second", { inboxIds: ["m1"] });
            announcer.teardown();
            fireTimers();
        """)
        assert report["pending"] == 0
        assert report["spoken"] == []

    def test_the_page_tears_the_announcer_down_before_dropping_it(self):
        page = client.page("buddy", "tok")
        stop_body = page.split("function stop() {", 1)[1].split("\n}", 1)[0]
        assert "announcer.teardown()" in stop_body
        assert stop_body.index("announcer.teardown()") < stop_body.index(
            "announcer = null"
        )


class TestASilentlyFailedAnnouncementIsRetried:
    """#978 item 5. ``speechSynthesis`` fails silently — the code says so
    itself — and ``utterance.onerror`` logged to the DOM without firing
    ``onSpokenAloud``. So an inbox notice was neither HEARD nor ACKED, yet its
    id sat in ``inFlight`` for the rest of the session and suppressed every
    later tick: the comment promising "the unheard notice is retried" was
    describing a path the code did not have.
    """

    def test_a_failed_browser_voice_reports_not_spoken(self):
        report = run_announcer("""
            speakFails = true;
            announcer.announce("Two updates came in.", { inboxIds: ["m1"] });
            fireTimers();
        """)
        assert report["anchored"] == [], "a failed utterance was never heard"
        assert [m["inboxIds"] for m in report["notSpoken"]] == [["m1"]]

    def test_a_successful_one_still_reports_spoken_only(self):
        """The must-fail control for the assertion above: if onNotSpoken fired
        on the success path too, every test here would pass for the wrong
        reason."""
        report = run_announcer("""
            announcer.announce("Two updates came in.", { inboxIds: ["m1"] });
            fireTimers();
        """)
        assert report["notSpoken"] == []
        assert [a["how"] for a in report["anchored"]] == ["fallback"]

    def test_the_notice_is_volunteered_again_on_a_later_tick(self):
        """The property the comment claimed. ``noticeFailed`` releases the
        id, so the next gated tick says it again — the message is not lost
        for the session over a browser-voice failure."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "docs", kind: "done", text: "draft ready" }];
            await notifier.pollOnce();
            const meta = announcedCalls[0].meta;
            notifier.noticeFailed(meta);
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 2
        assert report["cursor"] == 0, "never acked — it was never heard"

    def test_without_the_release_it_is_suppressed_forever(self):
        """The control. Same script, no ``noticeFailed`` — one announcement,
        which is the defect."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "docs", kind: "done", text: "draft ready" }];
            await notifier.pollOnce();
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1

    def test_a_heard_notice_is_never_released_by_a_later_failure(self):
        """``seen`` is what settles a notice, and it outranks this: releasing
        an id the owner HAS heard would replay it."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "docs", kind: "done", text: "draft ready" }];
            await notifier.pollOnce();
            const meta = announcedCalls[0].meta;
            await notifier.noticeSpoken(meta);
            notifier.noticeFailed(meta);
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1

    def test_the_page_routes_a_browser_voice_error_back(self):
        page = client.page("buddy", "tok")
        deps = page.split("announcer = createAnnouncer({", 1)[1].split("      onSpoken,", 1)[0]
        assert "onSpeakFailed" in deps, "the speak dep must take a failure callback"
        assert "onNotSpoken" in page
        handler = page.split("function onNotSpoken(meta)", 1)[1].split("\n}", 1)[0]
        assert "noticeFailed" in handler


class TestTheStopTimeRaceCannotStrandAStray:
    """#978 item 6. ``pollOnce`` is not cancellable mid-flight. After
    ``stop()`` the full gate fails but ``canInterrupt`` could still pass, and
    ``announce`` with a null announcer did a bare ``speechSynthesis.speak`` —
    no meta, no ``onSpoken``, never acked, never seen. Being cursor-past, the
    spool will never return those replies: the ``strays`` array is their only
    way back to the owner's ear, and this path emptied it into nothing.
    """

    def test_a_poll_resolving_after_stop_announces_nothing(self):
        report = run_notifier("""
            spool = [{ id: "m1", from: "watchdog", kind: "escalation", text: "wedged" }];
            const inFlight = notifier.pollOnce();
            notifier.stop();
            await inFlight;
        """)
        assert report["announced"] == []

    def test_it_never_takes_a_stray_out_of_the_array(self):
        """The invariant the array exists for. A stray dropped here is lost
        for good — the cursor is already past it, so the spool will never
        return it. The stopped check therefore runs BEFORE the pull, not after
        it: putting them back would work too, but only if every later return
        path remembered to."""
        report = run_notifier("""
            strays.push({ id: "s1", from: "docs", kind: "escalation", text: "still open" });
            const inFlight = notifier.pollOnce();
            notifier.stop();
            await inFlight;
        """)
        assert report["announced"] == []
        assert report["strays"] == 1

    def test_a_stray_survives_to_the_next_session(self):
        """Whole-loop: the array is page-lifetime, so the notifier built after
        a reconnect finds it and says it."""
        report = run_notifier("""
            strays.push({ id: "s1", from: "docs", kind: "done", text: "still open" });
            const inFlight = notifier.pollOnce();
            notifier.stop();
            await inFlight;
            notifier = makeNotifier();
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1
        assert "still open" in report["announced"][0]["text"]

    def test_the_page_will_not_announce_through_a_dead_announcer(self):
        """The second half of the fix: even if a tick did get through, the
        interrupt gate now requires a live announcer, so nothing reaches the
        bare speechSynthesis path with inbox meta attached."""
        page = client.page("buddy", "tok")
        wiring = page.split("canInterrupt: ", 1)[1].split("\n", 1)[0]
        assert "!!announcer" in wiring


class TestCarriedTheReasonIsNotDecidedByStopwords:
    """#978 item 7. Overlap ≥ 0.6 over raw tokens, with duplicates
    double-counting. The greeting is ~7/9 stopwords, so an unrelated VAD reply
    that happens to share them verdicts "the model said it" and DISARMS —
    which in the greeting's case means the greet-as-health-check (b4446fb)
    reports the scripted-speech path healthy when the greeting never happened,
    and the browser voice that would have said MODEL_AUDIO_DEAD never fires.

    The pricing runs the other way from most guards here: tightening this
    moves errors from "silently believed spoken" to "said twice". A paraphrase
    that drops content words now falls through to the fallback voice, so the
    owner hears it in a robot voice — possibly after the model already said
    something like it. Double-speak is the cheap failure; silence is not.
    """

    def test_the_greeting_is_not_disarmed_by_a_stopword_heavy_reply(self):
        report = run_announcer("""
            announcer.announce("Hey, I'm listening. What's on your mind?",
                               { greeting: true }, "MODEL AUDIO DEAD");
            announcer.onResponseDone("I'm not sure what's on your mind today");
            fireTimers();
        """)
        assert report["spoken"] == ["MODEL AUDIO DEAD"]
        assert [a["how"] for a in report["anchored"]] == ["fallback"]

    def test_the_real_greeting_still_disarms_it(self):
        """The false-reject half, and the reason this is a weighting and not a
        stricter threshold: the greeting the model actually speaks must still
        count, or every healthy session speaks MODEL_AUDIO_DEAD."""
        report = run_announcer("""
            announcer.announce("Hey, I'm listening. What's on your mind?",
                               { greeting: true }, "MODEL AUDIO DEAD");
            announcer.onResponseDone("Hey — I'm listening. What's on your mind?");
            fireTimers();
        """)
        assert report["spoken"] == []
        assert [a["how"] for a in report["anchored"]] == ["model"]

    def test_a_proposal_is_not_anchored_by_a_stopword_echo(self):
        """The same mechanism on the path that costs the most: a false disarm
        here anchors a proposal on a response that never stated it, and the
        owner's correct nonce is then judged against a proposal they were
        never read."""
        report = run_announcer("""
            announcer.announce("To send it to the orchestrator, say confirm tango.",
                               { anchor: "p1" }, "To send it, say the word I gave you.");
            announcer.onResponseDone("Do you want me to send it to the orchestrator or not");
        """)
        assert report["anchored"] == []
        assert report["armed"] is True

    def test_repeated_words_cannot_pad_the_overlap(self):
        """Duplicates double-counted, so a reply repeating one shared word
        could carry the score on its own."""
        report = run_announcer("""
            announcer.announce("Send the report to the reviewer now",
                               { anchor: "p1" });
            announcer.onResponseDone("the the the the the the the the");
        """)
        assert report["anchored"] == []

    def test_an_all_stopword_script_can_still_be_verified(self):
        """The degenerate case the weighting must not break: if a scripted
        line has NO content words, dropping them all would leave nothing to
        compare and the announcement could never disarm — an unconditional
        double-speak on every such line."""
        report = run_announcer("""
            announcer.announce("What is it about?");
            announcer.onResponseDone("What is it about?");
            fireTimers();
        """)
        assert report["spoken"] == []

    def test_every_spine_line_the_model_speaks_verbatim_still_disarms(self):
        """The broad false-reject sweep. Whatever the weighting is, saying the
        line EXACTLY must always count — otherwise the fallback doubles every
        refusal the model gets right."""
        from agentwire.voice_layer import confirm as confirm_mod

        for reason, line in confirm_mod.SPOKEN.items():
            report = run_announcer(f"""
                announcer.announce({json.dumps(line)});
                announcer.onResponseDone({json.dumps(line)});
                fireTimers();
            """)
            assert report["spoken"] == [], reason


class TestTheCommentsDescribeTheCodeTheySitOn:
    """The nits, pinned. A false sentence at a use site is how the next
    reviewer reads the wrong mechanism as the shipped one — this module's
    recurring failure, and the reason each of these is asserted rather than
    merely fixed.
    """

    def test_the_wait_outcomes_comment_names_all_three(self):
        """``in_flight`` became a wait outcome in #987 and the enumeration at
        the dispatch site still said two."""
        page = client.page("buddy", "tok")
        dispatch = page.split("async function handleFunctionCall(item)", 1)[1]
        dispatch = dispatch.split("function spokenText", 1)[0]
        assert "pending_transcript / not_announced)" not in dispatch
        for name in ("pending_transcript", "not_announced", "in_flight"):
            assert name in dispatch, name

    def test_nothing_in_the_client_branches_on_a_wait_outcome_by_name(self):
        """Why the enumeration above is a comment fix and not a code fix: the
        router keys on ``confirm_terminal``, which the spine sets for every
        non-wait outcome, so a third wait outcome needed no dispatch change.
        This is the assertion that made that safe to claim."""
        page = client.page("buddy", "tok")
        script = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        code = "\n".join(
            line for line in script.splitlines() if not line.strip().startswith("//")
        )
        for name in ("pending_transcript", "not_announced", "in_flight",
                     "owner_should_wait"):
            assert name not in code, f"{name} is branched on, not just described"

    def test_the_outcome_router_comment_does_not_claim_to_hold_state(self):
        """It holds none — its own header says the opposite, and the guess it
        replaced (remember the last proposal's session) is the bug #966 fixed."""
        page = client.page("buddy", "tok")
        preamble = page.split("const outcomeRouter = createOutcomeRouter", 1)[0]
        assert "holds the most recent proposal's target session" not in preamble

    def test_the_interrupt_comment_does_not_overstate_pre_emption(self):
        page = client.page("buddy", "tok")
        assert "an escalation may pre-empt the buddy's own\n      // speech" not in page

    def test_the_module_docstring_stamps_utterances_at_speech_start(self):
        """The docstring said ``input_audio_buffer.committed``, which is the
        ordering the whole clock change exists to reject."""
        doc = client.__doc__
        assert "an utterance at ``input_audio_buffer.committed``" not in doc
        assert "input_audio_buffer.speech_started" in doc

    def test_the_ring_docstring_says_no_prior_speech_started(self):
        from agentwire.voice_layer import transcript as transcript_mod

        assert "a\ntranscript arriving with no prior commit" not in transcript_mod.__doc__
        assert "no prior ``speech_started``" in transcript_mod.__doc__


# =============================================================================
# #993 review — F2 to F6
# =============================================================================


class TestTheBrowserVoiceErrorIsActuallyWired:
    """Review F2. A MUTATION SURVIVED: deleting the call from
    ``utterance.onerror`` left the whole suite green. That one line is the
    only wire between the browser's error event and the entirety of item 5 —
    the node tests exercise the announcer's failure leg with a fake ``speak``,
    and the page pin asserted the parameter NAME in the signature and that
    ``onNotSpoken`` reaches ``noticeFailed``, but nothing asserted the handler
    in between invokes anything.

    This is not the browser half that cannot be verified here. It is a
    page-source pin, the same technique the clock origin uses.
    """

    def test_the_error_handler_invokes_the_failure_callback(self):
        page = client.page("buddy", "tok")
        handler = page.split("utterance.onerror = (event) => {", 1)[1]
        handler = handler.split("};", 1)[0]
        assert "onSpeakFailed()" in handler

    def test_the_success_handler_still_invokes_the_spoken_one(self):
        """The control: an assertion that only ever looked at onerror would
        pass just as well with onend gutted."""
        page = client.page("buddy", "tok")
        handler = page.split("utterance.onend = () => {", 1)[1].split("};", 1)[0]
        assert "onSpokenAloud()" in handler


class TestAFailedStrayIsPutBack:
    """Review F3. ``pollOnce`` SPLICES a stray out of the array when it pulls
    one, and a stray is cursor-past: the spool will never return it. Releasing
    only the id from ``inFlight`` therefore leaves the message gone from both
    places, so "the next gated tick says it again" was FALSE for exactly the
    class that becomes strays — escalations, the one kind that emails the
    owner on dead-letter.

    ``test_it_never_takes_a_stray_out_of_the_array`` forbids this loss, and
    its own docstring named the trap: putting them back works "only if every
    later return path remembered to". This is the return path that did not.
    """

    def test_a_failed_stray_is_volunteered_again(self):
        report = run_notifier("""
            strays.push({ id: "s1", from: "watchdog", kind: "escalation",
                          text: "a done report dead-lettered" });
            await notifier.pollOnce();
            notifier.noticeFailed(announcedCalls[0].meta);
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 2
        assert "dead-lettered" in report["announced"][1]["text"]

    def test_it_is_back_in_the_array_not_merely_re_announced(self):
        """The array is the page-lifetime store that survives a reconnect. An
        id released into nothing is announced once more and then gone."""
        report = run_notifier("""
            strays.push({ id: "s1", from: "watchdog", kind: "escalation",
                          text: "a done report dead-lettered" });
            await notifier.pollOnce();
            notifier.noticeFailed(announcedCalls[0].meta);
        """)
        assert report["strays"] == 1

    def test_it_is_not_put_back_twice(self):
        report = run_notifier("""
            strays.push({ id: "s1", from: "watchdog", kind: "escalation",
                          text: "wedged" });
            await notifier.pollOnce();
            const meta = announcedCalls[0].meta;
            notifier.noticeFailed(meta);
            notifier.noticeFailed(meta);
        """)
        assert report["strays"] == 1

    def test_a_heard_stray_is_never_put_back(self):
        """``seen`` outranks the release, here as everywhere: re-queueing a
        stray the owner HAS heard replays it with no cursor to suppress it."""
        report = run_notifier("""
            strays.push({ id: "s1", from: "watchdog", kind: "escalation",
                          text: "wedged" });
            await notifier.pollOnce();
            const meta = announcedCalls[0].meta;
            await notifier.noticeSpoken(meta);
            notifier.noticeFailed(meta);
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 1
        assert report["strays"] == 0

    def test_a_spool_message_is_not_pushed_into_the_strays_array(self):
        """The other direction. A message still in the spool needs only the
        inFlight release — the cursor never moved. Pushing it into strays too
        would announce it twice once the cursor finally advances."""
        report = run_notifier("""
            spool = [{ id: "m1", from: "docs", kind: "done", text: "draft ready" }];
            await notifier.pollOnce();
            notifier.noticeFailed(announcedCalls[0].meta);
            await notifier.pollOnce();
        """)
        assert len(report["announced"]) == 2
        assert report["strays"] == 0


class TestTheHandshakeGateCoversTheFallbackSpeech:
    """Review F4. ``armFallback`` nulls ``current`` BEFORE calling ``speak``,
    and the real ``speak`` is asynchronous — ``onSpokenAloud`` runs from
    ``utterance.onend``. In between, ``anchorPending()`` and
    ``confirmGate.outstanding()`` are both false, so ``canInterrupt`` passes
    and an alarm goes out while the browser voice is still saying "...say
    confirm tango". The window is roughly an utterance long against a 5s poll.

    The unit harness could not see it: its ``speak`` called back
    synchronously, so the window had zero width. That is the fixture-shaped
    blind spot, and it is closed here (``speakDefers``/``finishSpeech``)
    before the behaviour is asserted — a pin written against the old fixture
    would be theatre.
    """

    def test_the_proposal_is_still_pending_while_the_voice_is_speaking(self):
        report = run_announcer("""
            speakDefers = true;
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
            fireTimers();                    // the fallback starts speaking
            logs.push("mid-speech: " + announcer.anchorPending());
            finishSpeech();
            logs.push("after-speech: " + announcer.anchorPending());
        """)
        assert "mid-speech: true" in report["logs"]
        assert "after-speech: false" in report["logs"]

    def test_the_control_that_the_old_fixture_could_not_have_caught(self):
        """With a synchronous speak there is no window at all, so this shape
        reads identical whether the fix is present or not. Stated so the pin
        above cannot be quietly reverted to the cheaper fixture."""
        report = run_announcer("""
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
            fireTimers();
            logs.push("mid-speech: " + announcer.anchorPending());
        """)
        assert "mid-speech: false" in report["logs"]

    def test_the_buddy_counts_as_speaking_while_the_voice_runs(self):
        """The same window on the FULL gate: canSpeak keys on pending(), and
        a notice volunteered mid-fallback talks over the buddy's own voice."""
        report = run_announcer("""
            speakDefers = true;
            announcer.announce("Two updates came in.", { inboxIds: ["m1"] });
            fireTimers();
            logs.push("mid-speech pending: " + announcer.pending());
            finishSpeech();
            logs.push("after-speech pending: " + announcer.pending());
        """)
        assert "mid-speech pending: 1" in report["logs"]
        assert "after-speech pending: 0" in report["logs"]

    def test_a_failed_utterance_also_ends_the_window(self):
        """The false-reject half. If only the success path cleared it, a
        browser voice that errored would leave the gate shut for the rest of
        the session — an unbounded mute in front of the TTL."""
        report = run_announcer("""
            speakDefers = true;
            speakFails = true;
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
            fireTimers();
            finishSpeech();
            logs.push("after-failure: " + announcer.anchorPending());
        """)
        assert "after-failure: false" in report["logs"]
        assert report["anchored"] == []

    def test_an_utterance_that_never_ends_is_watchdogged(self):
        """The false-reject half of F4's own fix, and it is the expensive one.

        ``speechSynthesis`` can drop an utterance without firing ``onend`` OR
        ``onerror``. Since this window gates volunteering, believing it
        forever is an UNBOUNDED mute — strictly worse than the one
        interjection the window exists to prevent. So the belief expires.
        """
        report = run_announcer("""
            speakDefers = true;
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
            fireTimers();                    // the fallback speaks; nothing ends
            logs.push("mid-speech: " + announcer.anchorPending());
            fireTimers();                    // ...and the watchdog comes due
            logs.push("after-watchdog: " + announcer.anchorPending());
            logs.push("after-watchdog pending: " + announcer.pending());
        """)
        assert "mid-speech: true" in report["logs"]
        assert "after-watchdog: false" in report["logs"]
        assert "after-watchdog pending: 0" in report["logs"]
        assert any("no end event within" in line for line in report["logs"])

    def test_a_real_end_event_cancels_the_watchdog(self):
        """The control: a watchdog left armed after a normal utterance would
        fire into a cleared state and log a failure that did not happen."""
        report = run_announcer("""
            speakDefers = true;
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
            fireTimers();
            finishSpeech();
        """)
        assert report["armedTimers"] == 0
        assert not any("no end event" in line for line in report["logs"])

    def test_a_torn_down_announcer_reports_nothing_pending(self):
        """stop() during fallback speech must not leave the window latched."""
        report = run_announcer("""
            speakDefers = true;
            announcer.announce("To send it, say confirm tango.", { anchor: "p1" });
            fireTimers();
            announcer.teardown();
            logs.push("after-teardown: " + announcer.anchorPending());
        """)
        assert "after-teardown: false" in report["logs"]


class TestTheDeferralBoundIsWhatItSays:
    """Review F5. The two deferrals STACK — the owner-speaking one is checked
    first and re-arms, and the in-flight one is still available on the
    re-armed timer. So the worst case is 5 fires, not 4, and the stated
    ``fallbackMs * (1 + maxOwnerDeferrals)`` was short by one whole interval.

    Pinned rather than restructured: stacking is CORRECT — the two deferrals
    answer different questions ("is the owner talking" and "is this our own
    audio still playing"), and making them share a budget would let a
    monologue consume the grace that stops the buddy speaking over itself.
    The defect was the arithmetic in the comment, so the number is now the
    thing under test.
    """

    def test_the_worst_case_is_five_intervals(self):
        report = run_announcer("""
            announcer.announce("I didn't hear the confirmation phrase.");
            announcer.onResponseCreated();     // our own audio may be in flight
            ownerIsSpeaking = true;
            for (let i = 0; i < 4; i++) {
                fireTimers();
                logs.push("fire " + (i + 1) + " spoken=" + spoken.length);
            }
            fireTimers();
            logs.push("fire 5 spoken=" + spoken.length);
        """)
        for n in range(1, 5):
            assert f"fire {n} spoken=0" in report["logs"]
        assert "fire 5 spoken=1" in report["logs"]

    def test_the_stated_bound_matches(self):
        page = client.page("buddy", "tok")
        assert "fallbackMs * (1 + maxOwnerDeferrals)" not in page
        assert "fallbackMs * (2 + maxOwnerDeferrals)" in page


class TestTheDedupHalfOfTheWeightingIsPinned:
    """Review F6. Dropping ``uniq`` from ``want`` survived the whole suite:
    the only repetition test used a STOPWORD, which the weighting filters out
    before the dedup can matter, so that half was never exercised.

    It is not a redundant belt: a proposal announcement legitimately repeats
    its nonce for clarity, and without the dedup a one-line model echo of just
    the nonce scores 0.6 and ANCHORS the proposal — the announcer reporting
    that a proposal the model never stated was spoken.
    """

    def test_a_nonce_echo_does_not_anchor_a_repeated_proposal(self):
        report = run_announcer("""
            announcer.announce(
              "Say confirm tango. Again, that's confirm tango. " +
              "To send it, say confirm tango.",
              { anchor: "p1" });
            announcer.onResponseDone("confirm tango");
        """)
        assert report["anchored"] == []
        assert report["armed"] is True

    def test_the_same_line_said_in_full_still_disarms(self):
        """The false-reject half: repetition in the script must not make the
        script harder for the model to satisfy honestly."""
        report = run_announcer("""
            const line = "Say confirm tango. Again, that's confirm tango. " +
                         "To send it, say confirm tango.";
            announcer.announce(line, { anchor: "p1" });
            announcer.onResponseDone(line);
            fireTimers();
        """)
        assert report["spoken"] == []
        assert [a["how"] for a in report["anchored"]] == ["model"]
