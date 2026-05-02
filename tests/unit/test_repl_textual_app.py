"""Phase 1B tests — Textual REPL skeleton.

Covers:
- _RichLogSink ANSI parsing
- App boots with mocked SDK
- TextArea submission routes through to /help
- Plain user turn fires the worker, sink renders SDK events

Phase 1C tests for persistence, mentions, prompted-mode, /clear lifecycle
arrive in the next PR.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# ---- _RichLogSink ANSI parsing --------------------------------------------


class TestRichLogSink:
    def test_writes_plain_text(self):
        from agentwire.sdk.sinks.textual import RichLogSink as _RichLogSink

        captured: list[Any] = []

        class _FakeLog:
            lines: list = []

            def write(self, x):
                captured.append(x)

            def refresh(self):
                pass

        sink = _RichLogSink(_FakeLog())
        sink.write("hello world\n")
        assert len(captured) == 1
        # Text.from_ansi returns a Rich Text instance; its .plain matches.
        assert captured[0].plain == "hello world"

    def test_buffers_until_newline(self):
        # No in-place line updates in Phase 1B — the sink buffers until \n,
        # then emits one complete line. Phase 2A's CurrentAction widget will
        # handle live in-place streaming.
        from agentwire.sdk.sinks.textual import RichLogSink as _RichLogSink

        captured: list[Any] = []

        class _FakeLog:
            lines: list = []

            def write(self, x):
                captured.append(x)

        sink = _RichLogSink(_FakeLog())
        sink.write("hello ")
        assert captured == []  # no newline yet, no emit
        sink.write("world\n")
        assert len(captured) == 1
        assert captured[0].plain == "hello world"

    def test_flush_emits_partial_buffer(self):
        # flush() emits whatever is in the buffer as a partial line — used
        # by the bullet-format renderer when a stream block hasn't seen a
        # newline yet but needs to land in the chat.
        from agentwire.sdk.sinks.textual import RichLogSink as _RichLogSink

        captured: list[Any] = []

        class _FakeLog:
            lines: list = []

            def write(self, x):
                captured.append(x)

        sink = _RichLogSink(_FakeLog())
        sink.write("- thinking: ")
        sink.flush()
        assert len(captured) == 1
        sink.write("first delta")
        sink.flush()
        assert len(captured) == 2

    def test_parses_ansi_escapes(self):
        from agentwire.sdk.sinks.textual import RichLogSink as _RichLogSink

        captured: list[Any] = []

        class _FakeLog:
            lines: list = []

            def write(self, x):
                captured.append(x)

            def refresh(self):
                pass

        sink = _RichLogSink(_FakeLog())
        sink.write("\x1b[1mbold\x1b[0m text\n")
        assert len(captured) == 1
        text = captured[0]
        assert text.plain == "bold text"
        # Rich Text spans: first 4 chars styled bold.
        spans = text.spans
        assert any("bold" in str(span.style).lower() for span in spans)

    def test_isatty_returns_true(self):
        # Required so _styled() in app.py emits ANSI codes that the sink parses.
        from agentwire.sdk.sinks.textual import RichLogSink as _RichLogSink

        class _FakeLog:
            lines: list = []

        sink = _RichLogSink(_FakeLog())
        assert sink.isatty() is True


# ---- Mock SDK ---------------------------------------------------------------


@dataclass
class _FakeOptions:
    """Mirrors the bits build_options inspects."""
    allowed_tools: list = field(default_factory=list)
    permission_mode: str = "bypassPermissions"


class _FakeAssistantMessage:
    def __init__(self, content):
        self.content = content


class _FakeUserMessage:
    def __init__(self, content):
        self.content = content


class _FakeSystemMessage:
    def __init__(self, subtype="init", data=None):
        self.subtype = subtype
        self.data = data or {}


class _FakeResultMessage:
    def __init__(self, *, total_cost_usd=0.0, duration_ms=0, usage=None, is_error=False, result=None):
        self.total_cost_usd = total_cost_usd
        self.duration_ms = duration_ms
        self.usage = usage or {}
        self.is_error = is_error
        self.result = result


class _FakeClient:
    """Mocks ClaudeSDKClient — async context manager + query/receive_response."""

    def __init__(self, options=None, **kwargs):
        self.options = options
        self._scripted: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, text: str) -> None:
        self._last_query = text

    def script(self, messages: list) -> None:
        self._scripted = list(messages)

    def receive_response(self):
        scripted = self._scripted
        self._scripted = []

        async def _gen():
            for msg in scripted:
                await asyncio.sleep(0)  # let other tasks see it
                yield msg

        return _gen()


@pytest.fixture
def patched_sdk(monkeypatch):
    """Patches `claude_agent_sdk` imports inside textual_app._open_session."""
    fakes = {
        "ClaudeAgentOptions": _FakeOptions,
        "ClaudeSDKClient": _FakeClient,
        "AssistantMessage": _FakeAssistantMessage,
        "UserMessage": _FakeUserMessage,
        "SystemMessage": _FakeSystemMessage,
        "ResultMessage": _FakeResultMessage,
    }

    # Build a fake module to satisfy `from claude_agent_sdk import ...`.
    import types
    fake_module = types.ModuleType("claude_agent_sdk")
    for name, cls in fakes.items():
        setattr(fake_module, name, cls)

    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", fake_module)

    # Also stub build_options since it imports from claude_agent_sdk and may
    # call methods on the real classes. We just need it to return an
    # object with an allowed_tools attribute.
    from agentwire.repl import textual_app

    def _fake_build_options(ClaudeAgentOptions, mode, model, system_prompt, **kwargs):
        return _FakeOptions(allowed_tools=["Read", "Bash", "Edit"])

    monkeypatch.setattr(textual_app, "build_options", _fake_build_options)
    return fakes


# ---- App boots --------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_boots_and_renders_banner(patched_sdk):
    from agentwire.repl.textual_app import AgentwireREPL

    app = AgentwireREPL(mode="bypass", model="claude-opus-4-7")
    async with app.run_test() as pilot:
        await pilot.pause()
        chat_lines = [
            line.text if hasattr(line, "text") else str(line)
            for line in app.query_one("#chat").lines
        ]
        all_text = " ".join(chat_lines)
        assert "agentwire repl" in all_text
        assert "claude-opus-4-7" in all_text


@pytest.mark.asyncio
async def test_help_command_writes_to_chat(patched_sdk):
    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.value = "/help"
        await inp.action_submit()
        await pilot.pause()
        chat_lines = [
            line.text if hasattr(line, "text") else str(line)
            for line in app.query_one("#chat").lines
        ]
        all_text = " ".join(chat_lines)
        assert "Available commands" in all_text or "help" in all_text.lower()


@pytest.mark.asyncio
async def test_user_turn_fires_worker_and_renders_sdk_events(patched_sdk):
    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()

        # Script the fake client to emit a SystemMessage(init) + ResultMessage.
        # Need to grab the actual instance the app opened.
        client = app._client
        assert isinstance(client, _FakeClient)
        client.script([
            _FakeSystemMessage(subtype="init", data={"model": "claude-opus-4-7", "session_id": "abc12345"}),
            _FakeResultMessage(total_cost_usd=0.0042, duration_ms=1500,
                               usage={"input_tokens": 10, "output_tokens": 5}),
        ])

        inp = app.query_one("#input", Input)
        inp.value = "hello"
        await inp.action_submit()

        # Let the worker run + post events back.
        for _ in range(20):
            await pilot.pause()

        chat_lines = [
            line.text if hasattr(line, "text") else str(line)
            for line in app.query_one("#chat").lines
        ]
        all_text = " ".join(chat_lines)
        assert "agent started" in all_text
        assert "claude-opus-4-7" in all_text
        # ResultMessage produces "[done · ...]"
        assert "done" in all_text


@pytest.mark.asyncio
async def test_user_turn_writes_transcript_event(patched_sdk, tmp_path, monkeypatch):
    # Persistence parity: each user turn writes a `user_input` event,
    # finalize() runs on unmount, and metadata.json/transcript.jsonl exist.
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        client = app._client
        assert isinstance(client, _FakeClient)
        client.script([
            _FakeSystemMessage(subtype="init", data={"model": "claude-opus-4-7", "session_id": "deadbeef"}),
            _FakeResultMessage(total_cost_usd=0.01, duration_ms=500,
                               usage={"input_tokens": 5, "output_tokens": 3}),
        ])

        inp = app.query_one("#input", Input)
        inp.value = "hello world"
        await inp.action_submit()
        for _ in range(20):
            await pilot.pause()

        session_dir = app.state.session_dir
        events_path = Path(session_dir) / "transcript.jsonl"
        assert events_path.exists()
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        kinds = [e.get("type") for e in events]
        assert "user_input" in kinds
        # Finalize happens on unmount — verify metadata.json exists after.
    metadata = Path(session_dir) / "metadata.json"
    assert metadata.exists()


@pytest.mark.asyncio
async def test_at_mention_expands_and_records(patched_sdk, tmp_path, monkeypatch):
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("hello mention\n")

    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        client = app._client
        client.script([_FakeResultMessage(total_cost_usd=0.0, duration_ms=10, usage={})])

        inp = app.query_one("#input", Input)
        inp.value = "summarize @notes.txt"
        await inp.action_submit()
        for _ in range(20):
            await pilot.pause()

        chat_lines = [
            line.text if hasattr(line, "text") else str(line)
            for line in app.query_one("#chat").lines
        ]
        all_text = " ".join(chat_lines)
        assert "expanded 1 mention" in all_text or "@notes.txt" in all_text

        events_path = Path(app.state.session_dir) / "transcript.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        user_events = [e for e in events if e.get("type") == "user_input"]
        assert len(user_events) == 1
        assert "mentions" in user_events[0]
        assert user_events[0]["mentions"][0]["raw"] == "@notes.txt"


@pytest.mark.asyncio
async def test_clear_writes_restart_event(patched_sdk, tmp_path, monkeypatch):
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        starting_restart_count = app.state.restart_count

        inp = app.query_one("#input", Input)
        inp.value = "/clear"
        await inp.action_submit()
        # Let the restart worker run.
        for _ in range(10):
            await pilot.pause()

        assert app.state.restart_count == starting_restart_count + 1
        events_path = Path(app.state.session_dir) / "transcript.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        kinds = [e.get("type") for e in events]
        assert "restart" in kinds


@pytest.mark.asyncio
async def test_permission_modal_dismisses_with_decision(patched_sdk, tmp_path, monkeypatch):
    # Phase 2C — permission prompts pop a centered ModalScreen rather than
    # parking an asyncio.Future on the app. Verify push_screen + dismiss
    # cycle works with the y/n/a key bindings.
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL, PermissionPrompt

    app = AgentwireREPL(mode="bypass")
    decisions: list = []

    def _capture(decision):
        decisions.append(decision)

    async with app.run_test() as pilot:
        await pilot.pause()
        modal = PermissionPrompt(tool_name="Bash", summary="ls -la")
        app.push_screen(modal, _capture)
        for _ in range(5):
            await pilot.pause()
        # The modal is up — pressing 'y' triggers action_decide('allow').
        await pilot.press("y")
        for _ in range(5):
            await pilot.pause()
        assert decisions == ["allow"]


@pytest.mark.asyncio
async def test_permission_modal_buttons(patched_sdk, tmp_path, monkeypatch):
    # Verify the three decision buttons each map to their decision string
    # via the on_button_pressed handler.
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL, PermissionPrompt

    app = AgentwireREPL(mode="bypass")
    decisions: list = []

    async with app.run_test() as pilot:
        await pilot.pause()
        for expected in ("allow", "deny", "always"):
            modal = PermissionPrompt(tool_name="Bash", summary="ls -la")
            app.push_screen(modal, decisions.append)
            for _ in range(5):
                await pilot.pause()
            # Click the button by id.
            await pilot.click(f"#{expected}")
            for _ in range(5):
                await pilot.pause()
        assert decisions == ["allow", "deny", "always"]


# TestActionSink + test_partials_route_to_action_pane +
# test_action_pane_cleared_on_result were removed when the CurrentAction
# pane was removed in the bullet/indent redesign — everything streams
# into the single chat sink now.


class TestStatusLine:
    """Phase 2B — running totals widget."""

    def test_pre_turn_format(self):
        from agentwire.repl.state import ReplState
        from agentwire.repl.textual_app import StatusLine

        line = StatusLine()
        # Patch update() to capture instead of needing a mounted app.
        captured: list = []
        line.update = lambda content="": captured.append(content)

        state = ReplState(mode="bypass", model="claude-opus-4-7", allowed_tools=[])
        line.refresh_from_state(state)
        assert captured
        text = captured[-1]
        assert "bypass" in text
        assert "claude-opus-4-7" in text
        assert "effort=high" in text
        assert "thinking=adaptive" in text

    def test_post_turn_format(self):
        from agentwire.repl.state import ReplState
        from agentwire.repl.textual_app import StatusLine

        line = StatusLine()
        captured: list = []
        line.update = lambda content="": captured.append(content)

        state = ReplState(mode="bypass", model="claude-opus-4-7", allowed_tools=[])
        state.turn_count = 3
        state.total_input_tokens = 100
        state.total_output_tokens = 250
        state.total_cost_usd = 0.0421
        line.refresh_from_state(state)
        text = captured[-1]
        assert "3 turns" in text
        assert "350 tok" in text
        assert "$0.0421" in text
        assert "100 in" in text and "250 out" in text

    def test_singular_turn(self):
        from agentwire.repl.state import ReplState
        from agentwire.repl.textual_app import StatusLine

        line = StatusLine()
        captured: list = []
        line.update = lambda content="": captured.append(content)

        state = ReplState(mode="bypass", model="x", allowed_tools=[])
        state.turn_count = 1
        line.refresh_from_state(state)
        assert "1 turn" in captured[-1]
        assert "1 turns" not in captured[-1]


@pytest.mark.asyncio
async def test_status_line_refreshes_on_result(patched_sdk, tmp_path, monkeypatch):
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL, StatusLine
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass", model="claude-opus-4-7")
    async with app.run_test() as pilot:
        await pilot.pause()
        client = app._client
        client.script([
            _FakeResultMessage(
                total_cost_usd=0.05,
                duration_ms=1000,
                usage={"input_tokens": 10, "output_tokens": 20},
            ),
        ])
        inp = app.query_one("#input", Input)
        inp.value = "hi"
        await inp.action_submit()
        for _ in range(20):
            await pilot.pause()

        status = app.query_one("#status", StatusLine)
        # Static.update() stores content in private state; query via render().
        rendered = status.render()
        text = str(rendered) if rendered is not None else ""
        assert "1 turn" in text
        assert "$0.0500" in text


@pytest.mark.asyncio
async def test_header_title_set(patched_sdk):
    from agentwire.repl.textual_app import AgentwireREPL

    app = AgentwireREPL(mode="bypass", model="claude-opus-4-7")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.title == "agentwire repl"
        assert "bypass" in app.sub_title
        assert "opus-4-7" in app.sub_title


@pytest.mark.asyncio
async def test_layout_command_announces_chat_only(patched_sdk, tmp_path, monkeypatch):
    """`/layout` is a no-op stub now — there's only one pane to size."""
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input, RichLog

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.value = "/layout"
        await inp.action_submit()
        await pilot.pause()

        chat_lines = [
            line.text if hasattr(line, "text") else str(line)
            for line in app.query_one("#chat", RichLog).lines
        ]
        all_text = " ".join(chat_lines)
        assert "chat-only layout" in all_text


