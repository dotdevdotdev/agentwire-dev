"""Portal side of the voice assistant (#1198, slice 3): its audio goes to the
dashboard, the button lands in `voice ask`, and a spoken yes/no answers a
pending approval."""

import argparse
import json
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentwire import prompt_router, voice_cli
from agentwire import voice_hooks as vh
from agentwire.config import load_config
from agentwire.voice_cli import classify_reply as real_classify

#: The approval dialog as it rendered live on the assistant's pane.
APPROVAL_SCREEN = """⏺ Bash(eval "echo approval-probe")
────────────────────────────────────────────────────────────────────────────────
 Bash command
 eval "echo approval-probe"
 │ Hook PreToolUse:Bash requires confirmation for this command:
 │ Unverifiable command (eval) — nothing can analyze what this runs, so a human
 │ must confirm it (not demoted by bypass) [settings]
 Do you want to proceed?
 ❯ 1. Yes
   2. No

 Esc to cancel · Tab to amend
"""


# ── server: audio + connections ──────────────────────────────────────────


def _ws():
    ws = MagicMock()
    ws.send_json = AsyncMock()
    return ws


@pytest.fixture
def server(tmp_path, monkeypatch):
    from agentwire.server import AgentWireServer

    srv = AgentWireServer(load_config(tmp_path / "none.yaml"))
    own = types.SimpleNamespace(clients=set(), config=types.SimpleNamespace(voice=None))
    srv._get_or_create_session = AsyncMock(return_value=own)
    srv._kokoro_shim_ready = AsyncMock(return_value=False)  # browser-synth path
    srv._speak_no_clients_fallback = AsyncMock(return_value=True)
    return srv


async def test_voice_audio_goes_to_every_open_portal_page(server):
    pages = {_ws(), _ws()}
    server.dashboard_clients = set(pages)

    assert await server.speak("agentwire-voice", "Four sessions are running.") is True

    for ws in pages:
        sent = [c.args[0] for c in ws.send_json.await_args_list]
        assert {"type": "speak_text", "session": "agentwire-voice",
                "text": "Four sessions are running."} in sent
    server._speak_no_clients_fallback.assert_not_awaited()


async def test_with_no_portal_open_voice_falls_back_to_the_speakers(server):
    server.dashboard_clients = set()
    await server.speak("agentwire-voice", "Done.")
    server._speak_no_clients_fallback.assert_awaited_once()


async def test_other_sessions_still_need_their_own_window(server):
    server.dashboard_clients = {_ws()}
    await server.speak("some-session", "hello")
    server._speak_no_clients_fallback.assert_awaited_once()


async def test_the_cli_sees_open_pages_as_the_assistants_listeners(server):
    server.dashboard_clients = {_ws(), _ws()}
    request = types.SimpleNamespace(match_info={"name": "agentwire-voice"})
    resp = await server.api_session_connections(request)
    assert json.loads(resp.body) == {"has_connections": True, "connection_count": 2}


async def test_the_button_calls_voice_ask_and_hook_states_reach_the_chip(server):
    server.run_agentwire_cmd = AsyncMock(return_value=(True, {"success": True}))
    server.broadcast_dashboard = AsyncMock()

    req = types.SimpleNamespace(json=AsyncMock(return_value={"text": "-which sessions are busy"}))
    await server.api_voice_ask(req)
    server.run_agentwire_cmd.assert_awaited_once_with(["voice", "ask", "--", "-which sessions are busy"])

    await server.api_voice_state(types.SimpleNamespace(json=AsyncMock(return_value={"state": "done"})))
    bad = await server.api_voice_state(types.SimpleNamespace(json=AsyncMock(return_value={"state": "x"})))
    states = [c.args[1]["state"] for c in server.broadcast_dashboard.await_args_list]
    assert states == ["sending", "done"] and bad.status == 400


# ── voice ask: spoken answers to a pending approval ─────────────────────


#: The dialog as Claude Code renders it with a feedback option.
FEEDBACK_SCREEN = APPROVAL_SCREEN.replace(
    " ❯ 1. Yes\n   2. No",
    " ❯ 1. Yes\n   2. Yes, and don't ask again for eval commands\n"
    "   3. No, and tell Claude what to do differently (esc)")


