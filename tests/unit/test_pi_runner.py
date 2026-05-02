"""Tests for agentwire/workflows/pi_runner.py — pi subprocess + JSONL parsing."""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agentwire.workflows import pi_runner
from agentwire.workflows.node import ActionNode


# ---------------------------------------------------------------------------
# build_pi_command
# ---------------------------------------------------------------------------


class TestBuildPiCommand:
    def test_minimal_node(self):
        node = ActionNode(id="t", prompt="hello")
        cmd = pi_runner.build_pi_command(node)
        assert cmd[0] == "pi"
        assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "hello"
        assert "--provider" in cmd and cmd[cmd.index("--provider") + 1] == "zai"
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "glm-5.1"
        assert "--mode" in cmd and cmd[cmd.index("--mode") + 1] == "json"
        assert "--no-session" in cmd
        # Empty tools list → DEFAULT_TOOLS injected, not --no-tools
        assert "--tools" in cmd
        tools_arg = cmd[cmd.index("--tools") + 1]
        assert tools_arg  # non-empty

    def test_explicit_provider_and_model(self):
        node = ActionNode(id="t", prompt="x", provider="deepseek", model="deepseek-chat")
        cmd = pi_runner.build_pi_command(node)
        assert cmd[cmd.index("--provider") + 1] == "deepseek"
        assert cmd[cmd.index("--model") + 1] == "deepseek-chat"

    def test_custom_binary(self):
        node = ActionNode(id="t", prompt="x")
        cmd = pi_runner.build_pi_command(node, pi_binary="/opt/pi/bin/pi")
        assert cmd[0] == "/opt/pi/bin/pi"

    def test_thinking_flag(self):
        node = ActionNode(id="t", prompt="x", thinking="high")
        cmd = pi_runner.build_pi_command(node)
        assert cmd[cmd.index("--thinking") + 1] == "high"

    def test_explicit_tools_used_verbatim(self):
        node = ActionNode(id="t", prompt="x", tools=["read", "bash"])
        cmd = pi_runner.build_pi_command(node)
        assert cmd[cmd.index("--tools") + 1] == "read,bash"


# ---------------------------------------------------------------------------
# _get_pi_api_key — config first, env fallback
# ---------------------------------------------------------------------------


class TestGetPiApiKey:
    def test_config_takes_precedence(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "pi:\n  providers:\n    zai:\n      api_key: from-config\n"
        )
        monkeypatch.setattr(pi_runner, "CONFIG_PATH", config_path)
        monkeypatch.setenv("ZAI_API_KEY", "from-env")
        assert pi_runner._get_pi_api_key("zai") == "from-config"

    def test_env_fallback_when_config_missing(self, tmp_path, monkeypatch):
        # Point at non-existent config
        monkeypatch.setattr(pi_runner, "CONFIG_PATH", tmp_path / "missing.yaml")
        monkeypatch.setenv("ZAI_API_KEY", "from-env")
        assert pi_runner._get_pi_api_key("zai") == "from-env"

    def test_env_fallback_when_config_empty_for_provider(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("pi:\n  providers:\n    zai:\n      api_key: ''\n")
        monkeypatch.setattr(pi_runner, "CONFIG_PATH", config_path)
        monkeypatch.setenv("ZAI_API_KEY", "from-env")
        assert pi_runner._get_pi_api_key("zai") == "from-env"

    def test_env_var_name_normalizes_hyphens(self, tmp_path, monkeypatch):
        """Provider 'foo-bar' should map to env var FOO_BAR_API_KEY."""
        monkeypatch.setattr(pi_runner, "CONFIG_PATH", tmp_path / "missing.yaml")
        monkeypatch.setenv("FOO_BAR_API_KEY", "ok")
        assert pi_runner._get_pi_api_key("foo-bar") == "ok"

    def test_neither_present_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pi_runner, "CONFIG_PATH", tmp_path / "missing.yaml")
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        assert pi_runner._get_pi_api_key("zai") == ""

    def test_malformed_config_does_not_crash(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.yaml"
        # Top-level YAML scalar (not a dict) — code path checks isinstance(data, dict)
        config_path.write_text("just-a-string")
        monkeypatch.setattr(pi_runner, "CONFIG_PATH", config_path)
        monkeypatch.setenv("ZAI_API_KEY", "from-env")
        assert pi_runner._get_pi_api_key("zai") == "from-env"


# ---------------------------------------------------------------------------
# _extract_final_assistant_text
# ---------------------------------------------------------------------------


class TestExtractFinalAssistantText:
    def test_empty_events(self):
        assert pi_runner._extract_final_assistant_text([]) == ""

    def test_single_assistant_message(self):
        events = [
            {"type": "message_end", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello world"}],
            }},
        ]
        assert pi_runner._extract_final_assistant_text(events) == "hello world"

    def test_returns_last_assistant_message(self):
        events = [
            {"type": "message_end", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "first"}],
            }},
            {"type": "message_end", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "final"}],
            }},
        ]
        assert pi_runner._extract_final_assistant_text(events) == "final"

    def test_skips_user_messages(self):
        events = [
            {"type": "message_end", "message": {
                "role": "user",
                "content": [{"type": "text", "text": "ignored"}],
            }},
            {"type": "message_end", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "answer"}],
            }},
        ]
        assert pi_runner._extract_final_assistant_text(events) == "answer"

    def test_concatenates_text_blocks(self):
        events = [
            {"type": "message_end", "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "part 1 "},
                    {"type": "text", "text": "part 2"},
                ],
            }},
        ]
        assert pi_runner._extract_final_assistant_text(events) == "part 1 part 2"

    def test_skips_non_text_blocks(self):
        events = [
            {"type": "message_end", "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "read", "input": {}},
                    {"type": "text", "text": "answer"},
                ],
            }},
        ]
        assert pi_runner._extract_final_assistant_text(events) == "answer"

    def test_skips_other_event_types(self):
        events = [
            {"type": "agent_start"},
            {"type": "turn_start"},
            {"type": "message_end", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
            }},
        ]
        assert pi_runner._extract_final_assistant_text(events) == "ok"


