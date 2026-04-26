"""Tests for the SDK-backed REPL — build_options, render_message, print mode.

Phase 1 PR 2. See docs/missions/agentwire-repl.md.
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentwire.sdk import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    FULL_TOOLS,
    PERMISSION_MODE_MAP,
    RESTRICTED_TOOLS,
    build_options,
    render_message,
)
from agentwire.sdk.client import _find_ancestor_file
from agentwire.sdk.render import (
    format_tool_input as _format_tool_input,
    format_tool_result as _format_tool_result,
)


# A fake ClaudeAgentOptions that records kwargs rather than validating them,
# so we can assert on shape without depending on the real SDK at test time.
class FakeOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


# --- build_options ---

class TestBuildOptions:
    @pytest.fixture(autouse=True)
    def _disable_mcp(self, monkeypatch):
        # These tests assert exact tool-list shape — turn off the auto-attached
        # agentwire MCP server so allowed_tools matches the static FULL_TOOLS /
        # RESTRICTED_TOOLS arrays. MCP attachment has its own dedicated tests.
        monkeypatch.setenv("AGENTWIRE_REPL_MCP", "0")
        # Same reasoning for damage control: separate dedicated tests.
        monkeypatch.setenv("AGENTWIRE_REPL_DAMAGE_CONTROL", "0")

    def test_bypass_mode(self, tmp_path):
        opts = build_options(FakeOptions, "bypass", None, None, cwd=tmp_path)
        assert opts.kwargs["permission_mode"] == "bypassPermissions"
        assert opts.kwargs["allowed_tools"] == FULL_TOOLS
        assert opts.kwargs["model"] == DEFAULT_MODEL
        assert opts.kwargs["effort"] == DEFAULT_EFFORT
        assert opts.kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert opts.kwargs["setting_sources"] == ["user"]

    def test_prompted_mode(self, tmp_path):
        opts = build_options(FakeOptions, "prompted", None, None, cwd=tmp_path)
        assert opts.kwargs["permission_mode"] == "default"
        assert opts.kwargs["allowed_tools"] == FULL_TOOLS

    def test_restricted_mode(self, tmp_path):
        opts = build_options(FakeOptions, "restricted", None, None, cwd=tmp_path)
        assert opts.kwargs["permission_mode"] == "plan"
        assert opts.kwargs["allowed_tools"] == RESTRICTED_TOOLS
        # Restricted drops Write/Edit/Bash
        for tool in ("Write", "Edit", "Bash"):
            assert tool not in opts.kwargs["allowed_tools"]

    def test_model_override(self, tmp_path):
        opts = build_options(FakeOptions, "bypass", "claude-sonnet-4-6", None, cwd=tmp_path)
        assert opts.kwargs["model"] == "claude-sonnet-4-6"

    def test_unknown_mode_defaults_bypass(self, tmp_path):
        opts = build_options(FakeOptions, "whatever", None, None, cwd=tmp_path)
        assert opts.kwargs["permission_mode"] == "bypassPermissions"

    def test_cwd_passed_as_str(self, tmp_path):
        opts = build_options(FakeOptions, "bypass", None, None, cwd=tmp_path)
        assert opts.kwargs["cwd"] == str(tmp_path)

    def test_no_system_prompt_no_field(self, tmp_path):
        # Empty cwd dir, no system_prompt → no system_prompt kwarg emitted
        opts = build_options(FakeOptions, "bypass", None, None, cwd=tmp_path)
        assert "system_prompt" not in opts.kwargs

    def test_explicit_system_prompt_appended(self, tmp_path):
        opts = build_options(FakeOptions, "bypass", None, "custom role text", cwd=tmp_path)
        sp = opts.kwargs["system_prompt"]
        assert sp["type"] == "preset"
        assert sp["preset"] == "claude_code"
        assert "custom role text" in sp["append"]

    def test_claude_md_auto_discovery(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("Project CLAUDE.md content")
        opts = build_options(FakeOptions, "bypass", None, None, cwd=tmp_path)
        sp = opts.kwargs["system_prompt"]
        assert "Project CLAUDE.md content" in sp["append"]

    def test_agents_md_auto_discovery(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Agent context from AGENTS.md")
        opts = build_options(FakeOptions, "bypass", None, None, cwd=tmp_path)
        sp = opts.kwargs["system_prompt"]
        assert "Agent context from AGENTS.md" in sp["append"]

    def test_both_files_and_explicit_all_concatenated(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("AAA")
        (tmp_path / "AGENTS.md").write_text("BBB")
        opts = build_options(FakeOptions, "bypass", None, "CCC", cwd=tmp_path)
        append = opts.kwargs["system_prompt"]["append"]
        assert "AAA" in append and "BBB" in append and "CCC" in append

    def test_ancestor_walk(self, tmp_path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        (tmp_path / "CLAUDE.md").write_text("from parent")
        opts = build_options(FakeOptions, "bypass", None, None, cwd=nested)
        assert "from parent" in opts.kwargs["system_prompt"]["append"]


class TestFindAncestorFile:
    def test_finds_in_current_dir(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("x")
        assert _find_ancestor_file(tmp_path, "CLAUDE.md") == f

    def test_walks_up(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        f = tmp_path / "CLAUDE.md"
        f.write_text("x")
        assert _find_ancestor_file(nested, "CLAUDE.md") == f

    def test_missing_returns_none(self, tmp_path):
        assert _find_ancestor_file(tmp_path, "CLAUDE.md") is None


# --- render_message ---

# Fake SDK message types (structural). The renderer uses isinstance, so we
# pass the same fake classes both to render_message and to the messages.
class FakeAssistantMessage:
    def __init__(self, content, model=None):
        self.content = content
        self.model = model


class FakeUserMessage:
    def __init__(self, content):
        self.content = content


class FakeSystemMessage:
    def __init__(self, subtype, data=None):
        self.subtype = subtype
        self.data = data or {}


class FakeResultMessage:
    def __init__(self, usage=None, total_cost_usd=None, duration_ms=None, is_error=False, result=None):
        self.usage = usage or {}
        self.total_cost_usd = total_cost_usd
        self.duration_ms = duration_ms
        self.is_error = is_error
        self.result = result


RENDER_KWARGS = dict(
    AssistantMessage=FakeAssistantMessage,
    UserMessage=FakeUserMessage,
    SystemMessage=FakeSystemMessage,
    ResultMessage=FakeResultMessage,
)


def _render(msg) -> str:
    buf = io.StringIO()
    render_message(msg, out=buf, **RENDER_KWARGS)
    return buf.getvalue()


class TestRenderSystem:
    def test_init_shows_model_and_session(self):
        msg = FakeSystemMessage("init", {"model": "claude-opus-4-7", "session_id": "abc123xyz456"})
        out = _render(msg)
        assert "claude-opus-4-7" in out
        assert "abc123xy" in out  # 8-char truncation

    def test_non_init_silent(self):
        msg = FakeSystemMessage("whatever", {})
        assert _render(msg) == ""


class TestRenderAssistant:
    def test_text_block(self):
        msg = FakeAssistantMessage([{"type": "text", "text": "Hello, world!"}])
        assert "Hello, world!" in _render(msg)

    def test_tool_use_bash(self):
        msg = FakeAssistantMessage([{
            "type": "tool_use", "name": "Bash", "input": {"command": "ls -la"},
        }])
        out = _render(msg)
        assert "- Bash ls -la" in out

    def test_tool_use_read_shows_file_path(self):
        msg = FakeAssistantMessage([{
            "type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x.txt"},
        }])
        out = _render(msg)
        assert "/tmp/x.txt" in out

    def test_tool_use_grep(self):
        msg = FakeAssistantMessage([{
            "type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"},
        }])
        assert "- Grep TODO" in _render(msg)

    def test_tool_use_websearch(self):
        msg = FakeAssistantMessage([{
            "type": "tool_use", "name": "WebSearch", "input": {"query": "brave search API"},
        }])
        assert "brave search API" in _render(msg)

    def test_thinking_block_shows_preview(self):
        msg = FakeAssistantMessage([{
            "type": "thinking", "thinking": "First line of reasoning\nSecond line here",
        }])
        out = _render(msg)
        assert "First line of reasoning" in out
        assert "Second line" not in out  # only first line shown

    def test_bash_long_command_truncated(self):
        long_cmd = "echo " + "x" * 200
        msg = FakeAssistantMessage([{
            "type": "tool_use", "name": "Bash", "input": {"command": long_cmd},
        }])
        out = _render(msg)
        assert "..." in out


class TestRenderUser:
    def test_tool_result_shown(self):
        msg = FakeUserMessage([{
            "type": "tool_result", "tool_use_id": "tu_1", "content": "hello output",
        }])
        out = _render(msg)
        assert "· result: hello output" in out

    def test_tool_result_list_content(self):
        msg = FakeUserMessage([{
            "type": "tool_result",
            "tool_use_id": "tu_1",
            "content": [{"type": "text", "text": "from block list"}],
        }])
        assert "from block list" in _render(msg)

    def test_text_content_not_rendered_as_tool_result(self):
        msg = FakeUserMessage("plain user text")  # string-only content
        assert _render(msg) == ""


class TestRenderResult:
    def test_success_with_tokens_and_cost(self):
        msg = FakeResultMessage(
            usage={"input_tokens": 1000, "output_tokens": 200},
            total_cost_usd=0.0123,
            duration_ms=4500,
        )
        out = _render(msg)
        assert "done" in out
        assert "1000+200 tok" in out
        assert "$0.0123" in out
        assert "4.5s" in out

    def test_error_shows_error(self):
        msg = FakeResultMessage(is_error=True, result="rate limit hit")
        out = _render(msg)
        assert "- error" in out
        assert "rate limit hit" in out

    def test_minimal_result(self):
        msg = FakeResultMessage()
        out = _render(msg)
        assert "- done" in out


class TestToolCallHierarchy:
    """Bullet-format renderer: tool_use renders as `- Tool args` immediately;
    matching tool_result emits as `  · result: preview` indented under it."""

    def _state(self):
        from agentwire.sdk import StreamRenderState as _StreamRenderState
        return _StreamRenderState()

    def test_tool_use_renders_immediately(self):
        from agentwire.sdk import StreamRenderState as _StreamRenderState
        s = _StreamRenderState()
        out = io.StringIO()

        msg = FakeAssistantMessage([{
            "type": "tool_use", "id": "tu_1", "name": "Bash",
            "input": {"command": "ls -la"},
        }])
        render_message(msg, out=out, stream_state=s, **RENDER_KWARGS)
        rendered = out.getvalue()
        # Top-level bullet is emitted right away.
        assert "- Bash ls -la" in rendered
        # And the id is tracked so a future tool_result can indent under it.
        assert "tu_1" in s.pending_tool_uses

        # UserMessage with tool_result emits an indented child.
        result = FakeUserMessage([{
            "type": "tool_result", "tool_use_id": "tu_1", "content": "12 files",
        }])
        render_message(result, out=out, stream_state=s, **RENDER_KWARGS)
        rendered = out.getvalue()
        assert "- Bash ls -la" in rendered
        assert "  · result: 12 files" in rendered
        # Pending cleared after the matching result lands.
        assert "tu_1" not in s.pending_tool_uses

    def test_tool_use_without_stream_state(self):
        # Without stream_state, the bullet still emits — there's just no
        # pending registry to track for the indent under it.
        msg = FakeAssistantMessage([{
            "type": "tool_use", "id": "tu_1", "name": "Bash",
            "input": {"command": "ls -la"},
        }])
        out = _render(msg)  # no stream_state
        assert "- Bash ls -la" in out

    def test_tool_use_without_id(self):
        # Even without an id, the bullet emits — just no pending registration.
        from agentwire.sdk import StreamRenderState as _StreamRenderState
        s = _StreamRenderState()
        out = io.StringIO()
        msg = FakeAssistantMessage([{
            "type": "tool_use", "name": "Bash",  # no id
            "input": {"command": "ls -la"},
        }])
        render_message(msg, out=out, stream_state=s, **RENDER_KWARGS)
        assert "- Bash ls -la" in out.getvalue()
        assert s.pending_tool_uses == {}

    def test_unmatched_tool_use_remains_visible(self):
        # If a tool_use never gets a tool_result, the bullet stays in chat
        # and the pending registry is just cleared on ResultMessage.
        from agentwire.sdk import StreamRenderState as _StreamRenderState
        s = _StreamRenderState()
        out = io.StringIO()
        msg = FakeAssistantMessage([{
            "type": "tool_use", "id": "tu_orphan", "name": "Bash",
            "input": {"command": "echo x"},
        }])
        render_message(msg, out=out, stream_state=s, **RENDER_KWARGS)
        result = FakeResultMessage()
        render_message(result, out=out, stream_state=s, **RENDER_KWARGS)
        rendered = out.getvalue()
        assert "- Bash echo x" in rendered
        assert "- done" in rendered
        # Pending cleared.
        assert s.pending_tool_uses == {}

    def test_error_result_indents_as_error_child(self):
        # An error result indents under its tool_use as `  · error: ...`.
        from agentwire.sdk import StreamRenderState as _StreamRenderState
        s = _StreamRenderState()
        out = io.StringIO()
        msg = FakeAssistantMessage([{
            "type": "tool_use", "id": "tu_err", "name": "Bash",
            "input": {"command": "false"},
        }])
        render_message(msg, out=out, stream_state=s, **RENDER_KWARGS)
        result = FakeUserMessage([{
            "type": "tool_result", "tool_use_id": "tu_err",
            "content": "exit 1", "is_error": True,
        }])
        render_message(result, out=out, stream_state=s, **RENDER_KWARGS)
        rendered = out.getvalue()
        assert "- Bash false" in rendered
        assert "  · error: exit 1" in rendered


# --- format helpers ---

class TestFormatToolInput:
    def test_read_file_path(self):
        assert _format_tool_input("Read", {"file_path": "x.py"}) == "x.py"

    def test_bash_command(self):
        assert _format_tool_input("Bash", {"command": "pwd"}) == "pwd"

    def test_bash_long_truncated(self):
        long = "a" * 150
        out = _format_tool_input("Bash", {"command": long})
        assert out.endswith("...")
        assert len(out) == 80

    def test_unknown_tool_fallback(self):
        out = _format_tool_input("Mystery", {"key": "value"})
        assert "key" in out

    def test_non_dict_input(self):
        assert _format_tool_input("Bash", "not a dict") == ""


# --- print mode end-to-end (mocked SDK) ---

class FakeAsyncContextManager:
    """Minimal async-context fake for ClaudeSDKClient."""

    def __init__(self, messages, options=None):
        self._messages = messages
        self.options = options
        self.queried_prompts: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def query(self, prompt):
        self.queried_prompts.append(prompt)

    async def receive_response(self):
        for m in self._messages:
            yield m


class TestPrintModeIntegration:
    """End-to-end print-mode test with claude-agent-sdk mocked out.

    Installs a fake `claude_agent_sdk` module in sys.modules BEFORE `run_repl`
    imports it, so the real SDK is never touched.
    """

    def test_prints_and_returns_zero(self, monkeypatch, capsys):
        import sys as _sys
        import types

        # Build the fake module
        fake_sdk = types.ModuleType("claude_agent_sdk")

        fake_sdk.AssistantMessage = FakeAssistantMessage
        fake_sdk.UserMessage = FakeUserMessage
        fake_sdk.SystemMessage = FakeSystemMessage
        fake_sdk.ResultMessage = FakeResultMessage
        fake_sdk.ClaudeAgentOptions = FakeOptions

        messages = [
            FakeSystemMessage("init", {"model": "claude-opus-4-7", "session_id": "abc12345def"}),
            FakeAssistantMessage([
                {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
            ]),
            FakeUserMessage([{"type": "tool_result", "tool_use_id": "t1", "content": "file contents here"}]),
            FakeAssistantMessage([{"type": "text", "text": "Here's the summary."}]),
            FakeResultMessage(
                usage={"input_tokens": 100, "output_tokens": 50},
                total_cost_usd=0.001,
                duration_ms=1500,
                is_error=False,
            ),
        ]

        captured_options = {}

        class MockClient:
            def __init__(self, options):
                captured_options["options"] = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def query(self, prompt):
                captured_options["prompt"] = prompt

            async def receive_response(self):
                for m in messages:
                    yield m

        fake_sdk.ClaudeSDKClient = MockClient
        monkeypatch.setitem(_sys.modules, "claude_agent_sdk", fake_sdk)

        from agentwire.repl.app import run_repl
        rc = run_repl(mode="bypass", print_prompt="summarize x.py")
        out = capsys.readouterr().out

        assert rc == 0
        assert captured_options["prompt"] == "summarize x.py"
        assert "agent started" in out
        assert "claude-opus-4-7" in out
        assert "- Read x.py" in out
        assert "· result: file contents here" in out
        assert "Here's the summary." in out
        assert "- done" in out
        assert "100+50 tok" in out

    def test_error_result_returns_nonzero(self, monkeypatch, capsys):
        import sys as _sys
        import types
        fake_sdk = types.ModuleType("claude_agent_sdk")
        fake_sdk.AssistantMessage = FakeAssistantMessage
        fake_sdk.UserMessage = FakeUserMessage
        fake_sdk.SystemMessage = FakeSystemMessage
        fake_sdk.ResultMessage = FakeResultMessage
        fake_sdk.ClaudeAgentOptions = FakeOptions

        messages = [FakeResultMessage(is_error=True, result="rate limited")]

        class MockClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def query(self, prompt): pass
            async def receive_response(self):
                for m in messages:
                    yield m

        fake_sdk.ClaudeSDKClient = MockClient
        monkeypatch.setitem(_sys.modules, "claude_agent_sdk", fake_sdk)

        from agentwire.repl.app import run_repl
        rc = run_repl(mode="bypass", print_prompt="x")
        out = capsys.readouterr().out
        assert rc == 1
        assert "- error" in out

    def test_missing_sdk_returns_one(self, monkeypatch, capsys):
        """If claude-agent-sdk isn't importable, print mode exits 1 with message."""
        import sys as _sys
        import builtins

        original_import = builtins.__import__

        def fail_import(name, *a, **kw):
            if name == "claude_agent_sdk":
                raise ImportError("No module named 'claude_agent_sdk'")
            return original_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fail_import)
        # Also ensure cached module is cleared
        monkeypatch.delitem(_sys.modules, "claude_agent_sdk", raising=False)

        from agentwire.repl.app import run_repl
        rc = run_repl(mode="bypass", print_prompt="x")
        err = capsys.readouterr().err
        assert rc == 1
        assert "claude-agent-sdk not installed" in err