@pytest.fixture
def ask(tmp_path, monkeypatch):
    """`voice ask` against a running assistant whose pane shows *screen*,
    with the reply classifier stubbed to *verdict*."""
    calls = types.SimpleNamespace(sent=[], answered=[], spoken=[], queued=[], judged=[],
                                  screen="", verdict="other", scope=None)
    monkeypatch.delenv("CLAUDECODE", raising=False)  # the owner's channel, not an agent
    svc = types.SimpleNamespace(name="agentwire-voice")
    monkeypatch.setattr(voice_cli, "_service", lambda: svc)
    monkeypatch.setattr(voice_cli, "REMINDED_FILE", tmp_path / "reminded")
    monkeypatch.setattr(vh, "APPROVAL_FILE", tmp_path / "approval.json")
    (tmp_path / "approval.json").write_text(json.dumps({"intent": "Prune merged worktrees"}))
    monkeypatch.setattr("agentwire.services.set_disabled", lambda *a: None)
    monkeypatch.setattr("agentwire.services.start_service", lambda s: (True, "already running"))
    monkeypatch.setattr("agentwire.send_cli.cmd_send", lambda a: calls.sent.append(a.prompt) or 0)
    monkeypatch.setattr("agentwire.inbox.enqueue",
                        lambda to, text, kind, sender: calls.queued.append((text, sender)))
    monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, escapes=False: calls.screen)
    monkeypatch.setattr(prompt_router, "answer",
                        lambda s, p, h, keys: calls.answered.append(keys) or (True, "sent"))
    monkeypatch.setattr(vh, "speak", lambda text, kind="say", display=None: calls.spoken.append(text))
    monkeypatch.setattr(voice_cli, "classify_reply",
                        lambda intent, text: calls.judged.append((intent, text))
                        or (calls.verdict, calls.scope))

    def run(text):
        return voice_cli.cmd_voice_ask(argparse.Namespace(text=[text], json=True))

    calls.run = run
    return calls


def test_the_whole_utterance_is_judged_against_the_dialogs_intent(ask):
    ask.screen, ask.verdict = APPROVAL_SCREEN, "approve"
    ask.run("yes, that's fine")
    assert ask.judged == [("Prune merged worktrees", "yes, that's fine")]


def test_a_bare_approval_presses_the_plain_yes_and_queues_nothing(ask):
    ask.screen, ask.verdict = FEEDBACK_SCREEN, "approve"
    assert ask.run("yes go ahead") == 0
    assert ask.answered == [["1"]] and ask.queued == [] and ask.sent == []


def test_approve_with_an_extra_request_delivers_the_full_utterance_next(ask):
    ask.screen, ask.verdict = FEEDBACK_SCREEN, "approve_and_more"
    assert ask.run("yes and then open it") == 0
    assert ask.answered == [["1"]]  # the one-time Yes, never "don't ask again"
    assert ask.queued == [("yes and then open it", "owner")]
    assert ask.sent == []  # never pasted into the live dialog


def test_deny_with_a_redirect_picks_tell_claude_and_sends_all_of_it(ask):
    ask.screen, ask.verdict = FEEDBACK_SCREEN, "deny"
    assert ask.run("no, check the logs first instead") == 0
    assert ask.answered == [["3"]]
    assert ask.queued == [("no, check the logs first instead", "owner")]


def test_other_fails_closed_queues_the_text_and_reminds_once(ask, capsys):
    ask.screen, ask.verdict = APPROVAL_SCREEN, "other"
    assert ask.run("which sessions are busy") == 1
    assert ask.run("and what is the scheduler doing") == 1
    assert ask.answered == [] and ask.sent == []
    assert ask.queued == [("which sessions are busy", "owner"),
                          ("and what is the scheduler doing", "owner")]
    assert ask.spoken == ["An approval is waiting. Say yes or no."]
    first, _ = json.JSONDecoder().raw_decode(capsys.readouterr().out)
    assert first["pending_approval"] is True


def test_a_classifier_failure_never_approves(ask, monkeypatch):
    """The real classifier, with its model call timing out / erroring /
    answering off-script: every one is "other" — no keys pressed."""
    import subprocess

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=8)

    replies = [timeout,
               lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="approve"),
               lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="Approve it, sure!"),
               lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="")]
    verdicts = [real_classify("x", "yes", run=r) for r in replies]
    assert verdicts == [("other", None)] * 4

    ask.screen = APPROVAL_SCREEN
    monkeypatch.setattr(voice_cli, "classify_reply", lambda intent, text: ("other", None))
    ask.run("yes")
    assert ask.answered == [] and ask.queued == [("yes", "owner")]


