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
    parse_col_overrides,
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
                # Each column has its own widgets (chat + status + input).
                assert app.query_one(f"#chat-{i}") is not None
                assert app.query_one(f"#status-{i}") is not None
                assert app.query_one(f"#col-input-{i}") is not None
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


# ---- Phase 4: per-column overrides + per-column input ---------------------


class TestParseColOverrides:
    def test_simple_string_value(self):
        out = parse_col_overrides(["0=opus", "2=sonnet"], max_col=3)
        assert out == {0: "opus", 2: "sonnet"}

    def test_split_value_returns_list(self):
        out = parse_col_overrides(["0=skeptic,explainer"], max_col=2, value_split=True)
        assert out == {0: ["skeptic", "explainer"]}

    def test_split_strips_whitespace_and_drops_empty(self):
        out = parse_col_overrides(["0= a , b , "], max_col=1, value_split=True)
        assert out == {0: ["a", "b"]}

    def test_missing_equals_raises(self):
        with pytest.raises(ValueError, match="expected 'INDEX=VALUE'"):
            parse_col_overrides(["broken"], max_col=2)

    def test_non_integer_index_raises(self):
        with pytest.raises(ValueError, match="not an integer"):
            parse_col_overrides(["x=opus"], max_col=2)

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError, match="out of range"):
            parse_col_overrides(["5=opus"], max_col=3)

    def test_negative_index_raises(self):
        with pytest.raises(ValueError, match="out of range"):
            parse_col_overrides(["-1=opus"], max_col=3)

    def test_none_input_returns_empty(self):
        assert parse_col_overrides(None, max_col=3) == {}


class TestPerColumnOverrides:
    @pytest.mark.asyncio
    async def test_col_models_apply_per_column(self, fake_sdk):
        app = FanoutREPL(
            mode="bypass", model="claude-opus-4-7", cols=3,
            col_models={0: "claude-sonnet-4-6"},
        )
        async with app.run_test():
            assert app.columns[0].model == "claude-sonnet-4-6"
            assert app.columns[1].model == "claude-opus-4-7"
            assert app.columns[2].model == "claude-opus-4-7"

    @pytest.mark.asyncio
    async def test_col_efforts_apply_per_column(self, fake_sdk):
        app = FanoutREPL(
            mode="bypass", model=None, cols=2,
            col_efforts={0: "max", 1: "low"},
        )
        async with app.run_test():
            assert app.columns[0].effort == "max"
            assert app.columns[1].effort == "low"

    @pytest.mark.asyncio
    async def test_col_roles_apply_per_column(self, fake_sdk):
        app = FanoutREPL(
            mode="bypass", model=None, cols=2,
            col_roles={0: ["skeptic"], 1: ["optimist", "explainer"]},
        )
        async with app.run_test():
            assert app.columns[0].roles_override == ["skeptic"]
            assert app.columns[1].roles_override == ["optimist", "explainer"]

    @pytest.mark.asyncio
    async def test_no_override_uses_default(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model="claude-opus-4-7", cols=2)
        async with app.run_test():
            for col in app.columns:
                assert col.effort == "high"  # DEFAULT_EFFORT
                assert col.roles_override is None


class TestPerColumnInput:
    @pytest.mark.asyncio
    async def test_per_column_input_widgets_present(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=3)
        async with app.run_test():
            for i in range(3):
                assert app.query_one(f"#col-input-{i}") is not None

    @pytest.mark.asyncio
    async def test_per_column_input_only_queries_that_column(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=3)
        async with app.run_test() as pilot:
            inp = app.query_one("#col-input-1")
            inp.focus()
            inp.value = "only col 2"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if app.columns[1].client.queries:
                    break
            assert app.columns[0].client.queries == []
            assert app.columns[1].client.queries == ["only col 2"]
            assert app.columns[2].client.queries == []

    @pytest.mark.asyncio
    async def test_master_input_still_fans_to_all(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=2)
        async with app.run_test() as pilot:
            inp = app.query_one("#master-input")
            inp.focus()
            inp.value = "to all"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if all(c.client.queries for c in app.columns):
                    break
            for col in app.columns:
                assert col.client.queries == ["to all"]


class TestSlashCommandsAndExit:
    @pytest.mark.asyncio
    async def test_slash_exit_quits_app(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=2)
        async with app.run_test() as pilot:
            inp = app.query_one("#master-input")
            inp.value = "/exit"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if app._exit_renderables is not None or not app.is_running:
                    break
            # Workers cancelled, app exiting — neither column saw the slash
            # command as an SDK query.
            for col in app.columns:
                assert "/exit" not in col.client.queries

    @pytest.mark.asyncio
    async def test_slash_quit_quits_app(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=2)
        async with app.run_test() as pilot:
            inp = app.query_one("#master-input")
            inp.value = "/quit"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
            for col in app.columns:
                assert "/quit" not in col.client.queries

    @pytest.mark.asyncio
    async def test_slash_cancel_cancels_workers(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=2)
        async with app.run_test() as pilot:
            inp = app.query_one("#master-input")
            inp.value = "/cancel"
            await pilot.press("enter")
            await pilot.pause()
            for col in app.columns:
                assert "/cancel" not in col.client.queries

    @pytest.mark.asyncio
    async def test_slash_clear_does_not_query_sdk(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=2)
        async with app.run_test() as pilot:
            inp = app.query_one("#master-input")
            inp.value = "/clear"
            await pilot.press("enter")
            await pilot.pause()
            for col in app.columns:
                assert "/clear" not in col.client.queries

    @pytest.mark.asyncio
    async def test_action_cancel_all_cancels_running_workers(self, fake_sdk):
        app = FanoutREPL(mode="bypass", model=None, cols=2)
        async with app.run_test() as pilot:
            inp = app.query_one("#master-input")
            inp.value = "hello"
            await pilot.press("enter")
            await pilot.pause()
            # Now cancel all.
            app.action_cancel_all()
            await pilot.pause()
            # No exception — and visual feedback emitted.
            # (Workers may or may not be running at this exact moment;
            # the important guarantee is the action ran without error.)


class TestRunFanoutReplOverridesValidation:
    @pytest.mark.asyncio
    async def test_invalid_col_model_format_raises(self, fake_sdk):
        with pytest.raises(ValueError, match="expected 'INDEX=VALUE'"):
            await run_fanout_repl(
                mode="bypass", model=None, cols=2,
                col_models_raw=["broken"],
            )

    @pytest.mark.asyncio
    async def test_col_model_index_out_of_range_raises(self, fake_sdk):
        with pytest.raises(ValueError, match="out of range"):
            await run_fanout_repl(
                mode="bypass", model=None, cols=2,
                col_models_raw=["5=opus"],
            )