@pytest.mark.asyncio
async def test_theme_no_args_shows_current_and_available(patched_sdk, tmp_path, monkeypatch):
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input, RichLog

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.value = "/theme"
        await inp.action_submit()
        await pilot.pause()

        chat_lines = [
            line.text if hasattr(line, "text") else str(line)
            for line in app.query_one("#chat", RichLog).lines
        ]
        all_text = " ".join(chat_lines)
        assert "theme:" in all_text
        assert "available:" in all_text


@pytest.mark.asyncio
async def test_theme_switch(patched_sdk, tmp_path, monkeypatch):
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input, RichLog

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Pick a theme that's likely available across textual versions.
        themes = app._available_themes()
        target = "textual-light" if "textual-light" in themes else themes[0]
        inp = app.query_one("#input", Input)
        inp.value = f"/theme {target}"
        await inp.action_submit()
        await pilot.pause()

        chat_lines = [
            line.text if hasattr(line, "text") else str(line)
            for line in app.query_one("#chat", RichLog).lines
        ]
        all_text = " ".join(chat_lines)
        assert "theme set" in all_text or "theme error" in all_text


@pytest.mark.parametrize(
    "text,cursor,expected",
    [
        # Detects after space
        ("summarize @notes", len("summarize @notes"), "notes"),
        # Detects at start of input
        ("@README", len("@README"), "README"),
        # `foo@bar.com` — @ not preceded by whitespace, must not trigger
        ("foo@bar.com", len("foo@bar.com"), None),
        # No @ at all
        ("just text", 9, None),
        # @prefix terminated by whitespace before cursor
        ("@notes hello", len("@notes hello"), None),
        # Cursor mid-prefix: "look at @REA" — text up to cursor is 12 chars
        ("look at @REA and stuff", 12, "REA"),
    ],
)
def test_current_at_prefix(text, cursor, expected):
    from agentwire.repl.textual_app import AgentwireREPL
    assert AgentwireREPL._current_at_prefix(text, cursor) == expected


