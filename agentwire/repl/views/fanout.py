"""Fan-out N-column view for the Textual REPL.

Layout: master input docked at the bottom, N independent columns above.
Each column is its own SDK conversation — own ClaudeSDKClient, own
StreamRenderState, own RichLog widgets. When the user types in the master
input, the same prompt fans out to all N clients in parallel; each column
streams its own response independently.

Use case: multi-generation A/B prompting. Run the same prompt across N
columns with different models / different roles / different effort, then
pick the best output. Per-column overrides land in a follow-on PR.

This is the first composite view built on `agentwire.sdk.*` — it
validates that the primitives are actually reusable. See
docs/missions/agentwire-sdk-primitives.md Phase 2.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Footer, Header, Input, RichLog, Static

from agentwire.repl.context import load_session_context
from agentwire.sdk import (
    HEARTBEAT,
    DEFAULT_MODEL,
    StreamRenderState,
    build_options,
    heartbeat_iter,
    render_message,
)
from agentwire.sdk.sinks.textual import ActionSink, RichLogSink


class ColumnSdkEvent(Message):
    """One SDK message (or HEARTBEAT) tagged with the column it belongs to."""

    def __init__(self, col: int, payload: Any) -> None:
        self.col = col
        self.payload = payload
        super().__init__()


class ColumnTurnFinished(Message):
    """Worker for column N signals turn complete (or cancelled)."""

    def __init__(self, col: int, error: str | None = None) -> None:
        self.col = col
        self.error = error
        super().__init__()


class FanoutColumn:
    """One column's per-conversation state.

    Mirrors what `AgentwireREPL` keeps in fields, but scoped to one of N
    columns: independent SDK client, independent stream state, independent
    sinks bound to its own RichLog widgets, independent running totals.
    """

    def __init__(self, col: int, mode: str, model: str | None) -> None:
        self.col = col
        self.mode = mode
        self.model = model or DEFAULT_MODEL
        self.client: Any = None
        self.sdk_classes: dict[str, Any] | None = None
        self.chat_sink: RichLogSink | None = None
        self.action_sink: ActionSink | None = None
        self.stream_state = StreamRenderState()
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.turn_count = 0


class FanoutStatusLine(Static):
    """Per-column running totals: cost / tokens / turn count."""

    DEFAULT_CSS = """
    FanoutStatusLine {
        height: 1;
        padding: 0 1;
        background: $background;
        color: $foreground;
    }
    """


class FanoutREPL(App):
    """Textual app: master input fans out to N independent SDK clients.

    Each column has its own ChatLog (finalized turns), CurrentAction
    (live partial-stream), and StatusLine. Typing in the master input
    fires one `query()` per column in parallel — they stream concurrently,
    and the user picks the winner by reading.
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_all", "Cancel all turns", priority=True),
        Binding("ctrl+d", "quit", "Exit"),
    ]

    DEFAULT_CSS = """
    Screen {
        background: $background;
    }

    Header {
        dock: top;
        background: $background;
        color: $primary;
    }
    HeaderTitle { background: $background; color: $primary; }
    HeaderIcon { background: $background; }

    #columns {
        height: 1fr;
        background: $background;
    }

    .column {
        width: 1fr;
        height: 1fr;
        background: $background;
    }

    .col-chat {
        height: 3fr;
        border: tall $primary;
        border-title-color: $primary;
        padding: 0 1;
        background: $background;
        scrollbar-color: $primary $background;
    }

    .col-action {
        height: 1fr;
        border: tall $secondary;
        border-title-color: $secondary;
        padding: 0 1;
        background: $background;
        scrollbar-color: $secondary $background;
    }

    .col-status {
        height: 1;
        padding: 0 1;
        background: $background;
        color: $secondary;
    }

    Input {
        dock: bottom;
        height: 3;
        border: tall $primary;
        border-title-color: $primary;
        background: $background;
        background-tint: $background;
    }
    Input:focus {
        background: $background;
        background-tint: $background;
        border: tall $primary;
    }

    Footer { dock: bottom; background: $background; }
    """

    def __init__(self, *, mode: str, model: str | None, cols: int, system_prompt: str | None = None, roles: list[str] | None = None) -> None:
        super().__init__()
        self.mode = mode
        self.system_prompt = system_prompt
        self.roles = roles
        self.title = f"agentwire repl — fanout · {cols} cols · {mode}"
        self.columns: list[FanoutColumn] = [
            FanoutColumn(col=i, mode=mode, model=model) for i in range(cols)
        ]
        self._ctx: Any = None

    def compose(self) -> ComposeResult:
        # Apply the agentwire brand theme — same one the chat view uses.
        from agentwire.repl.textual_app import build_agentwire_theme
        try:
            from agentwire.config import load_config
            cfg = load_config()
            overrides = getattr(cfg, "repl", None)
            theme_overrides = getattr(overrides, "theme", {}) if overrides else {}
        except Exception:
            theme_overrides = {}
        self.register_theme(build_agentwire_theme(theme_overrides))
        self.theme = "agentwire"

        yield Header()
        with Horizontal(id="columns"):
            for col in self.columns:
                with Vertical(classes="column", id=f"col-{col.col}"):
                    chat = RichLog(id=f"chat-{col.col}", classes="col-chat", wrap=True, markup=False)
                    chat.border_title = f"col {col.col + 1} · {col.model}"
                    yield chat
                    action = RichLog(id=f"action-{col.col}", classes="col-action", wrap=True, markup=False)
                    action.border_title = "current action"
                    yield action
                    yield FanoutStatusLine(self._format_status(col), classes="col-status", id=f"status-{col.col}")
        yield Input(id="master-input", placeholder="> fan-out to all columns")
        yield Footer()

    async def on_mount(self) -> None:
        # Bind sinks to the just-mounted RichLogs.
        for col in self.columns:
            chat = self.query_one(f"#chat-{col.col}", RichLog)
            action = self.query_one(f"#action-{col.col}", RichLog)
            col.chat_sink = RichLogSink(chat)
            col.action_sink = ActionSink(action)

        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKClient,
                ResultMessage,
                SystemMessage,
                UserMessage,
            )
        except ImportError as e:
            self.exit(message=f"claude-agent-sdk not installed: {e}")
            return

        sdk_classes = {
            "AssistantMessage": AssistantMessage,
            "UserMessage": UserMessage,
            "SystemMessage": SystemMessage,
            "ResultMessage": ResultMessage,
        }
        self._ctx = load_session_context(Path.cwd(), role_overrides=self.roles)

        # Open one SDK client per column.
        for col in self.columns:
            col.sdk_classes = sdk_classes
            options = build_options(
                ClaudeAgentOptions,
                col.mode,
                col.model,
                self.system_prompt,
                cwd=Path.cwd(),
                session_context=self._ctx,
            )
            col.client = ClaudeSDKClient(options=options)
            await col.client.__aenter__()

        # Banner inside each column so the user sees what each column is.
        for col in self.columns:
            assert col.chat_sink is not None
            col.chat_sink.write(
                f"[col {col.col + 1} · {col.model} · {col.mode} · ready]\n"
            )
            col.chat_sink.flush()

        self.query_one("#master-input", Input).focus()

    async def on_unmount(self) -> None:
        # Cleanly close every SDK client when the app exits.
        for col in self.columns:
            if col.client is not None:
                try:
                    await col.client.__aexit__(None, None, None)
                except Exception:
                    pass
                col.client = None

    # ------ master input → fan-out ------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        if not text:
            return
        event.input.value = ""
        # Echo the user turn into every column's chat (so each column has
        # the same context visible).
        for col in self.columns:
            assert col.chat_sink is not None
            col.chat_sink.write(f"\n> {text}\n")
            col.chat_sink.flush()
            col.turn_count += 1
            self._refresh_status(col)
        # Fire one worker per column.
        for col in self.columns:
            self.run_worker(
                self._run_turn(col, text),
                exclusive=False,
                group=f"col-{col.col}",
                name=f"col-{col.col}-turn",
            )

    async def _run_turn(self, col: FanoutColumn, text: str) -> None:
        if col.client is None:
            return
        try:
            await col.client.query(text)
            async for message in heartbeat_iter(
                col.client.receive_response(), idle_timeout=5.0
            ):
                self.post_message(ColumnSdkEvent(col.col, message))
        except asyncio.CancelledError:
            self.post_message(ColumnTurnFinished(col.col, error="cancelled"))
            raise
        except Exception as exc:
            self.post_message(
                ColumnTurnFinished(col.col, error=f"{type(exc).__name__}: {exc}")
            )
            return
        self.post_message(ColumnTurnFinished(col.col))

    # ------ rendering on UI thread ------

    def on_column_sdk_event(self, event: ColumnSdkEvent) -> None:
        col = self.columns[event.col]
        if col.chat_sink is None or col.action_sink is None or col.sdk_classes is None:
            return
        msg = event.payload
        if msg is HEARTBEAT:
            col.stream_state.heartbeat(col.action_sink)
            col.action_sink.flush()
            return
        try:
            render_message(
                msg,
                AssistantMessage=col.sdk_classes["AssistantMessage"],
                UserMessage=col.sdk_classes["UserMessage"],
                SystemMessage=col.sdk_classes["SystemMessage"],
                ResultMessage=col.sdk_classes["ResultMessage"],
                out=col.chat_sink,
                action_out=col.action_sink,
                stream_state=col.stream_state,
            )
            col.chat_sink.flush()
            col.action_sink.flush()
            ResultMessage = col.sdk_classes["ResultMessage"]
            if isinstance(msg, ResultMessage):
                col.action_sink.clear()
                usage = getattr(msg, "usage", {}) or {}
                if isinstance(usage, dict):
                    col.input_tokens += usage.get("input_tokens", 0) or 0
                    col.output_tokens += usage.get("output_tokens", 0) or 0
                cost = getattr(msg, "total_cost_usd", None)
                if cost:
                    col.cost_usd += float(cost)
                self._refresh_status(col)
        except Exception:
            pass  # don't kill the whole app on render glitch

    def on_column_turn_finished(self, event: ColumnTurnFinished) -> None:
        col = self.columns[event.col]
        if event.error and col.chat_sink is not None:
            col.chat_sink.write(f"[col {col.col + 1} · {event.error}]\n")
            col.chat_sink.flush()

    # ------ cancellation ------

    def action_cancel_all(self) -> None:
        """Master Ctrl+C — cancel every running column turn."""
        for w in self.workers:
            if w.name and w.name.endswith("-turn") and w.is_running:
                w.cancel()

    # ------ status line ------

    def _format_status(self, col: FanoutColumn) -> str:
        return (
            f"col {col.col + 1} · {col.turn_count} turns · "
            f"{col.input_tokens + col.output_tokens} tok · "
            f"${col.cost_usd:.4f}"
        )

    def _refresh_status(self, col: FanoutColumn) -> None:
        try:
            line = self.query_one(f"#status-{col.col}", FanoutStatusLine)
            line.update(self._format_status(col))
        except Exception:
            pass


async def run_fanout_repl(
    *,
    mode: str,
    model: str | None,
    cols: int,
    system_prompt: str | None = None,
    roles: list[str] | None = None,
) -> int:
    """Entry point invoked from `agentwire repl --view fanout --cols N`."""
    if cols < 2:
        raise ValueError("fanout requires --cols >= 2")
    if cols > 6:
        raise ValueError("fanout caps at 6 columns; pick fewer for usable widths")
    app = FanoutREPL(
        mode=mode, model=model, cols=cols,
        system_prompt=system_prompt, roles=roles,
    )
    await app.run_async()
    return 0