def test_the_classifier_reads_a_clean_verdict(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["timeout"] = cmd, kw.get("timeout")
        return types.SimpleNamespace(returncode=0,
                                     stdout="answer: yes\nalso_asks: yes\nlasts: once\n")

    assert real_classify("Prune worktrees", "yes and open it",
                         run=fake_run) == ("approve_and_more", None)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--tools") + 1] == ""  # no tools: it can only answer
    assert 'Owner\'s entire utterance: "yes and open it"' in cmd[2]
    assert seen["timeout"] == voice_cli.CLASSIFIER_TIMEOUT_S


def test_yes_never_picks_a_persistent_grant(ask):
    ask.screen = APPROVAL_SCREEN.replace(
        " ❯ 1. Yes\n   2. No",
        " ❯ 1. Yes, and don't ask again for eval commands\n   2. No")
    ask.verdict = "approve_and_more"
    assert ask.run("yes and then open it") == 1
    assert ask.answered == []
    assert ask.queued == [("yes and then open it", "owner")]  # still not dropped
    assert ask.spoken == ["I can't approve that one by voice. It's waiting in the portal."]


def test_with_no_dialog_a_yes_is_just_a_request(ask):
    ask.screen, ask.verdict = "❯ \n", "approve"
    ask.run("yes")
    assert ask.sent == [["yes"]] and ask.answered == [] and ask.judged == []


def test_approval_keys_pick_the_plain_one_time_yes():
    opts = [{"number": 1, "label": "Yes, and don't ask again for git"},
            {"number": 2, "label": "Yes"},
            {"number": 3, "label": "No"},
            {"number": 4, "label": "No, and tell Claude what to do differently (esc)"}]
    assert voice_cli.approval_keys(opts, "yes") == ["2"]
    assert voice_cli.approval_keys(opts, "no") == ["4"]
    assert voice_cli.approval_keys([{"number": 1, "label": "Yes, allow all edits"}], "yes") == []
    assert voice_cli.approval_keys([{"number": 1, "label": "Yes"}], "no") == ["Escape"]


def test_owner_requests_queued_behind_a_dialog_get_an_ack(tmp_path, monkeypatch):
    import io
    import sys

    said = []
    monkeypatch.setattr(vh, "STATE_FILE", tmp_path / "turn.json")
    monkeypatch.setattr(vh, "signal", lambda s: None)
    monkeypatch.setattr(vh, "speak", lambda text, kind="say", display=None: said.append(text))
    for prompt in ("[MSG from owner · request] open it  ⟨#ab12⟩",
                   "[MSG from worker-x · done] PR is up  ⟨#cd34⟩"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "prompt_id": "p"})))
        vh.main(["prompt-submit"])
    assert said == ["On it."]


# ── the spoken approval request: intent, not command ────────────────────


def test_approval_speaks_the_intent_and_shows_the_command(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "message": {"content": "clean up"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I'll prune the merged worktrees."},
            {"type": "tool_use", "name": "Bash", "input": {
                "command": "agentwire worktree --prune --gc-merged && rm -r /tmp/x",
                "description": "Prune merged worktrees and clear the temp dir"}},
        ]}},
    ]))
    spoken, display = vh.approval_request(
        {"transcript_path": str(transcript), "message": "Claude needs your permission to use Bash"})
    assert spoken == "Prune merged worktrees and clear the temp dir. Yes or no?"
    assert "agentwire worktree --prune --gc-merged && rm -r /tmp/x" in display
    assert "rm" not in spoken and "--" not in spoken

    # No description on the call: the assistant's own stated plan.
    rows = transcript.read_text().replace(', "description": "Prune merged worktrees and clear the temp dir"', "")
    transcript.write_text(rows)
    assert vh.approval_request({"transcript_path": str(transcript)})[0] == \
        "I'll prune the merged worktrees. Yes or no?"

    # Unreadable transcript: Claude's own notification text, still one sentence.
    spoken, display = vh.approval_request({"transcript_path": str(tmp_path / "gone"),
                                           "message": "Claude needs your permission to use Bash"})
    assert (spoken, display) == ("Claude needs your permission to use Bash. Yes or no?", None)


# ── hooks drive the chip ────────────────────────────────────────────────


def test_hooks_signal_working_done_and_failed(tmp_path, monkeypatch):
    import io
    import sys

    states: list[str] = []
    monkeypatch.setattr(vh, "STATE_FILE", tmp_path / "turn.json")
    monkeypatch.setattr(vh, "speak", lambda *a, **k: None)
    monkeypatch.setattr(vh, "signal", states.append)

    def fire(event, **payload):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        vh.main([event])

    fire("prompt-submit", prompt="x", prompt_id="p1")
    fire("stop", prompt_id="p1", last_assistant_message="ok")
    fire("prompt-submit", prompt="y", prompt_id="p2")
    fire("stop-failure", prompt_id="p2", error="rate_limit")
    fire("stop", prompt_id="p2", last_assistant_message="")
    assert states == ["working", "done", "working", "failed"]


def test_voice_state_never_fails_the_hook_when_the_portal_is_down(monkeypatch, capsys):
    def down(*a, **k):
        raise ConnectionError("portal down")

    monkeypatch.setattr("agentwire.core.portal_request", down)
    assert voice_cli.cmd_voice_state(argparse.Namespace(state="done", json=True)) == 0
    assert json.loads(capsys.readouterr().out)["delivered"] is False