# ---------------------------------------------------------------------------
# _extract_tool_calls
# ---------------------------------------------------------------------------


class TestExtractToolCalls:
    def test_empty(self):
        assert pi_runner._extract_tool_calls([]) == []

    def test_single_tool_call(self):
        events = [
            {"type": "message_end", "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "x"}},
                ],
            }},
        ]
        calls = pi_runner._extract_tool_calls(events)
        assert calls == [{"id": "t1", "name": "read", "input": {"path": "x"}}]

    def test_dedupe_by_id(self):
        block = {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "x"}}
        events = [
            {"type": "message_end", "message": {"role": "assistant", "content": [block]}},
            {"type": "turn_end", "message": {"role": "assistant", "content": [block]}},
        ]
        calls = pi_runner._extract_tool_calls(events)
        assert len(calls) == 1

    def test_skips_user_messages(self):
        events = [
            {"type": "message_end", "message": {
                "role": "user",
                "content": [{"type": "tool_use", "id": "x", "name": "read"}],
            }},
        ]
        assert pi_runner._extract_tool_calls(events) == []

    def test_skips_non_tooluse_blocks(self):
        events = [
            {"type": "message_end", "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "thinking..."},
                    {"type": "tool_use", "id": "t1", "name": "read"},
                ],
            }},
        ]
        calls = pi_runner._extract_tool_calls(events)
        assert len(calls) == 1 and calls[0]["id"] == "t1"


# ---------------------------------------------------------------------------
# _extract_token_usage
# ---------------------------------------------------------------------------


class TestExtractTokenUsage:
    def test_empty(self):
        usage = pi_runner._extract_token_usage([])
        assert usage == {
            "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
            "totalTokens": 0, "cost": 0.0,
        }

    def test_single_message(self):
        events = [
            {"type": "message_end", "message": {
                "role": "assistant",
                "usage": {"input": 100, "output": 50, "totalTokens": 150,
                          "cost": {"total": 0.0025}},
            }},
        ]
        usage = pi_runner._extract_token_usage(events)
        assert usage["input"] == 100
        assert usage["output"] == 50
        assert usage["totalTokens"] == 150
        assert usage["cost"] == 0.0025

    def test_aggregates_across_messages(self):
        events = [
            {"type": "message_end", "message": {
                "role": "assistant",
                "usage": {"input": 100, "output": 50, "cost": {"total": 0.01}},
            }},
            {"type": "message_end", "message": {
                "role": "assistant",
                "usage": {"input": 200, "output": 75, "cost": {"total": 0.02}},
            }},
        ]
        usage = pi_runner._extract_token_usage(events)
        assert usage["input"] == 300
        assert usage["output"] == 125
        assert usage["cost"] == pytest.approx(0.03)

    def test_missing_usage_skipped(self):
        events = [
            {"type": "message_end", "message": {"role": "assistant"}},
        ]
        usage = pi_runner._extract_token_usage(events)
        assert usage["input"] == 0


# ---------------------------------------------------------------------------
# run_node — subprocess paths via Popen mock
# ---------------------------------------------------------------------------


