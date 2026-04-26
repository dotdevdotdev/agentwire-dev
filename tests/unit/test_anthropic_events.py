"""Tests for agentwire/sdk/events.py — SDK → pi JSONL translation.

Uses synthetic stand-ins that match claude-agent-sdk's Message shape by duck-type:
- has `type` attribute on content blocks
- has `content` attribute as list of blocks or string
- has other common attrs

This avoids importing claude-agent-sdk in a unit test (CI-safe).
"""

from __future__ import annotations

from types import SimpleNamespace

from agentwire.sdk import events as ev


# ---- Synthetic message builders ---------------------------------------------

def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id, name, input_):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input_)


def _tool_result_block(tool_use_id, content):
    return SimpleNamespace(type="tool_result", tool_use_id=tool_use_id, content=content)


def _thinking_block(text):
    return SimpleNamespace(type="thinking", thinking=text)


def _assistant(content, model="claude-opus-4-7"):
    return SimpleNamespace(content=content, model=model)


def _user_with_blocks(blocks):
    return SimpleNamespace(content=blocks)


def _user_text(text):
    return SimpleNamespace(content=text)


def _result(input_tokens, output_tokens, cost=0.0, is_error=False, session_id="s1"):
    return SimpleNamespace(
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        total_cost_usd=cost,
        is_error=is_error,
        session_id=session_id,
        duration_ms=100,
    )


def _system_init(session_id="sess-1", model="claude-opus-4-7"):
    return SimpleNamespace(
        subtype="init",
        data={"session_id": session_id, "model": model},
    )


# ---- Tests ------------------------------------------------------------------

class TestSystemInit:
    def test_emits_session_then_agent_start(self):
        events = ev.translate_system_init(_system_init("abc", "claude-opus-4-7"))
        assert [e["type"] for e in events] == ["session", "agent_start"]
        assert events[0]["session_id"] == "abc"
        assert events[1]["model"] == "claude-opus-4-7"


class TestAssistantMessage:
    def test_text_block_translated(self):
        msg = _assistant([_text_block("hello world")])
        event = ev.translate_assistant(msg)
        assert event["type"] == "message_end"
        assert event["message"]["role"] == "assistant"
        assert event["message"]["content"] == [{"type": "text", "text": "hello world"}]

    def test_tool_use_block_translated(self):
        msg = _assistant([_tool_use_block("t1", "Read", {"path": "/a"})])
        event = ev.translate_assistant(msg)
        content = event["message"]["content"]
        assert content == [{
            "type": "tool_use",
            "id": "t1",
            "name": "Read",
            "input": {"path": "/a"},
        }]

    def test_mixed_blocks_preserved_in_order(self):
        msg = _assistant([
            _text_block("first"),
            _tool_use_block("t1", "Bash", {"command": "ls"}),
            _text_block("second"),
        ])
        event = ev.translate_assistant(msg)
        types = [b["type"] for b in event["message"]["content"]]
        assert types == ["text", "tool_use", "text"]

    def test_thinking_block_preserved(self):
        msg = _assistant([_thinking_block("pondering...")])
        event = ev.translate_assistant(msg)
        assert event["message"]["content"] == [
            {"type": "thinking", "thinking": "pondering..."}
        ]


class TestUserMessage:
    def test_plain_string_content(self):
        msg = _user_text("hi there")
        event = ev.translate_user(msg)
        assert event["message"]["role"] == "user"
        assert event["message"]["content"] == [{"type": "text", "text": "hi there"}]

    def test_tool_result_preserved(self):
        msg = _user_with_blocks([_tool_result_block("t1", "file contents")])
        event = ev.translate_user(msg)
        assert event["message"]["content"] == [{
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": "file contents",
        }]


class TestResultMessage:
    def test_emits_turn_end_then_agent_end(self):
        events = ev.translate_result(_result(100, 50, cost=0.0))
        assert [e["type"] for e in events] == ["turn_end", "agent_end"]

    def test_tokens_preserved(self):
        events = ev.translate_result(_result(100, 50))
        turn_end = events[0]
        assert turn_end["usage"]["input"] == 100
        assert turn_end["usage"]["output"] == 50

    def test_cost_preserved(self):
        events = ev.translate_result(_result(100, 50, cost=0.025))
        assert events[0]["usage"]["cost"]["total"] == 0.025

    def test_subscription_zero_cost(self):
        events = ev.translate_result(_result(100, 50, cost=0.0))
        assert events[0]["usage"]["cost"]["total"] == 0.0


class TestFinalTextExtraction:
    def test_single_assistant_text(self):
        events = [ev.translate_assistant(_assistant([_text_block("the answer")]))]
        assert ev.extract_final_text_from_assistants(events) == "the answer"

    def test_multi_turn_text_concatenated(self):
        events = [
            ev.translate_assistant(_assistant([_text_block("first part ")])),
            ev.translate_assistant(_assistant([_text_block("second part")])),
        ]
        assert ev.extract_final_text_from_assistants(events) == "first part second part"

    def test_ignores_non_text_blocks(self):
        events = [
            ev.translate_assistant(_assistant([
                _thinking_block("reasoning"),
                _tool_use_block("t1", "Read", {}),
                _text_block("visible"),
            ])),
        ]
        assert ev.extract_final_text_from_assistants(events) == "visible"

    def test_ignores_user_messages(self):
        events = [
            ev.translate_user(_user_text("user said this")),
            ev.translate_assistant(_assistant([_text_block("assistant said this")])),
        ]
        assert ev.extract_final_text_from_assistants(events) == "assistant said this"


class TestToolCallExtraction:
    def test_collects_tool_uses(self):
        events = [ev.translate_assistant(_assistant([
            _tool_use_block("t1", "Read", {"path": "/a"}),
            _tool_use_block("t2", "Bash", {"command": "ls"}),
        ]))]
        calls = ev.extract_tool_calls(events)
        assert [c["name"] for c in calls] == ["Read", "Bash"]

    def test_dedup_by_id(self):
        # Same tool_use id appearing in multiple events → only one entry.
        msg = _assistant([_tool_use_block("t1", "Read", {"path": "/a"})])
        e = ev.translate_assistant(msg)
        calls = ev.extract_tool_calls([e, e, e])
        assert len(calls) == 1


class TestTokenAccumulation:
    def test_sums_across_turn_ends(self):
        events = [
            *ev.translate_result(_result(100, 50, cost=0.01)),
            *ev.translate_result(_result(30, 20, cost=0.005)),
        ]
        tokens = ev.extract_tokens_used(events)
        assert tokens["input"] == 130
        assert tokens["output"] == 70
        assert abs(tokens["cost"] - 0.015) < 1e-9

    def test_zero_events_zero_tokens(self):
        tokens = ev.extract_tokens_used([])
        assert tokens == {"input": 0, "output": 0, "cost": 0.0}
