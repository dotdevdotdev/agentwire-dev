"""Tests for pure functions and dataclasses in agentwire/server.py."""

import asyncio

from agentwire.server import PendingPermission, SessionConfig, _is_allowed_in_restricted_mode

# ---------------------------------------------------------------------------
# _is_allowed_in_restricted_mode
# ---------------------------------------------------------------------------


class TestIsAllowedInRestrictedMode:
    """Security boundary — regex-based command filter."""

    def test_ask_user_question_allowed(self):
        assert _is_allowed_in_restricted_mode("AskUserQuestion", {}) is True

    def test_non_bash_tool_rejected(self):
        assert _is_allowed_in_restricted_mode("Read", {}) is False
        assert _is_allowed_in_restricted_mode("Edit", {}) is False
        assert _is_allowed_in_restricted_mode("Write", {}) is False

    def test_say_double_quotes(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'say "hello"'}) is True

    def test_say_single_quotes(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": "say 'hello world'"}) is True

    def test_agentwire_say(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'agentwire say "hello"'}) is True

    def test_agentwire_say_with_session(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'agentwire say -s session "hello"'}) is True

    def test_say_background(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'say "hello" &'}) is True

    def test_say_with_voice_flag(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'say -v alice "hello"'}) is True

    def test_say_chain_rejected(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'say "hi" && rm -rf /'}) is False

    def test_say_redirect_rejected(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'say "hi" > /tmp/log'}) is False

    def test_say_command_substitution_rejected(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'say $(cat /etc/passwd)'}) is False

    def test_multiline_rejected(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'say "hi"\nrm -rf /'}) is False

    def test_empty_command_rejected(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": ""}) is False

    def test_echo_rejected(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'echo hello'}) is False

    def test_no_quotes_rejected(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": "say hello"}) is False

    def test_missing_command_key(self):
        assert _is_allowed_in_restricted_mode("Bash", {}) is False

    def test_say_pipe_rejected(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": 'say "hi" | tee /tmp/x'}) is False

    def test_whitespace_stripped(self):
        assert _is_allowed_in_restricted_mode("Bash", {"command": '  say "hello"  '}) is True


# ---------------------------------------------------------------------------
# SessionConfig
# ---------------------------------------------------------------------------


class TestSessionConfig:
    def test_defaults(self):
        cfg = SessionConfig()
        assert cfg.voice == "default"
        assert cfg.type == "claude-bypass"
        assert cfg.roles == []

    def test_roles_none_to_empty_list(self):
        cfg = SessionConfig(roles=None)
        assert cfg.roles == []

    def test_custom_values(self):
        cfg = SessionConfig(
            voice="alice",
            type="bare",
            roles=["voice", "worker"],
            machine="gpu-box",
        )
        assert cfg.voice == "alice"
        assert cfg.type == "bare"
        assert cfg.roles == ["voice", "worker"]
        assert cfg.machine == "gpu-box"


# ---------------------------------------------------------------------------
# PendingPermission
# ---------------------------------------------------------------------------


class TestPendingPermission:
    def test_defaults(self):
        pp = PendingPermission(request={"tool": "Bash"})
        assert isinstance(pp.event, asyncio.Event)
        assert pp.decision is None
        assert pp.request == {"tool": "Bash"}

    def test_event_not_set(self):
        pp = PendingPermission(request={})
        assert not pp.event.is_set()