# test_at_mention_preview_shows_in_action_pane removed: the live mention
# preview lived in the CurrentAction pane, which is gone post-redesign.
# Tab completion (test_tab_completes_mention below) still works.


@pytest.mark.asyncio
async def test_tab_completes_mention(patched_sdk, tmp_path, monkeypatch):
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("readme")

    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.value = "look at @REA"
        inp.cursor_position = len(inp.value)
        app.action_complete_mention()
        await pilot.pause()

        assert "@README.md" in inp.value


@pytest.mark.parametrize(
    "name,summary,query,expected_score",
    [
        # Empty query matches everything (score 0 = best)
        ("/help", "Show help", "", 0),
        # Prefix match on name
        ("/cost", "Show cost", "co", 0),
        # Substring in name (not prefix)
        ("/help", "Show help", "lp", 1),
        # Substring in summary only
        ("/save", "Show transcript path", "trans", 2),
        # No match
        ("/help", "Show help", "xyzzy", -1),
        # Leading slash in query is normalized away
        ("/cost", "Show cost", "/co", 0),
    ],
)
def test_command_palette_fuzzy_score(name, summary, query, expected_score):
    from agentwire.repl.textual_app import CommandPalette
    assert CommandPalette._fuzzy_score(name, summary, query) == expected_score


