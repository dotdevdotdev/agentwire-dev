"""Tests for sdk-* session types + build_agent_command dispatch + REPL scaffold.

Covers the session-type plumbing end-to-end.
"""

from __future__ import annotations

import pytest

from agentwire.project_config import SessionType, normalize_session_type
from agentwire.__main__ import build_agent_command


# --- SessionType enum round-trip ---

class TestSdkSessionTypes:
    @pytest.mark.parametrize("value,member", [
        ("sdk-bypass", SessionType.SDK_BYPASS),
        ("sdk-prompted", SessionType.SDK_PROMPTED),
        ("sdk-restricted", SessionType.SDK_RESTRICTED),
    ])
    def test_from_str(self, value, member):
        assert SessionType.from_str(value) == member

    def test_from_str_case_insensitive(self):
        assert SessionType.from_str("SDK-BYPASS") == SessionType.SDK_BYPASS

    def test_from_str_underscore_to_hyphen(self):
        assert SessionType.from_str("sdk_bypass") == SessionType.SDK_BYPASS

    def test_to_cli_flags_empty(self):
        # sdk-* types don't produce Claude CLI flags (they spawn `agentwire repl`).
        # to_cli_flags is Claude-specific; sdk-* returns empty to avoid collision.
        assert SessionType.SDK_BYPASS.to_cli_flags() == []
        assert SessionType.SDK_PROMPTED.to_cli_flags() == []
        assert SessionType.SDK_RESTRICTED.to_cli_flags() == []


# --- normalize_session_type passthrough ---

class TestNormalizeSdk:
    @pytest.mark.parametrize("sdk_type", [
        "sdk-bypass", "sdk-prompted", "sdk-restricted",
    ])
    def test_passthrough(self, sdk_type):
        assert normalize_session_type(sdk_type, "claude") == sdk_type


# --- build_agent_command dispatch ---

class TestBuildAgentCommandSdk:
    def test_bypass(self):
        cmd = build_agent_command("sdk-bypass")
        assert cmd.command == "agentwire repl --mode bypass"
        assert cmd.temp_file is None
        assert cmd.env == {}

    def test_prompted(self):
        cmd = build_agent_command("sdk-prompted")
        assert cmd.command == "agentwire repl --mode prompted"

    def test_restricted(self):
        cmd = build_agent_command("sdk-restricted")
        assert cmd.command == "agentwire repl --mode restricted"

    def test_with_model(self):
        cmd = build_agent_command("sdk-bypass", model="claude-opus-4-7")
        assert cmd.command == "agentwire repl --mode bypass --model claude-opus-4-7"

    def test_with_role_instructions_appends_system_prompt(self, tmp_path):
        # merge_roles is called inside build_agent_command; we simulate a role
        # with instructions to exercise the temp-file path.
        from agentwire.roles import RoleConfig
        role = RoleConfig(
            name="test",
            description="t",
            tools=[],
            disallowed_tools=[],
            instructions="you are a test role",
            color=None,
        )
        cmd = build_agent_command("sdk-bypass", roles=[role])
        assert cmd.command.startswith("agentwire repl --mode bypass")
        assert "--append-system-prompt" in cmd.command
        assert cmd.temp_file is not None
        # cleanup
        import os
        if cmd.temp_file and os.path.exists(cmd.temp_file):
            os.unlink(cmd.temp_file)


# --- REPL interactive loop ---
# The Textual REPL is tested in test_repl_textual_app.py. This file covers
# the session-type + build_agent_command dispatch surface.
