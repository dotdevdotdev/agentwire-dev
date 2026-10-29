"""#1190 — propose → spoken proposal → "Confirm Bravo." → send_session_message.

The owner proposed a message, heard it read back, said "Confirm Bravo." — and
nothing happened. Two halves are pinned here:

- **The client** (the real served page, tests/page_harness.py): once the model
  emits ``send_session_message``, the page must dispatch it with the token,
  must not cancel the owner's confirm turn, and must announce the outcome —
  whether the scripted proposal was heard verbatim, mismatched (fallback
  voice), or confirmed late. This path was driven against the owner's exact
  sequence and is NOT where #1190 broke; the pin keeps it that way.
- **The live replay's confirm step** (:mod:`agentwire.voice_layer.replay`),
  driven offline against a fake Realtime model: the replay must hand the model
  the same event sequence the page does (scripted announce, no extra
  ordinary response), judge a missing or token-less confirm call as a failure,
  and report a refused service as a refusal rather than blaming the model.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agentwire.voice_layer import replay
from agentwire.voice_layer.client import SCRIPTED_INSTRUCTION

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node runs the page JS")

PROPOSE = replay.CANNED_OUTPUTS["propose_session_message"]
SEND = replay.CANNED_OUTPUTS["send_session_message"]


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

def _client_flow(scripted_text: str, delay_ms: int, barge_in: bool = False) -> dict:
    from tests.page_harness import run_page

    return run_page(f"""
    const PROPOSE = {json.dumps(PROPOSE)};
    const SEND = {json.dumps(SEND)};
    toolHandler = (name) => name === "propose_session_message" ? PROPOSE
      : name === "send_session_message" ? SEND : {{ success: true }};
    await connect();
    await modelResponds({{ text: "Hey, I'm listening. What's on your mind?" }});

    serverEvent({{ type: "input_audio_buffer.speech_started", item_id: "u1" }});
    serverEvent({{ type: "input_audio_buffer.committed", item_id: "u1" }});
    await modelResponds({{ calls: [{{ name: "propose_session_message",
      args: {{ session: "agentwire", message: "x" }} }}] }});
    serverEvent({{ type: "conversation.item.input_audio_transcription.completed", item_id: "u1",
      transcript: "Let's tell the agentwire session…" }});
    await settle();
    const confirmAt = wire.length;
    if ({json.dumps(barge_in)}) {{
      // The owner says the phrase WHILE the proposal is still being read:
      // server VAD cancels the scripted turn with a partial transcript.
      serverEvent({{ type: "response.created", response: {{ id: "script" }} }});
      serverEvent({{ type: "input_audio_buffer.speech_started", item_id: "u2" }});
      serverEvent({{ type: "response.done", response: {{ id: "script", status: "cancelled",
        output: [{{ type: "message", content: [{{ transcript: {json.dumps(scripted_text)} }}] }}] }} }});
    }} else {{
      await modelResponds({{ text: {json.dumps(scripted_text)} }});
      await advance({delay_ms});
      serverEvent({{ type: "input_audio_buffer.speech_started", item_id: "u2" }});
    }}
    serverEvent({{ type: "input_audio_buffer.committed", item_id: "u2" }});
    await modelResponds({{ calls: [{{ name: "send_session_message",
      args: {{ confirm_token: PROPOSE.confirm_token }} }}] }});
    serverEvent({{ type: "conversation.item.input_audio_transcription.completed", item_id: "u2",
      transcript: "Confirm Bravo." }});
    await settle();
    await advance(15000);
    await drainTrace();
    result = {{
      sends: bridge.tools.filter((t) => t.name === "send_session_message").map((t) => t.arguments),
      afterConfirm: wire.slice(confirmAt).map((e) => e.type),
      scripted: wire.filter((e) => e.response && e.response.instructions).map((e) => e.response.instructions),
      stalls: bridge.traces.filter((e) => e.kind === "stall").map((e) => e.fields.stall),
      anchors: bridge.anchors.map((a) => a.proposal_id),
    }};
    """)


@needs_node
def test_a_spoken_confirm_reaches_the_bridge_and_its_outcome_is_announced():
    variants = {
        "heard verbatim": (PROPOSE["say"], 0),
        "model paraphrased (fallback voice speaks it)": ("Ready when you are.", 0),
        "confirmed after the fallback window": (PROPOSE["say"], 7000),
        "barged in on the proposal": (PROPOSE["say"][:40], 0),
    }
    bad = {}
    for label, (scripted_text, delay) in variants.items():
        r = _client_flow(scripted_text, delay, barge_in=label.startswith("barged"))
        problems = []
        if r["sends"] != [{"confirm_token": PROPOSE["confirm_token"]}]:
            problems.append(f"send dispatched as {r['sends']}")
        if "response.cancel" in r["afterConfirm"]:
            problems.append("the page cancelled the owner's confirm turn")
        if not any(SEND["say"] in s for s in r["scripted"]):
            problems.append("the queued outcome was never announced")
        if r["anchors"] != [PROPOSE["anchor_proposal_id"]]:
            problems.append(f"anchors {r['anchors']}")
        if r["stalls"]:
            problems.append(f"stalled: {r['stalls']}")
        if problems:
            bad[label] = problems
    assert not bad, bad


# ---------------------------------------------------------------------------
# The replay's confirm step, against a fake Realtime model
# ---------------------------------------------------------------------------

class FakeRealtime:
    """Answers each ``response.create`` from *policy(items, response_params)*."""

    def __init__(self, policy):
        self.policy = policy
        self.sent: list[dict] = []
        self.items: list[dict] = []
        self.queue: asyncio.Queue = asyncio.Queue()

    async def send_json(self, event: dict) -> None:
        self.sent.append(event)
        if event["type"] == "conversation.item.create":
            self.items.append(event["item"])
        if event["type"] == "response.create":
            response = self.policy(self.items, event.get("response") or {})
            await self.queue.put(json.dumps({"type": "response.done", "response": response}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return SimpleNamespace(data=await self.queue.get())


def _call(name: str, **args) -> dict:
    return {"type": "function_call", "name": name, "call_id": f"call_{name}",
            "arguments": json.dumps(args)}


def _say(text: str) -> dict:
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def _model(on_confirm):
    """A model that proposes, reads scripts verbatim, and answers the confirm
    with whatever *on_confirm(token)* returns."""
    def policy(items, params):
        if params.get("instructions"):
            return {"status": "completed", "output": [_say(params["instructions"][len(SCRIPTED_INSTRUCTION):])]}
        last = [i for i in items if i.get("type") == "message"][-1]["content"][0]["text"]
        if last.startswith("Let's tell"):
            return {"status": "completed", "output": [_call(
                "propose_session_message", session=replay.REPLAY_SESSION, message=replay.REPLAY_MESSAGE)]}
        token = next(json.loads(i["output"])["confirm_token"] for i in reversed(items)
                     if i.get("type") == "function_call_output" and "confirm_token" in i["output"])
        return on_confirm(token)
    return policy


CONFIRM_STEPS = replay.STEPS[-2:]


def _run(policy):
    fake = FakeRealtime(policy)

    @asynccontextmanager
    async def connect(_key):
        yield fake

    results = asyncio.run(replay.replay("unused", CONFIRM_STEPS, connect=connect))
    return fake, results


def test_the_replay_hands_the_model_the_pages_sequence_and_passes_a_good_confirm():
    fake, results = _run(_model(lambda token: {"status": "completed", "output": [
        _call("send_session_message", confirm_token=token)]}))
    assert [replay.judge(t, s) for t, s in zip(results, CONFIRM_STEPS)] == [[], []]
    # After each must_speak output, the ONE response is the announcer's script —
    # never a bare create the page would not have sent.
    creates = [e for e in fake.sent if e["type"] == "response.create"]
    scripted = [e["response"]["instructions"] for e in creates if e.get("response")]
    assert scripted == [SCRIPTED_INSTRUCTION + PROPOSE["say"], SCRIPTED_INSTRUCTION + SEND["say"]]
    assert len(creates) == 4  # propose turn, its script, confirm turn, its script


def test_the_1190_shapes_fail_the_judge():
    shapes = {
        "no call (the owner's transcript)": (
            lambda token: {"status": "completed", "output": []},
            "never called any of ['send_session_message']"),
        "wrong token": (
            lambda token: {"status": "completed", "output": [
                _call("send_session_message", confirm_token="not-the-token")]},
            "confirm_token="),
    }
    missed = {}
    for label, (on_confirm, expect) in shapes.items():
        _, results = _run(_model(on_confirm))
        problems = replay.judge(results[-1], CONFIRM_STEPS[-1])
        if not any(expect in p for p in problems):
            missed[label] = problems
    assert not missed, missed


def test_a_refused_service_is_reported_not_judged():
    def out_of_credits(items, params):
        return {"status": "failed", "output": [], "status_details": {
            "type": "failed", "error": {"type": "insufficient_quota", "code": "insufficient_quota"}}}

    with pytest.raises(replay.ServiceRefusedError, match="insufficient_quota"):
        _run(out_of_credits)