@pytest.mark.asyncio
async def test_command_palette_opens_and_filters(patched_sdk, tmp_path, monkeypatch):
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL, CommandPalette
    from textual.widgets import Input, OptionList

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()

        # Open palette via the action.
        app.action_agentwire_palette()
        for _ in range(5):
            await pilot.pause()

        # Find the palette modal in the screen stack.
        palette = next(
            (s for s in app.screen_stack if isinstance(s, CommandPalette)),
            None,
        )
        assert palette is not None

        # Filter to commands containing "cost".
        palette_input = palette.query_one("#palette-input", Input)
        palette_input.value = "cost"
        palette._refilter("cost")
        await pilot.pause()

        olist = palette.query_one("#palette-list", OptionList)
        assert olist.option_count >= 1


@pytest.mark.asyncio
async def test_command_palette_dismiss_writes_to_input(
    patched_sdk, tmp_path, monkeypatch
):
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL, CommandPalette
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()

        app.action_agentwire_palette()
        for _ in range(5):
            await pilot.pause()

        palette = next(
            (s for s in app.screen_stack if isinstance(s, CommandPalette)),
            None,
        )
        assert palette is not None
        # Dismiss with a known command name.
        palette.dismiss("/help")
        for _ in range(5):
            await pilot.pause()

        inp = app.query_one("#input", Input)
        assert inp.value.startswith("/help")


