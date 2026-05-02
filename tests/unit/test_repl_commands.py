"""Tests for the REPL slash command registry + state tracking.

Phase 2 PR 1. See docs/missions/agentwire-repl.md.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from agentwire.repl.commands import (
    COMMANDS,
    CONTINUE,
    EXIT,
    RESTART,
    RESUME,
    dispatch_command,
)
from agentwire.repl.state import (
    ReplState,
    reset_for_restart,
    track_result,
    track_system_init,
)


def _state(**overrides) -> ReplState:
    defaults = dict(
        mode="bypass",
        model="claude-opus-4-7",
        allowed_tools=["Read", "Bash", "Edit"],
    )
    defaults.update(overrides)
    return ReplState(**defaults)


# --- dispatch ---

class TestDispatch:
    def test_non_slash_returns_none(self):
        state = _state()
        out = io.StringIO()
        assert dispatch_command("hello world", state, out) is None

    def test_unknown_command_continues_with_notice(self):
        state = _state()
        out = io.StringIO()
        action = dispatch_command("/nonexistent", state, out)
        assert action == CONTINUE
        assert "unknown command" in out.getvalue()
        assert "/nonexistent" in out.getvalue()

    @pytest.mark.parametrize("cmd,expected_action", [
        ("/help", CONTINUE),
        ("/exit", EXIT),
        ("/quit", EXIT),  # alias of /exit
        ("/clear", RESTART),
    ])
    def test_dispatch_action(self, cmd, expected_action):
        out = io.StringIO()
        assert dispatch_command(cmd, _state(), out) == expected_action

    def test_args_passed_through(self):
        # No current commands take args, but the dispatcher must not error when
        # a user types extra words. `/help foo bar` should still run /help.
        state = _state()
        out = io.StringIO()
        assert dispatch_command("/help some extra", state, out) == CONTINUE


# --- /help ---

class TestHelp:
    def test_lists_every_command_once(self):
        out = io.StringIO()
        dispatch_command("/help", _state(), out)
        rendered = out.getvalue()
        for name in ("/help", "/clear", "/cost", "/tools", "/model", "/exit"):
            assert name in rendered
        # /quit is an alias, not a row
        assert rendered.count("Exit the REPL") == 1

    def test_shows_aliases(self):
        out = io.StringIO()
        dispatch_command("/help", _state(), out)
        assert "/quit" in out.getvalue()


# --- /cost ---

class TestCost:
    def test_no_turns(self):
        out = io.StringIO()
        dispatch_command("/cost", _state(), out)
        assert "no turns yet" in out.getvalue()

    def test_with_turns(self):
        state = _state()
        state.total_input_tokens = 1000
        state.total_output_tokens = 500
        state.total_cost_usd = 0.025
        state.turn_count = 3
        out = io.StringIO()
        dispatch_command("/cost", state, out)
        rendered = out.getvalue()
        assert "3 turns" in rendered
        assert "1000+500=1500 tok" in rendered
        assert "$0.0250" in rendered

    def test_singular_turn(self):
        state = _state()
        state.turn_count = 1
        state.total_input_tokens = 10
        state.total_output_tokens = 5
        out = io.StringIO()
        dispatch_command("/cost", state, out)
        assert "1 turn " in out.getvalue()  # singular, with trailing space


# --- /tools ---

class TestTools:
    def test_shows_list(self):
        out = io.StringIO()
        dispatch_command("/tools", _state(allowed_tools=["Read", "Bash"]), out)
        rendered = out.getvalue()
        assert "Read" in rendered
        assert "Bash" in rendered
        assert "mode=bypass" in rendered

    def test_empty(self):
        out = io.StringIO()
        dispatch_command("/tools", _state(allowed_tools=[]), out)
        assert "no tools allowed" in out.getvalue()

    def test_restricted_mode(self):
        out = io.StringIO()
        state = _state(mode="restricted", allowed_tools=["Read", "Grep"])
        dispatch_command("/tools", state, out)
        assert "mode=restricted" in out.getvalue()

    def test_mcp_attachment_called_out(self):
        out = io.StringIO()
        dispatch_command("/tools", _state(allowed_tools=["Read", "mcp__agentwire"]), out)
        rendered = out.getvalue()
        assert "MCP server attached" in rendered

    def test_mcp_not_present_no_extra_line(self):
        out = io.StringIO()
        dispatch_command("/tools", _state(allowed_tools=["Read", "Bash"]), out)
        assert "MCP server attached" not in out.getvalue()


# --- /model ---

class TestModel:
    def test_shows_model_and_mode(self):
        out = io.StringIO()
        state = _state(model="claude-opus-4-7", mode="bypass")
        state.session_id = "abc12345def"
        dispatch_command("/model", state, out)
        rendered = out.getvalue()
        assert "claude-opus-4-7" in rendered
        assert "bypass" in rendered
        assert "abc12345" in rendered  # 8-char truncation

    def test_no_session_yet(self):
        out = io.StringIO()
        dispatch_command("/model", _state(), out)
        assert "not yet started" in out.getvalue()


# --- /clear state reset ---

class TestClearSemantics:
    def test_clear_returns_restart(self):
        out = io.StringIO()
        state = _state()
        state.total_input_tokens = 100
        state.turn_count = 5
        action = dispatch_command("/clear", state, out)
        assert action == RESTART
        # /clear itself doesn't zero the state — the outer loop does that
        # via reset_for_restart(). Separation keeps the handler pure.
        assert state.total_input_tokens == 100
        assert state.turn_count == 5


# --- ReplState trackers ---

class TestTrackSystemInit:
    def test_captures_session_id(self):
        state = _state()
        msg = SimpleNamespace(data={"session_id": "sess-xyz"})
        track_system_init(state, msg)
        assert state.session_id == "sess-xyz"

    def test_camelcase_session_id(self):
        state = _state()
        msg = SimpleNamespace(data={"sessionId": "camel-id"})
        track_system_init(state, msg)
        assert state.session_id == "camel-id"

    def test_missing_data_noop(self):
        state = _state()
        msg = SimpleNamespace(data=None)
        track_system_init(state, msg)
        assert state.session_id is None


class TestTrackResult:
    def test_accumulates_tokens(self):
        state = _state()
        m1 = SimpleNamespace(usage={"input_tokens": 10, "output_tokens": 5}, total_cost_usd=0.01)
        m2 = SimpleNamespace(usage={"input_tokens": 20, "output_tokens": 15}, total_cost_usd=0.02)
        track_result(state, m1)
        track_result(state, m2)
        assert state.total_input_tokens == 30
        assert state.total_output_tokens == 20
        assert state.total_cost_usd == pytest.approx(0.03)
        assert state.turn_count == 2

    def test_missing_cost_ignored(self):
        state = _state()
        m = SimpleNamespace(usage={"input_tokens": 10, "output_tokens": 5}, total_cost_usd=None)
        track_result(state, m)
        assert state.total_cost_usd == 0.0
        assert state.turn_count == 1

    def test_non_numeric_cost_ignored(self):
        state = _state()
        m = SimpleNamespace(usage={}, total_cost_usd="not a number")
        track_result(state, m)
        assert state.total_cost_usd == 0.0

    def test_missing_usage_still_increments_turn(self):
        state = _state()
        m = SimpleNamespace(usage=None, total_cost_usd=None)
        track_result(state, m)
        assert state.turn_count == 1


class TestSave:
    def test_no_session_dir(self):
        out = io.StringIO()
        dispatch_command("/save", _state(), out)
        assert "no transcript dir yet" in out.getvalue()

    def test_with_session_dir_and_session_id(self):
        out = io.StringIO()
        state = _state()
        state.session_dir = "/tmp/foo"
        state.transcript_name = "s-001"
        state.turn_count = 4
        state.session_id = "sdk-xyz"
        dispatch_command("/save", state, out)
        rendered = out.getvalue()
        assert "s-001" in rendered
        assert "4 turns" in rendered
        assert "/tmp/foo" in rendered
        assert "/resume s-001" in rendered

    def test_singular_turn(self):
        out = io.StringIO()
        state = _state()
        state.session_dir = "/tmp/x"
        state.transcript_name = "s"
        state.turn_count = 1
        dispatch_command("/save", state, out)
        assert "1 turn]" in out.getvalue()
        assert "1 turns" not in out.getvalue()


class TestResume:
    def test_no_sessions_found(self, tmp_path, monkeypatch):
        from agentwire.repl import persistence
        monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "empty")
        out = io.StringIO()
        action = dispatch_command("/resume", _state(), out)
        assert action == CONTINUE
        assert "no saved sessions" in out.getvalue()

    def test_lists_available(self, tmp_path, monkeypatch):
        from agentwire.repl import persistence
        home = tmp_path / "repl"
        monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", home)
        t = persistence.create_session(
            mode="bypass", model="m", allowed_tools=[],
            name="alpha", home=home,
        )
        t.close()

        out = io.StringIO()
        action = dispatch_command("/resume", _state(), out)
        assert action == CONTINUE
        rendered = out.getvalue()
        assert "alpha" in rendered
        assert "Usage: /resume" in rendered

    def test_missing_named_session(self, tmp_path, monkeypatch):
        from agentwire.repl import persistence
        monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "empty")
        out = io.StringIO()
        action = dispatch_command("/resume not-there", _state(), out)
        assert action == CONTINUE
        assert "no session found" in out.getvalue()

    def test_session_without_sdk_id(self, tmp_path, monkeypatch):
        from agentwire.repl import persistence
        home = tmp_path / "repl"
        monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", home)
        t = persistence.create_session(
            mode="bypass", model="m", allowed_tools=[],
            name="no-id", home=home,
        )
        t.close()

        out = io.StringIO()
        action = dispatch_command("/resume no-id", _state(), out)
        assert action == CONTINUE  # not RESUME, since nothing to resume to
        assert "no recorded sdk_session_id" in out.getvalue()

    def test_resume_sets_pending_and_returns_resume(self, tmp_path, monkeypatch):
        from agentwire.repl import persistence
        home = tmp_path / "repl"
        monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", home)
        t = persistence.create_session(
            mode="bypass", model="m", allowed_tools=[],
            name="good", home=home,
        )
        persistence.record_session_id(t, "sdk-target-id")
        t.close()

        state = _state()
        out = io.StringIO()
        action = dispatch_command("/resume good", state, out)
        assert action == RESUME
        assert state.pending_resume_sdk_session_id == "sdk-target-id"
        assert "resuming good" in out.getvalue()


class TestEffort:
    def test_show_default(self):
        state = _state()
        out = io.StringIO()
        action = dispatch_command("/effort", state, out)
        assert action == CONTINUE
        assert f"effort={state.effort}" in out.getvalue()

    def test_set_changes_state_and_restarts(self):
        state = _state()
        out = io.StringIO()
        action = dispatch_command("/effort low", state, out)
        assert action == RESTART
        assert state.effort == "low"

    def test_unknown_value_continues(self):
        state = _state()
        out = io.StringIO()
        action = dispatch_command("/effort wat", state, out)
        assert action == CONTINUE
        assert "unknown effort" in out.getvalue()
        assert state.effort == "high"  # unchanged

    def test_same_value_noop_continues(self):
        state = _state()
        state.effort = "max"
        out = io.StringIO()
        action = dispatch_command("/effort max", state, out)
        assert action == CONTINUE
        assert "already" in out.getvalue()


class TestSay:
    def test_no_args_shows_usage(self):
        out = io.StringIO()
        action = dispatch_command("/say", _state(), out)
        assert action == CONTINUE
        assert "Usage" in out.getvalue()

    def test_voice_in_no_args_when_set(self):
        out = io.StringIO()
        s = _state()
        s.voice = "alice"
        dispatch_command("/say", s, out)
        assert "voice=alice" in out.getvalue()

    def test_invokes_subprocess(self, monkeypatch):
        captured = {}

        class FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return FakeProc()

        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)

        out = io.StringIO()
        s = _state()
        s.voice = "alice"
        action = dispatch_command("/say hello world", s, out)
        assert action == CONTINUE
        assert captured["cmd"][:2] == ["agentwire", "say"]
        assert "--voice" in captured["cmd"]
        assert "alice" in captured["cmd"]
        assert "hello world" in captured["cmd"]
        assert "[said" in out.getvalue()

    def test_no_voice_omits_voice_flag(self, monkeypatch):
        captured = {}

        class FakeProc:
            returncode = 0

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return FakeProc()

        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)

        out = io.StringIO()
        action = dispatch_command("/say hi", _state(), out)
        assert "--voice" not in captured["cmd"]

    def test_subprocess_error(self, monkeypatch):
        class FakeProc:
            returncode = 1
            stdout = ""
            stderr = "tts unreachable"

        import subprocess
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProc())

        out = io.StringIO()
        dispatch_command("/say bad", _state(), out)
        assert "say failed" in out.getvalue()
        assert "tts unreachable" in out.getvalue()

    def test_agentwire_not_on_path(self, monkeypatch):
        import subprocess
        def raise_fnf(*a, **kw):
            raise FileNotFoundError("no agentwire")
        monkeypatch.setattr(subprocess, "run", raise_fnf)

        out = io.StringIO()
        dispatch_command("/say hi", _state(), out)
        assert "not found" in out.getvalue()


class TestRunWorkflow:
    def test_no_args_shows_usage(self):
        out = io.StringIO()
        action = dispatch_command("/run-workflow", _state(), out)
        assert action == CONTINUE
        assert "Usage" in out.getvalue()

    def test_invokes_subprocess(self, monkeypatch):
        captured = {}

        class FakeProc:
            returncode = 0

            def __init__(self):
                self.stdout = iter(["hello\n", "world\n"])

            def wait(self):
                return 0

        def fake_popen(cmd, **kw):
            captured["cmd"] = cmd
            return FakeProc()

        import subprocess
        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        out = io.StringIO()
        action = dispatch_command("/run-workflow my-flow --runner anthropic", _state(), out)
        assert action == CONTINUE
        assert captured["cmd"] == ["agentwire", "workflow", "run", "my-flow", "--runner", "anthropic"]
        rendered = out.getvalue()
        assert "hello" in rendered
        assert "world" in rendered
        assert "exited with code 0" in rendered


class TestThinking:
    def test_show_default(self):
        state = _state()
        out = io.StringIO()
        action = dispatch_command("/thinking", state, out)
        assert action == CONTINUE
        assert f"thinking={state.thinking_mode}" in out.getvalue()

    def test_summarized_restarts(self):
        state = _state()
        out = io.StringIO()
        action = dispatch_command("/thinking summarized", state, out)
        assert action == RESTART
        assert state.thinking_mode == "summarized"

    def test_off_restarts(self):
        state = _state()
        out = io.StringIO()
        action = dispatch_command("/thinking off", state, out)
        assert action == RESTART
        assert state.thinking_mode == "off"

    def test_unknown_value_continues(self):
        state = _state()
        out = io.StringIO()
        action = dispatch_command("/thinking weird", state, out)
        assert action == CONTINUE
        assert "unknown thinking" in out.getvalue()


class TestResetForRestart:
    def test_zeros_counters_preserves_config(self):
        state = _state()
        state.total_input_tokens = 100
        state.total_output_tokens = 50
        state.total_cost_usd = 0.5
        state.turn_count = 4
        state.session_id = "old"
        reset_for_restart(state)
        assert state.total_input_tokens == 0
        assert state.total_output_tokens == 0
        assert state.total_cost_usd == 0.0
        assert state.turn_count == 0
        assert state.session_id is None
        assert state.restart_count == 1
        # Config preserved
        assert state.mode == "bypass"
        assert state.model == "claude-opus-4-7"
        assert state.allowed_tools == ["Read", "Bash", "Edit"]

    def test_multiple_restarts_increment(self):
        state = _state()
        reset_for_restart(state)
        reset_for_restart(state)
        reset_for_restart(state)
        assert state.restart_count == 3

    def test_clears_always_allow(self):
        state = _state()
        state.always_allow_tools = {"Read", "Bash"}
        reset_for_restart(state)
        assert state.always_allow_tools == set()
