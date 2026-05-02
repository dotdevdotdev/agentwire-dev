"""Tests for __main__.py — build_agent_command for each session type."""

import os
from unittest.mock import patch

import pytest

from agentwire.roles import RoleConfig


# Mock for load_config() in __main__ used by the pi-* branch.
FAKE_CONFIG = {
    "pi": {
        "binary": "pi",
        "providers": {
            "zai": {
                "env_var": "ZAI_API_KEY",
                "api_key": "test-key-123",
                "default_model": "glm-5.1",
            },
            "deepseek": {
                "env_var": "DEEPSEEK_API_KEY",
                "api_key": "test-deepseek-key",
                "default_model": "deepseek-chat",
            },
        },
    },
}


@pytest.fixture(autouse=True)
def mock_main_load_config():
    with patch("agentwire.__main__.load_config", return_value=FAKE_CONFIG):
        yield


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

    # === pi-zai session types ===

    def test_pi_zai_basic(self):
        """pi-zai launches pi binary with Z.AI provider and default model.

        Security regression: key var name AND value must both stay out of
        cmd.command (visible in ps auxwww) — they're injected via tmux env.
        """
        cmd = self._build("pi-zai")
        assert cmd.command.startswith("pi --provider zai")
        assert "ZAI_API_KEY" not in cmd.command
        assert "test-key-123" not in cmd.command  # actual key value too
        assert cmd.env == {"ZAI_API_KEY": "test-key-123"}
        assert "--model glm-5.1" in cmd.command
        # Pi has no --dangerously-skip-permissions (no permission system)
        assert "--dangerously-skip-permissions" not in cmd.command
        assert cmd.temp_file is None

    def test_pi_zai_restricted(self):
        """pi-zai-restricted whitelists read-only tools + bash."""
        cmd = self._build("pi-zai-restricted")
        assert "pi --provider zai" in cmd.command
        assert "--tools read,grep,find,bash" in cmd.command

    def test_pi_zai_readonly(self):
        """pi-zai-readonly has no bash, no edits — pure inspection."""
        cmd = self._build("pi-zai-readonly")
        assert "pi --provider zai" in cmd.command
        assert "--tools read,grep,find" in cmd.command
        # No bash, no edit, no write
        assert "bash" not in cmd.command.split("--tools")[1]
        assert "edit" not in cmd.command.split("--tools")[1]
        assert "write" not in cmd.command.split("--tools")[1]

    def test_pi_zai_model_override(self):
        cmd = self._build("pi-zai", model="glm-4.7-flash")
        assert "--model glm-4.7-flash" in cmd.command
        # Default should not also appear
        assert "--model glm-5.1 " not in cmd.command

    def test_pi_zai_with_role_instructions(self):
        """pi-zai with role.instructions uses --append-system-prompt."""
        roles = [RoleConfig(name="worker", instructions="You are a worker agent.")]
        cmd = self._build("pi-zai", roles=roles)
        assert "--append-system-prompt" in cmd.command
        assert cmd.temp_file is not None
        with open(cmd.temp_file) as f:
            content = f.read()
        assert "You are a worker agent." in content
        os.unlink(cmd.temp_file)

    def test_pi_zai_with_role_tools(self):
        """pi-zai translates Claude tool names (CamelCase) to pi's lowercase."""
        roles = [RoleConfig(name="test", tools=["Read", "Bash", "Edit"])]
        cmd = self._build("pi-zai", roles=roles)
        assert "--tools" in cmd.command
        # Extracted tool list section
        tools_section = cmd.command.split("--tools")[1].split()[0]
        assert "read" in tools_section
        assert "bash" in tools_section
        assert "edit" in tools_section

    def test_pi_zai_filters_unknown_tools(self):
        """pi-zai drops tool names pi doesn't support (e.g., Glob, WebFetch)."""
        roles = [RoleConfig(name="test", tools=["Read", "Glob", "WebFetch", "Bash"])]
        cmd = self._build("pi-zai", roles=roles)
        tools_section = cmd.command.split("--tools")[1].split()[0]
        # Glob → find (but only if we translated; current impl filters out unknowns)
        # WebFetch → not supported by pi
        assert "webfetch" not in tools_section.lower()
        # Read and Bash are valid
        assert "read" in tools_section
        assert "bash" in tools_section

    def test_pi_zai_restricted_ignores_role_tools(self):
        """pi-zai-restricted keeps its curated tool list regardless of roles."""
        roles = [RoleConfig(name="test", tools=["Edit", "Write"])]
        cmd = self._build("pi-zai-restricted", roles=roles)
        # Should have restricted's tool list, not role's
        assert "--tools read,grep,find,bash" in cmd.command


    def test_pi_zai_readonly_ignores_role_instructions(self):
        """pi-zai-readonly is a curated context, skips role instructions."""
        roles = [RoleConfig(name="test", instructions="Be creative")]
        cmd = self._build("pi-zai-readonly", roles=roles)
        assert "--append-system-prompt" not in cmd.command
        assert cmd.temp_file is None

    # === pi-<provider> generalization (multi-provider) ===

    def test_pi_deepseek_basic(self):
        """pi-deepseek launches pi binary with deepseek provider + key."""
        cmd = self._build("pi-deepseek")
        assert cmd.command.startswith("pi --provider deepseek")
        assert "--model deepseek-chat" in cmd.command
        # Deepseek key under DEEPSEEK_API_KEY, never on the command line
        assert "test-deepseek-key" not in cmd.command
        assert cmd.env == {"DEEPSEEK_API_KEY": "test-deepseek-key"}

    def test_pi_deepseek_restricted(self):
        """pi-<provider>-restricted parses correctly for any provider."""
        cmd = self._build("pi-deepseek-restricted")
        assert "pi --provider deepseek" in cmd.command
        assert "--tools read,grep,find,bash" in cmd.command

    def test_pi_unknown_provider_raises(self):
        """Provider not in pi.providers raises a clear error at build time."""
        with pytest.raises(ValueError, match="pi provider 'bogus'"):
            self._build("pi-bogus")

    def test_pi_extra_env_merged_with_provider_key(self):
        """pi.extra_env (e.g. BRAVE_SEARCH_API_KEY) merges with the provider key."""
        config = {
            "pi": {
                "binary": "pi",
                "extra_env": {"BRAVE_SEARCH_API_KEY": "test-brave"},
                "providers": {
                    "zai": {
                        "env_var": "ZAI_API_KEY",
                        "api_key": "test-key-123",
                        "default_model": "glm-5.1",
                    },
                },
            },
        }
        with patch("agentwire.__main__.load_config", return_value=config):
            cmd = self._build("pi-zai")
        assert cmd.env["ZAI_API_KEY"] == "test-key-123"
        assert cmd.env["BRAVE_SEARCH_API_KEY"] == "test-brave"

    def test_pi_system_prompt_combined_with_role(self):
        """pi.system_prompt is prepended to role instructions in the temp file."""
        config = {
            "pi": {
                "binary": "pi",
                "system_prompt": "GLOBAL_INSTRUCTIONS",
                "providers": {
                    "zai": {
                        "env_var": "ZAI_API_KEY",
                        "api_key": "test-key-123",
                        "default_model": "glm-5.1",
                    },
                },
            },
        }
        roles = [RoleConfig(name="worker", instructions="ROLE_INSTRUCTIONS")]
        with patch("agentwire.__main__.load_config", return_value=config):
            cmd = self._build("pi-zai", roles=roles)
        assert cmd.temp_file is not None
        with open(cmd.temp_file) as f:
            content = f.read()
        assert "GLOBAL_INSTRUCTIONS" in content
        assert "ROLE_INSTRUCTIONS" in content
        # Global must come first so role can override / extend it
        assert content.index("GLOBAL_INSTRUCTIONS") < content.index("ROLE_INSTRUCTIONS")
        os.unlink(cmd.temp_file)

    def test_pi_system_prompt_alone_writes_temp_file(self):
        """Even with no role, pi.system_prompt alone triggers --append-system-prompt."""
        config = {
            "pi": {
                "binary": "pi",
                "system_prompt": "ONLY_GLOBAL",
                "providers": {
                    "zai": {
                        "env_var": "ZAI_API_KEY",
                        "api_key": "test-key-123",
                        "default_model": "glm-5.1",
                    },
                },
            },
        }
        with patch("agentwire.__main__.load_config", return_value=config):
            cmd = self._build("pi-zai")
        assert cmd.temp_file is not None
        with open(cmd.temp_file) as f:
            assert f.read() == "ONLY_GLOBAL"
        os.unlink(cmd.temp_file)

    def test_pi_restricted_skips_system_prompt(self):
        """Restricted variants are curated — pi.system_prompt is skipped."""
        config = {
            "pi": {
                "binary": "pi",
                "system_prompt": "GLOBAL_INSTRUCTIONS",
                "providers": {
                    "zai": {
                        "env_var": "ZAI_API_KEY",
                        "api_key": "test-key-123",
                        "default_model": "glm-5.1",
                    },
                },
            },
        }
        with patch("agentwire.__main__.load_config", return_value=config):
            cmd = self._build("pi-zai-restricted")
        assert "--append-system-prompt" not in cmd.command
        assert cmd.temp_file is None


class TestSessionEnvInjection:
    def test_build_tmux_env_flags_empty(self):
        from agentwire.__main__ import _build_tmux_env_flags
        assert _build_tmux_env_flags({}) == []

    def test_build_tmux_env_flags_pairs(self):
        from agentwire.__main__ import _build_tmux_env_flags
        flags = _build_tmux_env_flags({"ZAI_API_KEY": "abc", "FOO": "bar"})
        # Each var becomes two list entries: "-e" and "K=V"
        assert flags.count("-e") == 2
        assert "ZAI_API_KEY=abc" in flags
        assert "FOO=bar" in flags

    def test_build_tmux_env_flags_shell_empty(self):
        from agentwire.__main__ import _build_tmux_env_flags_shell
        assert _build_tmux_env_flags_shell({}) == ""

    def test_build_tmux_env_flags_shell_quoted(self):
        from agentwire.__main__ import _build_tmux_env_flags_shell
        frag = _build_tmux_env_flags_shell({"ZAI_API_KEY": "abc 123"})
        # Trailing space so it splices into the middle of a command string
        assert frag.endswith(" ")
        assert "-e" in frag
        # Value with spaces must be shell-quoted as a single -e argument
        assert "'ZAI_API_KEY=abc 123'" in frag

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