class TestStreamRenderState:
    """Partial-message rendering — added 2026-04-25 to fix the silent gap."""

    def _state(self):
        from agentwire.sdk import StreamRenderState as _StreamRenderState
        return _StreamRenderState()

    def test_text_delta_streams_inline(self):
        s = self._state()
        out = io.StringIO()
        s.handle_partial(
            {"type": "content_block_start", "content_block": {"type": "text"}},
            out,
        )
        s.handle_partial(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello "}},
            out,
        )
        s.handle_partial(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "world"}},
            out,
        )
        s.handle_partial({"type": "content_block_stop"}, out)
        rendered = out.getvalue()
        assert "Hello world" in rendered
        assert s.streamed_text is True
        assert s.open_block is None

    def test_thinking_delta_streams_as_bullet_with_continuation(self):
        # Thinking starts a `- thinking: ` bullet; subsequent newline-bounded
        # chunks become `  · <continuation>` indented children.
        s = self._state()
        out = io.StringIO()
        s.handle_partial(
            {"type": "content_block_start", "content_block": {"type": "thinking"}},
            out,
        )
        s.handle_partial(
            {"type": "content_block_delta",
             "delta": {"type": "thinking_delta", "thinking": "let me\nplan this"}},
            out,
        )
        s.handle_partial({"type": "content_block_stop"}, out)
        rendered = out.getvalue()
        assert "- thinking: let me" in rendered
        assert "  · plan this" in rendered
        assert s.streamed_thinking is True

    def test_input_json_delta_skipped(self):
        # No live byte counter post-redesign — tool input only renders at
        # snapshot time when full args are known. Partial input_json_delta
        # events do not emit anything.
        s = self._state()
        out = io.StringIO()
        s.handle_partial(
            {"type": "content_block_start",
             "content_block": {"type": "tool_use", "name": "Write"}},
            out,
        )
        s.handle_partial(
            {"type": "content_block_delta",
             "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "x"}'}},
            out,
        )
        s.handle_partial({"type": "content_block_stop"}, out)
        # No output — snapshot AssistantMessage will render the bullet.
        assert out.getvalue() == ""

    def test_streamevent_dataclass_detected_by_render_message(self, monkeypatch):
        # StreamEvent is a @dataclass, not a dict. Earlier code did
        # `isinstance(message, dict)` and silently dropped every partial event.
        # Verify duck-type detection works for dataclass-shaped objects.
        from dataclasses import dataclass
        from agentwire.sdk import render_message, StreamRenderState as _StreamRenderState

        @dataclass
        class FakeStreamEvent:
            uuid: str
            session_id: str
            event: dict
            parent_tool_use_id: str | None = None

        s = _StreamRenderState()
        out = io.StringIO()
        evt = FakeStreamEvent(
            uuid="abc",
            session_id="s",
            event={"type": "content_block_start", "content_block": {"type": "text"}},
        )
        render_message(
            evt,
            AssistantMessage=FakeAssistantMessage,
            UserMessage=FakeUserMessage,
            SystemMessage=FakeSystemMessage,
            ResultMessage=FakeResultMessage,
            out=out,
            stream_state=s,
        )
        # text block opened — render_message routed the partial through
        # handle_partial. (Before the duck-type fix this was a no-op.)
        assert s.open_block == "text"
        assert s.streamed_text is True

    def test_no_ansi_codes_for_non_tty_output(self):
        # Tests run with StringIO as `out` — isatty()==False — so no ANSI
        # codes should ever leak into the captured output. Substring asserts
        # in the rest of the suite depend on this.
        s = self._state()
        out = io.StringIO()
        s.handle_partial(
            {"type": "content_block_start", "content_block": {"type": "thinking"}},
            out,
        )
        s.handle_partial(
            {"type": "content_block_delta",
             "delta": {"type": "thinking_delta", "thinking": "plan\n"}},
            out,
        )
        s.handle_partial({"type": "content_block_stop"}, out)
        rendered = out.getvalue()
        assert "\x1b[" not in rendered  # no ANSI escapes
        assert "- thinking: plan" in rendered

    def test_ansi_codes_emitted_for_tty_output(self):
        # When out.isatty() is True, dim-style ANSI codes wrap the thinking
        # bullet. This is the visual hierarchy: thinking is secondary noise,
        # dim makes it recede.
        from agentwire.sdk import StreamRenderState as _StreamRenderState

        class _TTYBuffer:
            def __init__(self):
                self.buf = io.StringIO()

            def write(self, s):
                self.buf.write(s)

            def flush(self):
                pass

            def isatty(self):
                return True

            def getvalue(self):
                return self.buf.getvalue()

        s = _StreamRenderState()
        out = _TTYBuffer()
        s.handle_partial(
            {"type": "content_block_start", "content_block": {"type": "thinking"}},
            out,
        )
        s.handle_partial(
            {"type": "content_block_delta",
             "delta": {"type": "thinking_delta", "thinking": "plan\n"}},
            out,
        )
        s.handle_partial({"type": "content_block_stop"}, out)
        rendered = out.getvalue()
        assert "\x1b[" in rendered  # ANSI present
        assert "- thinking: plan" in rendered  # raw text still recoverable

    def test_assistant_skips_streamed_text(self, monkeypatch):
        # When partials already streamed text, the snapshot AssistantMessage
        # text block should NOT re-render.
        from agentwire.sdk import render_message, StreamRenderState as _StreamRenderState
        s = _StreamRenderState()
        s.streamed_text = True
        out = io.StringIO()

        msg = FakeAssistantMessage(content=[{"type": "text", "text": "full reply"}])
        render_message(
            msg,
            AssistantMessage=FakeAssistantMessage,
            UserMessage=FakeUserMessage,
            SystemMessage=FakeSystemMessage,
            ResultMessage=FakeResultMessage,
            out=out,
            stream_state=s,
        )
        assert "full reply" not in out.getvalue()
        # state resets after snapshot
        assert s.streamed_text is False

    def test_assistant_renders_when_no_partials(self):
        from agentwire.sdk import render_message, StreamRenderState as _StreamRenderState
        s = _StreamRenderState()
        out = io.StringIO()
        msg = FakeAssistantMessage(content=[{"type": "text", "text": "full reply"}])
        render_message(
            msg,
            AssistantMessage=FakeAssistantMessage,
            UserMessage=FakeUserMessage,
            SystemMessage=FakeSystemMessage,
            ResultMessage=FakeResultMessage,
            out=out,
            stream_state=s,
        )
        assert "full reply" in out.getvalue()

    def test_heartbeat_emits_bullet_per_tick(self):
        # Each idle tick emits its own `- still working · Ns` bullet —
        # slightly noisy on long quiet turns but each line is self-contained
        # in the bullet format.
        s = self._state()
        out = io.StringIO()
        s.heartbeat(out)
        s.heartbeat(out)
        rendered = out.getvalue()
        assert "- still working · 5s" in rendered
        assert "- still working · 10s" in rendered

    def test_heartbeat_resets_on_real_event(self):
        # When a real event arrives, the heartbeat counter resets so the
        # next gap starts fresh. The previously-emitted bullets stay in the
        # chat (they're history) — there's no in-place mutation.
        s = self._state()
        out = io.StringIO()
        s.heartbeat(out)
        s.heartbeat(out)
        assert s._heartbeat_count == 2
        s.handle_partial(
            {"type": "content_block_start", "content_block": {"type": "text"}},
            out,
        )
        # Counter zeroed.
        assert s._heartbeat_count == 0
        # Subsequent heartbeats start at 5s again.
        s.heartbeat(out)
        rendered = out.getvalue()
        assert rendered.count("- still working · 5s") == 2


class TestHeartbeatIter:
    def test_emits_heartbeat_on_idle(self):
        from agentwire.sdk import heartbeat_iter as _heartbeat_iter, HEARTBEAT as _HEARTBEAT
        import asyncio

        async def slow_source():
            await asyncio.sleep(0.15)
            yield "done"

        async def collect():
            results = []
            async for x in _heartbeat_iter(slow_source(), idle_timeout=0.05):
                results.append(x)
                if x == "done":
                    break
            return results

        results = asyncio.run(collect())
        assert _HEARTBEAT in results
        assert "done" in results

    def test_no_heartbeat_when_fast(self):
        from agentwire.sdk import heartbeat_iter as _heartbeat_iter, HEARTBEAT as _HEARTBEAT
        import asyncio

        async def fast_source():
            yield 1
            yield 2

        async def collect():
            results = []
            async for x in _heartbeat_iter(fast_source(), idle_timeout=1.0):
                results.append(x)
            return results

        results = asyncio.run(collect())
        assert results == [1, 2]
        assert _HEARTBEAT not in results


class TestThinkingConfig:
    def test_adaptive_default(self):
        # adaptive now defaults to display:summarized (Opus 4.7 hides
        # thinking by default; we always want it visible in the REPL).
        from agentwire.sdk import thinking_config as _thinking_config
        assert _thinking_config("adaptive") == {"type": "adaptive", "display": "summarized"}

    def test_summarized_sets_display(self):
        from agentwire.sdk import thinking_config as _thinking_config
        assert _thinking_config("summarized") == {"type": "adaptive", "display": "summarized"}

    def test_off_disabled(self):
        from agentwire.sdk import thinking_config as _thinking_config
        assert _thinking_config("off") == {"type": "disabled"}

    def test_unknown_falls_back_adaptive(self):
        from agentwire.sdk import thinking_config as _thinking_config
        assert _thinking_config("nonsense") == {"type": "adaptive", "display": "summarized"}


class TestMcpBakedIn:
    def test_mcp_servers_attached_by_default(self, monkeypatch):
        from agentwire.sdk import build_options; from agentwire.sdk.client import MCP_SERVER_NAME, MCP_TOOL_PREFIX
        monkeypatch.delenv("AGENTWIRE_REPL_MCP", raising=False)

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        build_options(FakeOptions, mode="bypass", model="m", system_prompt=None, cwd=None)
        assert MCP_SERVER_NAME in captured["mcp_servers"]
        cfg = captured["mcp_servers"][MCP_SERVER_NAME]
        assert cfg["type"] == "stdio"
        assert cfg["args"] == ["-m", "agentwire", "mcp"]
        assert MCP_TOOL_PREFIX in captured["allowed_tools"]

    def test_mcp_disabled_via_env(self, monkeypatch):
        from agentwire.sdk import build_options; from agentwire.sdk.client import MCP_TOOL_PREFIX
        monkeypatch.setenv("AGENTWIRE_REPL_MCP", "0")

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        build_options(FakeOptions, mode="bypass", model="m", system_prompt=None, cwd=None)
        assert "mcp_servers" not in captured
        assert MCP_TOOL_PREFIX not in captured["allowed_tools"]

    def test_restricted_mode_keeps_mcp(self, monkeypatch):
        # MCP tools include lots of read-only inspection tools (sessions_list,
        # panes_list); blocking the entire server in restricted mode loses too
        # much. Plan-mode permission_mode still gates execution.
        from agentwire.sdk import build_options; from agentwire.sdk.client import MCP_TOOL_PREFIX
        monkeypatch.delenv("AGENTWIRE_REPL_MCP", raising=False)

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        build_options(FakeOptions, mode="restricted", model="m", system_prompt=None, cwd=None)
        assert MCP_TOOL_PREFIX in captured["allowed_tools"]


class TestSessionContextThreading:
    def test_role_instructions_appended_to_system_prompt(self, monkeypatch):
        from agentwire.sdk import build_options
        from agentwire.repl.context import SessionContext
        monkeypatch.setenv("AGENTWIRE_REPL_MCP", "0")
        monkeypatch.setenv("AGENTWIRE_REPL_DAMAGE_CONTROL", "0")

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        ctx = SessionContext(
            role_names=["tester"],
            role_instructions="Test rigorously.",
            voice="alice",
            missing_roles=[],
        )
        build_options(
            FakeOptions, mode="bypass", model="m", system_prompt=None,
            cwd=None, session_context=ctx,
        )
        sp = captured["system_prompt"]
        assert sp["type"] == "preset"
        assert "tester" in sp["append"]
        assert "Test rigorously" in sp["append"]

    def test_no_session_context_no_change(self, monkeypatch):
        from agentwire.sdk import build_options
        monkeypatch.setenv("AGENTWIRE_REPL_MCP", "0")
        monkeypatch.setenv("AGENTWIRE_REPL_DAMAGE_CONTROL", "0")

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        build_options(FakeOptions, mode="bypass", model="m", system_prompt=None, cwd=None)
        # No system_prompt key when nothing to append.
        assert "system_prompt" not in captured


class TestDamageControlAttachment:
    def test_hooks_attached_when_patterns_load(self, monkeypatch, tmp_path):
        # Point damage_control at a known patterns file
        from agentwire.sdk import damage_control
        patterns_file = tmp_path / "patterns.yaml"
        patterns_file.write_text(
            "bashToolPatterns:\n  - pattern: '\\brm\\s+-rf'\n    reason: rm -rf\n"
        )
        monkeypatch.setattr(damage_control, "DEFAULT_PATTERNS_PATH", patterns_file)
        monkeypatch.delenv("AGENTWIRE_REPL_DAMAGE_CONTROL", raising=False)
        monkeypatch.setenv("AGENTWIRE_REPL_MCP", "0")

        from agentwire.sdk import build_options

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        build_options(FakeOptions, mode="bypass", model="m", system_prompt=None, cwd=None)
        assert "hooks" in captured
        assert "PreToolUse" in captured["hooks"]
        matchers = captured["hooks"]["PreToolUse"]
        assert len(matchers) == 1
        assert "Bash" in matchers[0].matcher

    def test_no_hooks_when_disabled(self, monkeypatch, tmp_path):
        from agentwire.sdk import damage_control
        patterns_file = tmp_path / "patterns.yaml"
        patterns_file.write_text("bashToolPatterns: []\n")
        monkeypatch.setattr(damage_control, "DEFAULT_PATTERNS_PATH", patterns_file)
        monkeypatch.setenv("AGENTWIRE_REPL_DAMAGE_CONTROL", "0")
        monkeypatch.setenv("AGENTWIRE_REPL_MCP", "0")

        from agentwire.sdk import build_options

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        build_options(FakeOptions, mode="bypass", model="m", system_prompt=None, cwd=None)
        assert "hooks" not in captured

    def test_no_hooks_when_patterns_missing(self, monkeypatch, tmp_path):
        # Patterns file doesn't exist → make_pre_tool_hook returns None →
        # no hooks attached.
        from agentwire.sdk import damage_control
        monkeypatch.setattr(damage_control, "DEFAULT_PATTERNS_PATH", tmp_path / "nope.yaml")
        monkeypatch.delenv("AGENTWIRE_REPL_DAMAGE_CONTROL", raising=False)
        monkeypatch.setenv("AGENTWIRE_REPL_MCP", "0")

        from agentwire.sdk import build_options

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        build_options(FakeOptions, mode="bypass", model="m", system_prompt=None, cwd=None)
        assert "hooks" not in captured


class TestSdkErrorClassify:
    def test_transient_429(self):
        from agentwire.sdk.errors import classify
        assert classify("HTTPError", "got 429 rate_limit") == "transient"

    def test_auth_401(self):
        from agentwire.sdk.errors import classify
        assert classify("AuthError", "401 unauthorized") == "permanent"

    def test_invalid_400(self):
        from agentwire.sdk.errors import classify
        assert classify("ValidationError", "invalid_request: bad field") == "invalid"

    def test_generic(self):
        from agentwire.sdk.errors import classify
        assert classify("RuntimeError", "something broke") == "error"


class TestBuildOptionsThreadsKnobs:
    def test_effort_and_thinking_passed_through(self):
        from agentwire.sdk import build_options

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        build_options(
            FakeOptions, mode="bypass", model="m", system_prompt=None,
            cwd=None, effort="max", thinking_mode="summarized",
        )
        assert captured["effort"] == "max"
        assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}

    def test_can_use_tool_passed_through(self):
        from agentwire.sdk import build_options

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        async def cb(*args, **kwargs): pass
        build_options(
            FakeOptions, mode="prompted", model="m", system_prompt=None,
            cwd=None, can_use_tool=cb,
        )
        assert captured["can_use_tool"] is cb

    def test_can_use_tool_omitted_when_none(self):
        from agentwire.sdk import build_options

        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.allowed_tools = kwargs.get("allowed_tools", [])

        build_options(
            FakeOptions, mode="bypass", model="m", system_prompt=None,
            cwd=None,
        )
        assert "can_use_tool" not in captured


class TestFormatToolResult:
    def test_none(self):
        assert _format_tool_result(None) == "(no content)"

    def test_string(self):
        assert _format_tool_result("hello") == "hello"

    def test_newlines_flattened(self):
        assert _format_tool_result("a\nb\nc") == "a b c"

    def test_truncated(self):
        long = "x" * 200
        out = _format_tool_result(long)
        assert out.endswith("...")
        assert len(out) == 120

    def test_list_of_blocks(self):
        assert _format_tool_result([{"type": "text", "text": "from block"}]) == "from block"
