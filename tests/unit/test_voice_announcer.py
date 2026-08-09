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
let timers = [];
let nextHandle = 1;
let channelOpen = true;

const announcer = createAnnouncer({
  send: (e) => { if (!channelOpen) return false; events.push(e); return true; },
  speak: (t, onDone) => { spoken.push(t); if (onDone) onDone(); },
  onSpoken: (meta, how) => anchored.push({ meta: meta, how: how }),
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
function report() {
  return JSON.stringify({
    events, spoken, logs, anchored,
    armedTimers: timers.length,
    pending: announcer.pending(),
    armed: announcer.armed(),
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
