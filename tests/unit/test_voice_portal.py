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


@pytest.fixture
def ask(monkeypatch):
    """`voice ask` against a running assistant whose pane shows *screen*."""
    calls = types.SimpleNamespace(sent=[], answered=[], spoken=[], screen="")
    svc = types.SimpleNamespace(name="agentwire-voice")
    monkeypatch.setattr(voice_cli, "_service", lambda: svc)
    monkeypatch.setattr("agentwire.services.set_disabled", lambda *a: None)
    monkeypatch.setattr("agentwire.services.start_service", lambda s: (True, "already running"))
    monkeypatch.setattr("agentwire.send_cli.cmd_send", lambda a: calls.sent.append(a.prompt) or 0)
    monkeypatch.setattr(prompt_router, "capture", lambda s, p=0, escapes=False: calls.screen)
    monkeypatch.setattr(prompt_router, "answer",
                        lambda s, p, h, keys: calls.answered.append(keys) or (True, "sent"))
    monkeypatch.setattr(vh, "speak", lambda text, kind="say", display=None: calls.spoken.append(text))

    def run(text):
        return voice_cli.cmd_voice_ask(argparse.Namespace(text=[text], json=True))

    calls.run = run
    return calls


def test_yes_and_no_answer_a_live_approval_instead_of_being_sent(ask):
    ask.screen = APPROVAL_SCREEN
    assert ask.run("Yes, go ahead.") == 0
    assert ask.run("no thanks") == 0
    assert ask.answered == [["1"], ["2"]]
    assert ask.sent == []


def test_anything_else_is_refused_while_an_approval_waits(ask, capsys):
    ask.screen = APPROVAL_SCREEN
    assert ask.run("which sessions are busy") == 1
    assert ask.answered == [] and ask.sent == []
    assert ask.spoken == ["An approval is waiting. Say yes or no."]
    assert json.loads(capsys.readouterr().out)["pending_approval"] is True


def test_with_no_dialog_a_yes_is_just_a_request(ask):
    ask.screen = "❯ \n"
    ask.run("yes")
    assert ask.sent == [["yes"]] and ask.answered == []


def test_spoken_answer_classification():
    cases = {"Yes.": "yes", "yeah do it": "yes", "OK": "yes", "go ahead": "yes",
             "No!": "no", "nope": "no", "don't": "no", "cancel that": "no",
             "yesterday's PRs": None, "know what, check it": None, "": None}
    wrong = {t: (want, voice_cli.spoken_answer(t)) for t, want in cases.items()
             if voice_cli.spoken_answer(t) != want}
    assert not wrong, wrong


def test_no_without_a_no_option_escapes():
    assert voice_cli.approval_keys([{"number": 1, "label": "Yes"}], "no") == ["Escape"]
    assert voice_cli.approval_keys([{"number": 1, "label": "Yes"}, {"number": 2, "label": "Yes, and don't ask again"},
                                    {"number": 3, "label": "No, and tell Claude"}], "no") == ["3"]


# ── hooks drive the chip ────────────────────────────────────────────────


def test_hooks_signal_working_done_failed_and_approval(tmp_path, monkeypatch):
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
    fire("notification", notification_type="permission_prompt", message="needs Bash")
    assert states == ["working", "done", "working", "failed", "approval"]


def test_voice_state_never_fails_the_hook_when_the_portal_is_down(monkeypatch, capsys):
    def down(*a, **k):
        raise ConnectionError("portal down")

    monkeypatch.setattr("agentwire.core.portal_request", down)
    assert voice_cli.cmd_voice_state(argparse.Namespace(state="done", json=True)) == 0
    assert json.loads(capsys.readouterr().out)["delivered"] is False
