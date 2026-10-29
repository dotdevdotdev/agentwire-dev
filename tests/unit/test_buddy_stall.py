"""#1191 — stalls and service failures are LOUD: traced, shown, and said.

#1190 looked like this from the owner's chair: they said "Confirm Bravo" and
nothing happened, ever, with the page still reading "listening". Meanwhile the
OpenAI org was out of credits — and a ``response.done`` with ``status=failed``
rendered nothing at all. Every test here drives the REAL served page
(tests/page_harness.py) on a virtual clock: no network, no real waits.
"""

from __future__ import annotations

import shutil

import pytest

from agentwire.voice_layer import client
from tests.page_harness import run_page

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node runs the page JS")

GREETED = """
await connect();
await modelResponds({ text: "Hey, I'm listening. What's on your mind?" });
"""

REPORT = """
await drainTrace();
result = {
  stalls: bridge.traces.filter((e) => e.kind === "stall").map((e) => e.fields),
  banner: { hidden: __els.banner.hidden, text: __els.banner.textContent },
  spoken: spokenFallback,
  wire: wire,
};
"""


def _ms(name: str) -> int:
    import re
    return int(re.search(rf"const {name} = (\d+);", client.page("b", "t")).group(1))


def test_a_silent_turn_after_the_owner_speaks_is_a_stall_with_the_state_snapshot():
    r = run_page(GREETED + f"""
    await ownerSays("Confirm Bravo.", "item_9");
    await advance({_ms("REPLY_STALL_MS") + 500});
    """ + REPORT)
    [stall] = r["stalls"]
    assert stall["stall"] == "no_reply" and stall["transcript"] == "Confirm Bravo."
    snapshot = {"responseActive", "ownerSpeaking", "confirmOutstanding", "confirmRemainingMs",
                "announcerPending", "anchorPending", "dc", "pc"}
    assert snapshot <= stall.keys(), snapshot - stall.keys()
    assert r["banner"]["hidden"] is False and "nothing came back" in r["banner"]["text"]
    assert any("nothing came back" in s for s in r["spoken"])


def test_a_reply_already_in_flight_when_the_transcript_lands_is_not_a_stall():
    # Server VAD creates the response at the COMMIT, before transcription.
    r = run_page(GREETED + f"""
    serverEvent({{ type: "input_audio_buffer.speech_started", item_id: "i1" }});
    serverEvent({{ type: "input_audio_buffer.committed", item_id: "i1" }});
    serverEvent({{ type: "response.created", response: {{ id: "vad1" }} }});
    serverEvent({{ type: "conversation.item.input_audio_transcription.completed", item_id: "i1", transcript: "How's it going?" }});
    serverEvent({{ type: "response.done", response: {{ id: "vad1", status: "completed",
      output: [{{ type: "message", content: [{{ transcript: "All quiet." }}] }}] }} }});
    await advance({_ms("REPLY_STALL_MS") * 2});
    """ + REPORT)
    assert r["stalls"] == [] and r["banner"]["hidden"] is True


def test_a_failed_response_out_of_credits_is_typed_shown_and_said():
    r = run_page(GREETED + """
    await ownerSays("Confirm Bravo.", "item_9");
    serverEvent({ type: "response.created", response: { id: "r1" } });
    serverEvent({ type: "response.done", response: { id: "r1", status: "failed", output: [],
      status_details: { type: "failed", error: { type: "insufficient_quota",
        code: "insufficient_quota", message: "You exceeded your current quota" } } } });
    await settle();
    """ + REPORT)
    [stall] = r["stalls"]
    assert (stall["stall"], stall["reason"], stall["source"]) == (
        "failure", "insufficient_quota", "response.done")
    assert "out of credits" in r["banner"]["text"]
    assert any("OpenAI says the account is out of credits" in s for s in r["spoken"])


def test_a_failed_response_with_no_reason_is_still_loud():
    r = run_page(GREETED + """
    serverEvent({ type: "response.created", response: { id: "r1" } });
    serverEvent({ type: "response.done", response: { id: "r1", status: "failed", output: [] } });
    await settle();
    """ + REPORT)
    [stall] = r["stalls"]
    assert (stall["stall"], stall["reason"]) == ("failure", "unknown")
    assert r["banner"]["hidden"] is False and r["spoken"]


def test_a_service_error_event_is_specific_not_the_generic_notice():
    r = run_page(GREETED + """
    const before = creates().length;
    serverEvent({ type: "error", error: { type: "rate_limit_error", code: "rate_limit_exceeded",
      message: "Rate limit reached" } });
    await settle();
    """ + REPORT + """
    result.newCreates = creates().length - before;
    """)
    [stall] = r["stalls"]
    assert stall["reason"] == "rate_limit_exceeded"
    assert "rate-limiting" in r["banner"]["text"]
    # The generic path asks the (possibly dead) model to say "may have missed
    # that" — a service failure must not also take it.
    assert r["newCreates"] == 0


def test_an_ordinary_error_event_keeps_the_generic_path():
    r = run_page(GREETED + """
    serverEvent({ type: "error", error: { type: "invalid_request_error", code: "invalid_value" } });
    await settle();
    """ + REPORT)
    assert r["stalls"] == []


def test_a_socket_close_carries_the_reason_the_service_gave():
    r = run_page(GREETED + """
    serverEvent({ type: "error", error: { type: "insufficient_quota", code: "insufficient_quota" } });
    __dc.close();
    await settle();
    """ + REPORT)
    kinds = [(s["stall"], s["reason"]) for s in r["stalls"]]
    assert kinds == [("failure", "insufficient_quota"), ("channel_closed", "insufficient_quota")]
    # Same words for both — said once, not twice.
    assert sum("out of credits" in s for s in r["spoken"]) == 1


def test_stop_is_not_a_dropped_channel():
    r = run_page(GREETED + "stop(); await settle();" + REPORT)
    assert r["stalls"] == []


def test_an_invalid_key_at_connect_is_loud():
    r = run_page("""
    mintReply = { success: false, error: "(401): Incorrect API key provided [invalid_api_key]" };
    await start();
    await settle();
    """ + REPORT)
    [stall] = r["stalls"]
    assert (stall["stall"], stall["reason"], stall["source"]) == ("failure", "invalid_api_key", "connect")
    assert "rejected the API key" in r["banner"]["text"]


def test_a_response_or_tool_that_never_finishes_is_a_stall():
    r = run_page(GREETED + f"""
    serverEvent({{ type: "response.created", response: {{ id: "stuck" }} }});
    await advance({_ms("RESPONSE_STALL_MS") + 500});
    toolHandler = () => new Promise(() => {{}});
    await modelResponds({{ calls: [{{ name: "fleet_sessions", call_id: "c1" }}] }});
    await advance({_ms("TOOL_STALL_MS") + 500});
    """ + REPORT)
    assert [s["stall"] for s in r["stalls"]] == ["response_stuck", "tool_stuck"]
    assert r["stalls"][1]["name"] == "fleet_sessions"


def test_the_next_healthy_turn_clears_the_banner():
    r = run_page(GREETED + f"""
    await ownerSays("Hello?", "i1");
    await advance({_ms("REPLY_STALL_MS") + 500});
    await modelResponds({{ text: "Sorry, I'm here." }});
    """ + REPORT)
    assert [s["stall"] for s in r["stalls"]] == ["no_reply"]
    assert r["banner"]["hidden"] is True
