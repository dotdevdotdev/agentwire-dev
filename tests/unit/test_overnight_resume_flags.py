"""Tests for overnight queue resume-flag injection (Phase 5 hardening).

The dispatch path injects `--resume <id> --fork-session` after the agent
binary token. Phase 5 added a session-type guard so sdk-* and pi-*
items don't get malformed claude flags shoved at them, and switched the
locator from rfind("claude") (buggy when `--model claude-opus-4-7` is in
the args) to a startswith check on the leading binary token.
"""

from __future__ import annotations

import pytest

from agentwire.overnight import _inject_resume_flags


class TestClaudeTypes:
    def test_basic_claude_bypass(self):
        cmd = "claude --dangerously-skip-permissions --model claude-opus-4-7"
        out = _inject_resume_flags(cmd, "claude-bypass", "abc123")
        assert out == (
            "claude --resume abc123 --fork-session "
            "--dangerously-skip-permissions --model claude-opus-4-7"
        )

    def test_claude_prompted(self):
        cmd = "claude --model claude-opus-4-7"
        out = _inject_resume_flags(cmd, "claude-prompted", "uuid-x")
        assert "--resume uuid-x --fork-session" in out
        # Resume flags inserted right after `claude`, not inside `claude-opus-4-7`
        assert out.startswith("claude --resume uuid-x --fork-session ")

    def test_no_resume_id_passthrough(self):
        cmd = "claude --model claude-opus-4-7"
        assert _inject_resume_flags(cmd, "claude-bypass", "") == cmd


class TestSdkTypes:
    @pytest.mark.parametrize("sdk_type", ["sdk-bypass", "sdk-prompted", "sdk-restricted"])
    def test_sdk_passthrough_even_with_resume_id(self, sdk_type):
        # SDK has its own --resume NAME convention; the claude UUID isn't usable.
        cmd = "agentwire repl --mode bypass --model claude-opus-4-7"
        out = _inject_resume_flags(cmd, sdk_type, "abc123")
        assert out == cmd
        assert "--fork-session" not in out

    def test_sdk_with_no_resume_id_passthrough(self):
        cmd = "agentwire repl --mode bypass"
        assert _inject_resume_flags(cmd, "sdk-bypass", "") == cmd


class TestPiTypes:
    @pytest.mark.parametrize(
        "pi_type",
        [
            "pi-zai", "pi-zai-restricted", "pi-zai-readonly",
            "pi-deepseek", "pi-deepseek-restricted",
            "pi-openrouter",
        ],
    )
    def test_pi_passthrough(self, pi_type):
        cmd = "pi --provider zai"
        out = _inject_resume_flags(cmd, pi_type, "abc123")
        assert out == cmd


class TestEdgeCases:
    def test_unknown_type_passthrough(self):
        cmd = "claude --model x"
        # Unknown types don't get the claude treatment, even if they happen to
        # start with the claude binary.
        assert _inject_resume_flags(cmd, "bare", "abc") == cmd

    def test_command_not_starting_with_claude_is_passthrough(self):
        # Wrapper command — don't try to be clever about embedded `claude`
        # tokens; only inject for the canonical leading token.
        cmd = "env CLAUDECODE=1 claude --model x"
        out = _inject_resume_flags(cmd, "claude-bypass", "abc")
        assert out == cmd