class TestRunNode:
    @pytest.fixture
    def fake_pi(self, monkeypatch):
        """shutil.which('pi') returns a non-None path."""
        monkeypatch.setattr(pi_runner.shutil, "which", lambda _: "/usr/local/bin/pi")
        monkeypatch.setattr(pi_runner, "_get_pi_api_key", lambda _: "test-key")

    def _popen_returning(self, stdout="", stderr="", returncode=0, timeout_first=False):
        """Build a Popen mock with the requested communicate() result."""
        proc = MagicMock()
        if timeout_first:
            proc.communicate.side_effect = [
                subprocess.TimeoutExpired(cmd="pi", timeout=1),
                ("", ""),  # second call after kill
            ]
        else:
            proc.communicate.return_value = (stdout, stderr)
        proc.returncode = returncode
        proc.kill = MagicMock()
        return proc

    def test_validation_failure_returns_failure_no_subprocess(self, fake_pi, monkeypatch):
        node = ActionNode(id="", prompt="")  # invalid: missing id and prompt
        with patch.object(pi_runner.subprocess, "Popen") as mock_popen:
            result = pi_runner.run_node(node)
        assert result.status == "failure"
        assert "id is required" in result.error or "prompt is required" in result.error
        mock_popen.assert_not_called()

    def test_pi_binary_missing(self, monkeypatch):
        monkeypatch.setattr(pi_runner.shutil, "which", lambda _: None)
        node = ActionNode(id="t", prompt="hello")
        result = pi_runner.run_node(node)
        assert result.status == "failure"
        assert "pi binary not found" in result.error

    def test_success_path(self, fake_pi):
        events_jsonl = "\n".join([
            json.dumps({"type": "agent_start"}),
            json.dumps({"type": "message_end", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "all done"}],
                "usage": {"input": 10, "output": 5, "totalTokens": 15},
            }}),
        ])
        proc = self._popen_returning(stdout=events_jsonl, stderr="", returncode=0)
        node = ActionNode(id="t", prompt="hello")
        with patch.object(pi_runner.subprocess, "Popen", return_value=proc):
            result = pi_runner.run_node(node)
        assert result.status == "success"
        assert result.final_text == "all done"
        assert result.exit_code == 0
        assert result.tokens_used["input"] == 10

    def test_nonzero_exit_records_error(self, fake_pi):
        proc = self._popen_returning(stdout="", stderr="boom", returncode=1)
        node = ActionNode(id="t", prompt="hello")
        with patch.object(pi_runner.subprocess, "Popen", return_value=proc):
            result = pi_runner.run_node(node)
        assert result.status == "failure"
        assert result.exit_code == 1
        assert "boom" in result.error

    def test_timeout(self, fake_pi):
        proc = self._popen_returning(timeout_first=True)
        node = ActionNode(id="t", prompt="hello", timeout=1)
        with patch.object(pi_runner.subprocess, "Popen", return_value=proc):
            result = pi_runner.run_node(node)
        assert result.status == "timeout"
        assert result.exit_code == -1
        assert "timeout" in result.error.lower()
        proc.kill.assert_called_once()

    def test_malformed_jsonl_lines_skipped(self, fake_pi):
        events_jsonl = "\n".join([
            "not valid json",
            json.dumps({"type": "message_end", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
            }}),
        ])
        proc = self._popen_returning(stdout=events_jsonl, returncode=0)
        node = ActionNode(id="t", prompt="hello")
        with patch.object(pi_runner.subprocess, "Popen", return_value=proc):
            result = pi_runner.run_node(node)
        # Bad line skipped; valid line parsed
        assert result.status == "success"
        assert result.final_text == "ok"
        # The malformed line is NOT in events (skipped on JSONDecodeError)
        types = [e.get("type") for e in result.events]
        assert "message_end" in types

    def test_api_error_in_event_surfaces_as_failure(self, fake_pi):
        events_jsonl = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "rate limit hit",
                "content": [],
            },
        })
        # exit 0, but stopReason=error → should still be marked failure
        proc = self._popen_returning(stdout=events_jsonl, returncode=0)
        node = ActionNode(id="t", prompt="hello")
        with patch.object(pi_runner.subprocess, "Popen", return_value=proc):
            result = pi_runner.run_node(node)
        assert result.status == "failure"
        assert "rate limit" in result.error

    def test_writes_event_log_when_path_given(self, fake_pi, tmp_path):
        events_jsonl = json.dumps({"type": "agent_start"})
        proc = self._popen_returning(stdout=events_jsonl, returncode=0)
        node = ActionNode(id="t", prompt="hello")
        log_path = tmp_path / "subdir" / "events.jsonl"
        with patch.object(pi_runner.subprocess, "Popen", return_value=proc):
            pi_runner.run_node(node, event_log_path=log_path)
        assert log_path.exists()
        # Subdir was created
        assert log_path.parent.is_dir()
        assert "agent_start" in log_path.read_text()

    def test_api_key_injected_into_env(self, fake_pi):
        captured_env = {}
        def capture_popen(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            proc = MagicMock()
            proc.communicate.return_value = ("", "")
            proc.returncode = 0
            return proc
        node = ActionNode(id="t", prompt="hello", provider="deepseek")
        with patch.object(pi_runner, "_get_pi_api_key", lambda p: f"key-for-{p}"):
            with patch.object(pi_runner.subprocess, "Popen", side_effect=capture_popen):
                pi_runner.run_node(node)
        assert captured_env.get("DEEPSEEK_API_KEY") == "key-for-deepseek"
