"""Tests for the fan-out N-column view (agentwire/repl/views/fanout.py).

Covers boot, master-input fan-out semantics, per-column state isolation,
cancellation, and arg validation. SDK client construction is patched to
avoid hitting real auth — we substitute fake context-manager classes that
record their construction.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest

from agentwire.repl.views.fanout import (
    ColumnSdkEvent,
    ColumnTurnFinished,
    FanoutColumn,
    FanoutREPL,
    run_fanout_repl,
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Don't let real config / damage-control / mcp interfere with tests."""
    monkeypatch.setenv("AGENTWIRE_REPL_MCP", "0")
    monkeypatch.setenv("AGENTWIRE_REPL_DAMAGE_CONTROL", "0")
    monkeypatch.chdir(tmp_path)


class FakeClient:
    """Stand-in for ClaudeSDKClient.

    Records every query() call. receive_response() yields a single
    ResultMessage so a turn completes deterministically.
    """

    instances: list["FakeClient"] = []

    def __init__(self, options=None):
        self.options = options
        self.queries: list[str] = []
        self.entered = False
        self.exited = False
        FakeClient.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *args):
        self.exited = True

    async def query(self, text: str):
        self.queries.append(text)

    def receive_response(self):
        async def gen():
            yield SimpleNamespace(
                __class__=FakeResultMessage,
                usage={"input_tokens": 5, "output_tokens": 10},
                total_cost_usd=0.0042,
                duration_ms=100,
                is_error=False,
                result=None,
            )
        return gen()


class FakeAssistantMessage:
    pass


class FakeUserMessage:
    pass


class FakeSystemMessage:
    pass


class FakeResultMessage:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class FakeOptions:
    def __init__(self, **kw):
        self.kwargs = kw
        self.allowed_tools = kw.get("allowed_tools", [])


@pytest.fixture
def fake_sdk(monkeypatch):
    """Patch claude_agent_sdk so FanoutREPL.on_mount can construct clients
    without real auth."""
    FakeClient.instances = []
    fake_module = SimpleNamespace(
        ClaudeAgentOptions=FakeOptions,
        ClaudeSDKClient=FakeClient,
        AssistantMessage=FakeAssistantMessage,
        UserMessage=FakeUserMessage,
        SystemMessage=FakeSystemMessage,
        ResultMessage=FakeResultMessage,
    )
    import sys
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_module)
    return fake_module


# ---- FanoutColumn ----------------------------------------------------------


class TestFanoutColumn:
    def test_init_defaults(self):
        col = FanoutColumn(col=0, mode="bypass", model=None)
        assert col.col == 0
        assert col.mode == "bypass"
        assert col.model  # filled with DEFAULT_MODEL when None
        assert col.client is None
        assert col.input_tokens == 0
        assert col.output_tokens == 0
        assert col.cost_usd == 0.0
        assert col.turn_count == 0

    def test_explicit_model(self):
        col = FanoutColumn(col=2, mode="prompted", model="claude-sonnet-4-6")
        assert col.col == 2
        assert col.model == "claude-sonnet-4-6"
        assert col.mode == "prompted"


# ---- run_fanout_repl arg validation ---------------------------------------


class TestArgValidation:
    @pytest.mark.asyncio
    async def test_rejects_one_column(self):
        with pytest.raises(ValueError, match=">= 2"):
            await run_fanout_repl(mode="bypass", model=None, cols=1)

    @pytest.mark.asyncio
    async def test_rejects_zero_columns(self):
        with pytest.raises(ValueError, match=">= 2"):
            await run_fanout_repl(mode="bypass", model=None, cols=0)

    @pytest.mark.asyncio
    async def test_rejects_too_many_columns(self):
        with pytest.raises(ValueError, match="caps at 6"):
            await run_fanout_repl(mode="bypass", model=None, cols=7)


# ---- FanoutREPL boot + fan-out --------------------------------------------


class TestFanoutREPLBoot:
    @pytest.mark.asyncio
    async def test_compose_creates_n_columns(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model="claude-opus-4-7", cols=3)
        async with app.run_test() as pilot:
            assert len(app.columns) == 3
            for i in range(3):
                # Each column has its own RichLog widgets.
                assert app.query_one(f"#chat-{i}") is not None
                assert app.query_one(f"#action-{i}") is not None
                assert app.query_one(f"#status-{i}") is not None
            # Master input present.
            assert app.query_one("#master-input") is not None

    @pytest.mark.asyncio
    async def test_each_column_gets_own_client(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model="claude-opus-4-7", cols=3)
        async with app.run_test():
            # Three independent FakeClient instances were constructed.
            assert len(FakeClient.instances) == 3
            for col in app.columns:
                assert col.client is not None
                # Each client got entered (async context manager).
                assert col.client.entered is True

    @pytest.mark.asyncio
    async def test_columns_have_independent_stream_state(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=2)
        async with app.run_test():
            assert app.columns[0].stream_state is not app.columns[1].stream_state


class TestFanoutFanOut:
    @pytest.mark.asyncio
    async def test_master_input_queries_all_columns(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=3)
        async with app.run_test() as pilot:
            inp = app.query_one("#master-input")
            inp.value = "hello world"
            await pilot.press("enter")
            # Give workers a moment to pick up the queries.
            for _ in range(20):
                await pilot.pause()
                if all(c.client.queries for c in app.columns):
                    break
            for col in app.columns:
                assert col.client.queries == ["hello world"]

    @pytest.mark.asyncio
    async def test_empty_input_does_not_fan_out(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=2)
        async with app.run_test() as pilot:
            inp = app.query_one("#master-input")
            inp.value = ""
            await pilot.press("enter")
            await pilot.pause()
            for col in app.columns:
                assert col.client.queries == []

    @pytest.mark.asyncio
    async def test_turn_count_increments_per_column(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=2)
        async with app.run_test() as pilot:
            inp = app.query_one("#master-input")
            inp.value = "first"
            await pilot.press("enter")
            await pilot.pause()
            inp.value = "second"
            await pilot.press("enter")
            await pilot.pause()
            for col in app.columns:
                assert col.turn_count == 2


class TestColumnSdkEvent:
    def test_event_carries_column_index(self):
        e = ColumnSdkEvent(col=2, payload="x")
        assert e.col == 2
        assert e.payload == "x"

    def test_turn_finished_carries_error(self):
        e = ColumnTurnFinished(col=1, error="boom")
        assert e.col == 1
        assert e.error == "boom"

    def test_turn_finished_default_no_error(self):
        e = ColumnTurnFinished(col=0)
        assert e.error is None


# ---- cleanup --------------------------------------------------------------


class TestCleanup:
    @pytest.mark.asyncio
    async def test_unmount_exits_all_clients(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=3)
        async with app.run_test():
            captured = list(FakeClient.instances)
        # After the context manager exits, each client should have
        # had __aexit__ called.
        for client in captured:
            assert client.exited is True
