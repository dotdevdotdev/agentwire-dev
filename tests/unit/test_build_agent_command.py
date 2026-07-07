"""Tests for __main__.py — build_agent_command for each session type."""

import os

import pytest

from agentwire.roles import RoleConfig


class TestBuildAgentCommand:
    def _build(self, session_type, roles=None, model=None):
        from agentwire.__main__ import build_agent_command
        return build_agent_command(session_type, roles=roles, model=model)

    def test_bare_empty_command(self):
        cmd = self._build("bare")
        assert cmd.command == ""
        assert cmd.temp_file is None

    def test_claude_bypass(self):
        cmd = self._build("claude-bypass")
        assert "claude" in cmd.command
        assert "--dangerously-skip-permissions" in cmd.command

    def test_claude_prompted(self):
        cmd = self._build("claude-prompted")
        assert "claude" in cmd.command
        assert "--dangerously-skip-permissions" not in cmd.command
        assert "--tools" not in cmd.command

    def test_claude_restricted(self):
        cmd = self._build("claude-restricted")
        assert "claude" in cmd.command
        assert "--tools Bash" in cmd.command

    def test_with_model_override(self):
        cmd = self._build("claude-bypass", model="haiku")
        assert "--model haiku" in cmd.command

    def test_with_roles_tools(self):
        roles = [RoleConfig(name="test", tools=["Bash", "Read"])]
        cmd = self._build("claude-bypass", roles=roles)
        assert "--tools" in cmd.command
        assert "Bash" in cmd.command

    def test_with_roles_instructions(self):
        roles = [RoleConfig(name="test", instructions="Be helpful")]
        cmd = self._build("claude-bypass", roles=roles)
        assert "--append-system-prompt" in cmd.command
        assert cmd.temp_file is not None
        if cmd.temp_file:
            os.unlink(cmd.temp_file)

    def test_restricted_ignores_role_flags(self):
        """claude-restricted should not get role tools/instructions."""
        roles = [RoleConfig(name="test", tools=["Read"], instructions="Hello")]
        cmd = self._build("claude-restricted", roles=roles)
        assert "--append-system-prompt" not in cmd.command

    def test_unknown_type_empty(self):
        cmd = self._build("nonexistent-type")
        assert cmd.command == ""


class TestSessionEnvInjection:
    def test_build_tmux_env_flags_empty(self):
        from agentwire.__main__ import _build_tmux_env_flags
        assert _build_tmux_env_flags({}) == []

    def test_build_tmux_env_flags_pairs(self):
        from agentwire.__main__ import _build_tmux_env_flags
        flags = _build_tmux_env_flags({"SVC_API_KEY": "abc", "FOO": "bar"})
        # Each var becomes two list entries: "-e" and "K=V"
        assert flags.count("-e") == 2
        assert "SVC_API_KEY=abc" in flags
        assert "FOO=bar" in flags

    def test_build_tmux_env_flags_shell_empty(self):
        from agentwire.__main__ import _build_tmux_env_flags_shell
        assert _build_tmux_env_flags_shell({}) == ""

    def test_build_tmux_env_flags_shell_quoted(self):
        from agentwire.__main__ import _build_tmux_env_flags_shell
        frag = _build_tmux_env_flags_shell({"SVC_API_KEY": "abc 123"})
        # Trailing space so it splices into the middle of a command string
        assert frag.endswith(" ")
        assert "-e" in frag
        # Value with spaces must be shell-quoted as a single -e argument
        assert "'SVC_API_KEY=abc 123'" in frag

    def test_build_tmux_env_flags_shell_multiple(self):
        from agentwire.__main__ import _build_tmux_env_flags_shell
        frag = _build_tmux_env_flags_shell({"A": "1", "B": "2"})
        assert frag.count("-e") == 2
        assert "A=1" in frag
        assert "B=2" in frag


class TestParseEnvArgs:
    def test_none_returns_empty(self):
        from agentwire.__main__ import parse_env_args
        assert parse_env_args(None) == {}
        assert parse_env_args([]) == {}

    def test_single_pair(self):
        from agentwire.__main__ import parse_env_args
        assert parse_env_args(["FOO=bar"]) == {"FOO": "bar"}

    def test_multiple_pairs(self):
        from agentwire.__main__ import parse_env_args
        result = parse_env_args(["A=1", "B=2", "C=3"])
        assert result == {"A": "1", "B": "2", "C": "3"}

    def test_value_with_equals_sign_preserved(self):
        from agentwire.__main__ import parse_env_args
        # Values can contain `=` (e.g. base64 payloads) — only split on the first.
        assert parse_env_args(["TOKEN=abc=def=xyz"]) == {"TOKEN": "abc=def=xyz"}

    def test_empty_value_allowed(self):
        from agentwire.__main__ import parse_env_args
        assert parse_env_args(["DEBUG="]) == {"DEBUG": ""}

    def test_missing_equals_exits(self):
        from agentwire.__main__ import parse_env_args
        with pytest.raises(SystemExit):
            parse_env_args(["BROKEN"])

    def test_empty_key_exits(self):
        from agentwire.__main__ import parse_env_args
        with pytest.raises(SystemExit):
            parse_env_args(["=value"])

    def test_later_value_wins(self):
        from agentwire.__main__ import parse_env_args
        # If the same key appears twice, last one wins (standard dict semantics).
        assert parse_env_args(["K=1", "K=2"]) == {"K": "2"}
