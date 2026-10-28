"""#1179 — a parallel batch of tool calls gets ONE response, after ALL outputs.

The live failure: the buddy fired five fleet reads in one response, the client
created a response after the FIRST output, and the model narrated the other
four as "still running" — they had all returned in about a second. These tests
drive the page's own dispatch code (extracted, never copied) through the
replayed batch from :mod:`agentwire.voice_layer.replay` with stubbed I/O.
"""

import json
import shutil
import subprocess

import pytest

from agentwire.voice_layer import client, replay
from tests.page_slice import page_slice

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to run the client's own JS"
)


def _dispatch_source() -> str:
    """sendFunctionCallOutput … dispatchToolBatch, as the page serves them."""
    return page_slice(
        client.page("buddy", "tok"),
        r"function maybeCreateResponse\(\)",
        r"async function dispatchToolBatch\(calls\) \{[\s\S]*?\n\}\n",
        "the tool-batch dispatch",
        shape=r"async function handleFunctionCall[\s\S]*async function dispatchToolBatch",
    )


def _run_batch(calls: list[dict], results: dict[str, dict]) -> dict:
    """Dispatch *calls* through the real page code; return what went on the wire.

    ``post`` resolves on a timer, LAST call fastest, so a create keyed to any
    one output's arrival is visibly early.
    """
    program = "\n".join([
        "let responseActive = false;",
        "const wire = [];",
        "const spoken = [];",
        "function send(e) { wire.push(e); return true; }",
        "function log() {}",
        "function announce(text) { spoken.push(text); wire.push({ type: 'announce', text }); }",
        "const outcomeRouter = { route() {} };",
        f"const RESULTS = {json.dumps(results)};",
        f"const N = {len(calls)};",
        "let i = 0;",
        "function post(path, body) {",
        "  const delay = (N - i++) * 5;",
        "  return new Promise((r) => setTimeout(() => r(RESULTS[body.name]), delay));",
        "}",
        _dispatch_source(),
        f"await dispatchToolBatch({json.dumps(calls)});",
        "console.log(JSON.stringify({ wire, spoken }));",
    ])
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _calls(names) -> list[dict]:
    return [
        {"type": "function_call", "name": n, "call_id": f"call_{i}", "arguments": "{}"}
        for i, n in enumerate(names)
    ]


def _kinds(wire: list[dict]) -> list[str]:
    return [
        e["item"]["type"] if e["type"] == "conversation.item.create" else e["type"]
        for e in wire
    ]


def test_the_replayed_batch_gets_one_response_after_every_output():
    """The #1179 batch: five outputs, THEN exactly one response.create.

    Mutation-checked: restoring the per-output ``maybeCreateResponse()`` puts
    a create after the first output and this goes red.
    """
    out = _run_batch(_calls(replay.FAILED_BATCH), replay.CANNED_OUTPUTS)
    n = len(replay.FAILED_BATCH)
    assert _kinds(out["wire"]) == ["function_call_output"] * n + ["response.create"]
    assert [json.loads(e["item"]["output"]) for e in out["wire"][:n]] == [
        replay.CANNED_OUTPUTS[name] for name in replay.FAILED_BATCH
    ]


def test_a_refusal_in_the_batch_is_announced_after_every_output_and_alone():
    """A must_speak result takes the turn — but only once the whole batch has
    landed, and with no ordinary create racing the announcer's scripted one."""
    refusal = {"success": False, "say": "No session called zed.", "must_speak": True}
    results = {**replay.CANNED_OUTPUTS, "fleet_session_output": refusal}
    names = ["fleet_sessions", "fleet_session_output", "fleet_projects"]
    out = _run_batch(_calls(names), results)
    assert _kinds(out["wire"]) == ["function_call_output"] * 3 + ["announce"]
    assert out["spoken"] == ["No session called zed."]


def test_a_single_call_still_gets_its_response():
    out = _run_batch(_calls(["fleet_sessions"]), replay.CANNED_OUTPUTS)
    assert _kinds(out["wire"]) == ["function_call_output", "response.create"]


class TestTheReplayJudge:
    """The live eval's scorer, pinned on the transcript it exists to reject."""

    def test_rejects_the_failed_exchange(self):
        problems = replay.judge([
            {"calls": list(replay.FAILED_BATCH), "said": ""},
            {"calls": [], "said": replay.FAILED_ANSWER},
        ])
        assert any("words" in p for p in problems), problems
        assert any("pending" in p for p in problems), problems

    def test_accepts_one_batch_and_a_short_answer(self):
        assert replay.judge([
            {"calls": list(replay.FAILED_BATCH), "said": ""},
            {"calls": [], "said": "Taskbar PR 1177 is dangling, portal fix is still active."},
        ]) == []

    def test_rejects_serial_calls_and_speaking_early(self):
        problems = replay.judge([
            {"calls": ["fleet_sessions"], "said": "Checking."},
            {"calls": ["fleet_dangling"], "said": ""},
            {"calls": [], "said": "One dangling PR."},
        ])
        assert len(problems) == 3, problems