class TestSparkline:
    """Phase 3D — unicode-block sparkline."""

    def test_empty_returns_empty_string(self):
        from agentwire.repl.textual_app import _sparkline
        assert _sparkline([]) == ""

    def test_all_zeros_baseline(self):
        from agentwire.repl.textual_app import _sparkline
        out = _sparkline([0, 0, 0])
        assert len(out) == 3
        # First bar is the baseline.
        assert all(c == "▁" for c in out)

    def test_increasing_series(self):
        from agentwire.repl.textual_app import _sparkline
        out = _sparkline([0.1, 0.5, 1.0])
        # Last char is the peak block.
        assert out[-1] == "█"
        # All chars are members of the bar set.
        assert all(c in "▁▂▃▄▅▆▇█" for c in out)


@pytest.mark.asyncio
async def test_status_line_shows_sparkline_after_turns(
    patched_sdk, tmp_path, monkeypatch
):
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL, StatusLine
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        client = app._client
        client.script([
            _FakeResultMessage(
                total_cost_usd=0.05,
                duration_ms=1000,
                usage={"input_tokens": 10, "output_tokens": 20},
            ),
        ])
        inp = app.query_one("#input", Input)
        inp.value = "trigger"
        await inp.action_submit()
        for _ in range(20):
            await pilot.pause()

        status = app.query_one("#status", StatusLine)
        text = str(status.render())
        # After one turn, the sparkline (one bar) is in the status line.
        assert any(b in text for b in "▁▂▃▄▅▆▇█")


