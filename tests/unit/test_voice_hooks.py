"""Hook-enforced speech for the voice assistant (#1198, slice 2).

The guarantees: every request gets an instant ack, every turn ends with
exactly one spoken result (the model's own `say`, or the hooks' fallback),
failures and approval requests are spoken, and none of it reaches any other
session.
"""

import io
import json
import shlex

import pytest

from agentwire import voice_hooks as vh
from agentwire.roles import RoleConfig


@pytest.fixture
def spoken(tmp_path, monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(vh, "STATE_FILE", tmp_path / "turn.json")
    monkeypatch.setattr(vh, "speak", lambda text, kind="say", display=None: said.append(text))
    monkeypatch.setattr(vh, "signal", lambda state: None)
    return said


def fire(event: str, **payload) -> int:
    import sys

    stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        return vh.main([event])
    finally:
        sys.stdin = stdin


def _settings_from(command: str) -> dict | None:
    if "--settings " not in command:
        return None
    return json.loads(shlex.split(command.split("--settings ", 1)[1])[0])


class TestScope:
    @pytest.fixture(autouse=True)
    def _prompts(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agentwire.core.CONFIG_DIR", tmp_path)

    def test_only_the_voice_assistant_launch_carries_the_hooks(self):
        from agentwire.core import build_agent_command

        role = RoleConfig(name="voice-assistant", instructions="x")
        settings = _settings_from(build_agent_command("bypass", [role]).command)
        assert set(settings["hooks"]) == {
            "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "StopFailure", "Notification",
        }
        assert settings["hooks"]["PostToolUse"][0]["matcher"] == "mcp__agentwire__say"
        assert all("-m agentwire.voice_hooks" in e["hooks"][0]["command"]
                   for entries in settings["hooks"].values() for e in entries)

        others = [RoleConfig(name="orchestrator", instructions="x"), RoleConfig(name="soul")]
        assert _settings_from(build_agent_command("bypass", others).command) is None
        assert _settings_from(build_agent_command("bypass").command) is None

    def test_the_append_system_prompt_flag_stays_last(self):
        from agentwire.core import build_agent_command

        cmd = build_agent_command("bypass", [RoleConfig(name="voice-assistant", instructions="x")]).command
        assert cmd.index("--settings") < cmd.index("--append-system-prompt")
        assert cmd.rstrip().endswith('.txt)"')


class TestAck:
    def test_a_request_is_acked_instantly(self, spoken):
        fire("prompt-submit", prompt="which sessions are busy", prompt_id="p1")
        assert spoken == ["On it."]

    def test_slash_commands_and_msg_deliveries_get_no_ack(self, spoken):
        for prompt in ("/compact", "[MSG from worker-x · done] PR is up  ⟨#ab12⟩"):
            fire("prompt-submit", prompt=prompt, prompt_id="p")
        assert spoken == []


class TestReport:
    def test_a_silent_turn_gets_its_answer_spoken(self, spoken):
        fire("prompt-submit", prompt="how many sessions", prompt_id="p1")
        fire("stop", prompt_id="p1", last_assistant_message="Four sessions are running. Details below.")
        assert spoken == ["On it.", "Four sessions are running. Details below."]

    def test_a_turn_that_said_its_result_is_not_spoken_twice(self, spoken):
        fire("prompt-submit", prompt="how many sessions", prompt_id="p1")
        fire("said", prompt_id="p1", tool_name="mcp__agentwire__say")
        fire("stop", prompt_id="p1", last_assistant_message="Four.")
        fire("stop", prompt_id="p1", last_assistant_message="Four.")
        assert spoken == ["On it."]

    def test_a_say_from_an_earlier_turn_does_not_silence_this_one(self, spoken):
        fire("prompt-submit", prompt="a", prompt_id="p1")
        fire("said", prompt_id="p1")
        fire("prompt-submit", prompt="b", prompt_id="p2")
        fire("stop", prompt_id="p2", last_assistant_message="Second answer.")
        assert spoken[-1] == "Second answer."

    def test_an_empty_final_message_still_reports(self, spoken):
        fire("stop", prompt_id="p1", last_assistant_message="")
        assert spoken == ["Done."]

    def test_failures_are_spoken_with_the_reason_once(self, spoken):
        fire("prompt-submit", prompt="x", prompt_id="p1")
        fire("stop-failure", prompt_id="p1", error="rate_limit")
        fire("stop", prompt_id="p1", last_assistant_message="")
        fire("stop-failure", prompt_id="p2", error="weird_new_error")
        assert spoken == ["On it.", "That failed: I hit a usage limit.",
                          "That failed: weird new error."]


class TestApprovals:
    def test_approval_requests_go_to_the_grant_check_others_are_ignored(self, spoken, monkeypatch):
        spawned = []
        monkeypatch.setattr(vh, "_spawn", spawned.append)
        monkeypatch.setattr(vh, "APPROVAL_FILE", vh.STATE_FILE.with_name("approval.json"))
        fire("notification", notification_type="idle_prompt", message="Claude is waiting")
        fire("notification", notification_type="permission_prompt",
             message="Claude needs your permission to use Bash")
        assert spawned == [["voice", "approval", "--announce",
                            "Claude needs your permission to use Bash. Yes or no?"]]
        assert spoken == []  # `voice approval` speaks: the grant, or the request


def test_a_hook_never_fails_the_turn(spoken, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert vh.main(["stop"]) == 0
    assert vh.main(["no-such-event"]) == 0
    assert spoken == []


class TestBrevity:
    def test_a_long_report_speaks_a_headline_and_shows_the_rest(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(vh, "STATE_FILE", tmp_path / "turn.json")
        monkeypatch.setattr(vh, "signal", lambda state: None)
        monkeypatch.setattr(vh, "speak", lambda text, kind="say", display=None:
                            calls.append((text, kind, display)))
        long = ("Four sessions are running. The portal and the scheduler are busy, "
                "the minecraft session is idle, and the voice worktree is waiting on review.")
        fire("stop", prompt_id="p", last_assistant_message=long)
        fire("stop-failure", prompt_id="q", error="rate_limit")
        fire("prompt-submit", prompt="hi", prompt_id="r")

        (text, kind, display), failure, ack = calls
        assert (text, kind, display) == ("Four sessions are running.", "report", long)
        assert (failure[1], ack[1]) == ("failure", "ack")
        assert all(len(c[0].split()) <= vh.MAX_SPOKEN_WORDS for c in calls)

    def test_a_short_report_is_spoken_whole_with_nothing_on_screen(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(vh, "STATE_FILE", tmp_path / "turn.json")
        monkeypatch.setattr(vh, "signal", lambda state: None)
        monkeypatch.setattr(vh, "speak", lambda text, kind="say", display=None:
                            calls.append((text, display)))
        fire("stop", prompt_id="p", last_assistant_message="Three busy. One idle.")
        assert calls == [("Three busy. One idle.", None)]

    def test_headline_cuts_at_sentences_then_words(self):
        assert vh.speech_headline("One. Two three. " + "x " * 20) == ("One. Two three.", True)
        text, cut = vh.speech_headline(" ".join(f"w{i}" for i in range(40)))
        assert cut and len(text.split()) == vh.MAX_SPOKEN_WORDS and text.endswith("…")
        assert vh.speech_headline("Short answer.") == ("Short answer.", False)


def test_spoken_summary_is_sayable_and_bounded():
    text = ("Four sessions: `portal` and **scheduler**. See https://example.com/x.\n"
            "```py\nprint(1)\n```\n- one more sentence here. " + "Padding sentence. " * 30)
    out = vh.spoken_summary(text)
    assert out.startswith("Four sessions: portal and scheduler.")
    assert "`" not in out and "*" not in out and "http" not in out and "print" not in out
    assert len(out) <= vh.SUMMARY_LIMIT and out.endswith(".")
