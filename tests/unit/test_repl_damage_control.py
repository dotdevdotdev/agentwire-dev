"""Tests for the REPL's Python-side damage control (Phase 3 PR 2).

Mirrors the shell-hook patterns from ~/.agentwire/hooks/damage-control/ but
runs in-process via the SDK's PreToolUse hook.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentwire.sdk.damage_control import (
    check_bash,
    check_path,
    load_patterns,
    make_pre_tool_hook,
)


@pytest.fixture
def patterns_file(tmp_path):
    p = tmp_path / "patterns.yaml"
    p.write_text(
        """
bashToolPatterns:
  - pattern: '\\brm\\s+-[rRf]'
    reason: rm -rf
  - pattern: '\\bgit\\s+push\\s+.*--force(?!-with-lease)'
    reason: git push --force
  - pattern: '\\brm\\s+[^-]'
    reason: rm bypassable
    bypassable: true
  - pattern: '\\bsudo\\s+apt\\s+install'
    reason: package install (ask)
    ask: true

editToolPatterns:
  - pattern: '/etc/passwd'
    reason: editing system file

writeToolPatterns:
  - pattern: '\\.env$'
    reason: writing dotenv file
"""
    )
    return p


@pytest.fixture
def patterns(patterns_file):
    return load_patterns(patterns_file)


class TestLoadPatterns:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_patterns(tmp_path / "nope.yaml") == {}

    def test_garbage_yaml_returns_empty(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("{not yaml")
        assert load_patterns(bad) == {}

    def test_valid_loads(self, patterns_file):
        result = load_patterns(patterns_file)
        assert "bashToolPatterns" in result
        assert "editToolPatterns" in result


class TestCheckBash:
    def test_clean_command_passes(self, patterns):
        assert check_bash("ls -la", patterns, mode="bypass") is None
        assert check_bash("git status", patterns, mode="prompted") is None

    def test_rm_rf_blocks_in_any_mode(self, patterns):
        verdict, reason = check_bash("rm -rf /tmp/foo", patterns, mode="bypass")
        assert verdict == "deny"
        assert "rm -rf" in reason

    def test_force_push_blocks(self, patterns):
        verdict, reason = check_bash("git push --force origin main", patterns, mode="bypass")
        assert verdict == "deny"

    def test_force_with_lease_passes(self, patterns):
        assert check_bash("git push --force-with-lease origin main", patterns, mode="bypass") is None

    def test_bypassable_passes_in_bypass_mode(self, patterns):
        assert check_bash("rm somefile.txt", patterns, mode="bypass") is None

    def test_bypassable_blocks_in_prompted_mode(self, patterns):
        verdict, reason = check_bash("rm somefile.txt", patterns, mode="prompted")
        assert verdict == "deny"

    def test_ask_returns_ask(self, patterns):
        verdict, reason = check_bash("sudo apt install vim", patterns, mode="bypass")
        assert verdict == "ask"

    def test_empty_command(self, patterns):
        assert check_bash("", patterns, mode="bypass") is None

    def test_invalid_regex_skipped(self, tmp_path):
        # Pattern with broken regex shouldn't crash; it should just be skipped
        p = tmp_path / "p.yaml"
        p.write_text("bashToolPatterns:\n  - pattern: '['\n    reason: broken\n")
        patterns = load_patterns(p)
        assert check_bash("rm -rf /", patterns, mode="bypass") is None


class TestCheckPath:
    def test_clean_path(self, patterns):
        assert check_path("/home/user/code.py", patterns, "editToolPatterns") is None

    def test_blocked_path(self, patterns):
        verdict, reason = check_path("/etc/passwd", patterns, "editToolPatterns")
        assert verdict == "deny"

    def test_dotenv_blocked_in_write(self, patterns):
        verdict, reason = check_path(".env", patterns, "writeToolPatterns")
        assert verdict == "deny"

    def test_no_pattern_set_returns_none(self):
        assert check_path("/etc/passwd", {}, "editToolPatterns") is None


class TestMakePreToolHook:
    def test_bash_block(self, patterns_file):
        hook = make_pre_tool_hook(mode="bypass", patterns_path=patterns_file)
        out = asyncio.run(hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
            "tool-id-1", None,
        ))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "rm -rf" in out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_bash_clean_returns_empty(self, patterns_file):
        hook = make_pre_tool_hook(mode="bypass", patterns_path=patterns_file)
        out = asyncio.run(hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
            "tool-id-1", None,
        ))
        assert out == {}

    def test_edit_block(self, patterns_file):
        hook = make_pre_tool_hook(mode="bypass", patterns_path=patterns_file)
        out = asyncio.run(hook(
            {"tool_name": "Edit", "tool_input": {"file_path": "/etc/passwd"}},
            "tool-id-1", None,
        ))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_write_dotenv_block(self, patterns_file):
        hook = make_pre_tool_hook(mode="bypass", patterns_path=patterns_file)
        out = asyncio.run(hook(
            {"tool_name": "Write", "tool_input": {"file_path": ".env"}},
            "tool-id-1", None,
        ))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_unknown_tool_passes(self, patterns_file):
        hook = make_pre_tool_hook(mode="bypass", patterns_path=patterns_file)
        out = asyncio.run(hook(
            {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}},
            "tool-id-1", None,
        ))
        assert out == {}

    def test_no_patterns_returns_none(self, tmp_path):
        # No file → load_patterns returns {} → make_pre_tool_hook returns None
        # so no hook is registered (rather than a no-op hook hogging cycles).
        hook = make_pre_tool_hook(mode="bypass", patterns_path=tmp_path / "missing.yaml")
        assert hook is None

    def test_bypassable_in_bypass(self, patterns_file):
        hook = make_pre_tool_hook(mode="bypass", patterns_path=patterns_file)
        out = asyncio.run(hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm somefile.txt"}},
            "tool-id-1", None,
        ))
        assert out == {}

    def test_bypassable_in_prompted_blocks(self, patterns_file):
        hook = make_pre_tool_hook(mode="prompted", patterns_path=patterns_file)
        out = asyncio.run(hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm somefile.txt"}},
            "tool-id-1", None,
        ))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