@pytest.mark.asyncio
async def test_scrub_opens_modal(patched_sdk, tmp_path, monkeypatch):
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL, TranscriptScrubber
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Add a couple of fake events to the transcript file so the scrubber
        # has something to show.
        import json as _json
        path = Path(app.state.session_dir) / "transcript.jsonl"
        with path.open("a") as f:
            f.write(_json.dumps({"type": "user_input", "text": "hello"}) + "\n")
            f.write(_json.dumps({"type": "user_input", "text": "world"}) + "\n")

        inp = app.query_one("#input", Input)
        inp.value = "/scrub"
        await inp.action_submit()
        for _ in range(5):
            await pilot.pause()

        scrubber = next(
            (s for s in app.screen_stack if isinstance(s, TranscriptScrubber)),
            None,
        )
        assert scrubber is not None
        # The OptionList should have at least 2 options (the two user_inputs).
        from textual.widgets import OptionList
        olist = scrubber.query_one("#scrub-list", OptionList)
        assert olist.option_count >= 2


class TestAgentwireBrandTheme:
    """Phase: brand theme — neon green primary + neon cyan accent on flat black."""

    def test_default_palette_matches_brand(self):
        from agentwire.repl.textual_app import build_agentwire_theme

        theme = build_agentwire_theme()
        assert theme.name == "agentwire"
        assert theme.dark is True
        assert theme.primary == "#00ff88"
        assert theme.secondary == "#00d4ff"
        assert theme.accent == "#00d4ff"
        assert theme.foreground == "#e2e8f0"
        assert theme.background == "#000000"
        assert theme.surface == "#0a0a0a"
        assert theme.success == "#00ff88"
        assert theme.warning == "#fbbf24"
        assert theme.error == "#dc2626"

    def test_palette_overrides_apply(self):
        from agentwire.repl.textual_app import build_agentwire_theme

        theme = build_agentwire_theme({
            "primary": "#ff00aa",
            "background": "#0d0d2a",
        })
        assert theme.primary == "#ff00aa"
        assert theme.background == "#0d0d2a"
        # Other palette keys keep their brand defaults.
        assert theme.secondary == "#00d4ff"
        assert theme.foreground == "#e2e8f0"

    def test_variable_overrides_apply(self):
        # Keys outside the palette (e.g. header-foreground) go to variables.
        from agentwire.repl.textual_app import build_agentwire_theme

        theme = build_agentwire_theme({
            "header-foreground": "#ff00ff",
        })
        assert theme.variables["header-foreground"] == "#ff00ff"

    def test_empty_overrides_keep_defaults(self):
        from agentwire.repl.textual_app import build_agentwire_theme

        theme = build_agentwire_theme({"primary": ""})
        # Empty string treated as no-op — brand default preserved.
        assert theme.primary == "#00ff88"


def test_repl_config_loads_theme_overrides(tmp_path, monkeypatch):
    """Config plumbing: repl.theme.* in YAML round-trips into Config.repl.theme."""
    from agentwire import config as cfg_mod

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "repl:\n"
        "  theme:\n"
        '    primary: "#ff00aa"\n'
        '    background: "#0d0d2a"\n'
    )
    cfg = cfg_mod.load_config(yaml_path)
    assert cfg.repl.theme["primary"] == "#ff00aa"
    assert cfg.repl.theme["background"] == "#0d0d2a"


def test_repl_config_default_theme_is_empty(tmp_path):
    """No `repl.theme` in YAML → empty dict, app falls back to brand defaults."""
    from agentwire import config as cfg_mod

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("server:\n  port: 8765\n")
    cfg = cfg_mod.load_config(yaml_path)
    assert cfg.repl.theme == {}


@pytest.mark.asyncio
async def test_exit_command_quits_app(patched_sdk):
    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.value = "/exit"
        await inp.action_submit()
        await pilot.pause()
        # After /exit, the app exits — return_code is set.
        assert app.return_code == 0 or not app.is_running


@pytest.mark.asyncio
async def test_result_with_error_sets_exit_code(patched_sdk, tmp_path, monkeypatch):
    # Parity with legacy REPL: a ResultMessage carrying is_error=True should
    # set exit_code=1 so the process returns non-zero.
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        client = app._client
        client.script([
            _FakeResultMessage(total_cost_usd=0.0, duration_ms=10, usage={},
                               is_error=True, result="boom"),
        ])
        inp = app.query_one("#input", Input)
        inp.value = "trigger error"
        await inp.action_submit()
        for _ in range(20):
            await pilot.pause()
        assert app._exit_code == 1


@pytest.mark.asyncio
async def test_seed_message_fires_first_turn(patched_sdk, tmp_path, monkeypatch):
    # Workflow human_gate runners pass seed_message — it should fire as the
    # first turn after on_mount instead of waiting for user input.
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL

    app = AgentwireREPL(mode="bypass", seed_message="hello from seed")
    async with app.run_test() as pilot:
        await pilot.pause()
        client = app._client
        # Worker fires immediately on mount because seed_message is set.
        # The worker calls client.query — verify it received the seed text.
        for _ in range(10):
            await pilot.pause()
            if getattr(client, "_last_query", None) == "hello from seed":
                break
        assert client._last_query == "hello from seed"


@pytest.mark.asyncio
async def test_unmount_finalizes_transcript_with_running_totals(
    patched_sdk, tmp_path, monkeypatch
):
    # metadata.json on unmount should reflect cost/token totals from any
    # ResultMessages tracked during the session.
    from agentwire.repl import persistence
    monkeypatch.setattr(persistence, "DEFAULT_REPL_HOME", tmp_path / "repl")
    from agentwire.repl.textual_app import AgentwireREPL
    from textual.widgets import Input

    app = AgentwireREPL(mode="bypass")
    async with app.run_test() as pilot:
        await pilot.pause()
        client = app._client
        client.script([
            _FakeResultMessage(
                total_cost_usd=0.5,
                duration_ms=2000,
                usage={"input_tokens": 100, "output_tokens": 200},
            ),
        ])
        inp = app.query_one("#input", Input)
        inp.value = "go"
        await inp.action_submit()
        for _ in range(20):
            await pilot.pause()
        session_dir = app.state.session_dir

    # After context exit (on_unmount fired): metadata reflects totals.
    metadata = json.loads((Path(session_dir) / "metadata.json").read_text())
    assert metadata["turn_count"] >= 1
    assert metadata["total_cost_usd"] >= 0.5
