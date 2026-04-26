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
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    HEARTBEAT,
    StreamRenderState,
    build_options,
    heartbeat_iter,
    render_message,
)
from agentwire.sdk.sinks.textual import RichLogSink


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
    Per-column overrides (model, effort, role list) are honored when set.
    """

    def __init__(
        self,
        col: int,
        mode: str,
        model: str | None,
        effort: str | None = None,
        roles: list[str] | None = None,
    ) -> None:
        self.col = col
        self.mode = mode
        self.model = model or DEFAULT_MODEL
        self.effort = effort or DEFAULT_EFFORT
        # `roles=None` means "inherit app-level"; an explicit `[]` overrides
        # to "no roles". Capture the distinction here.
        self.roles_override: list[str] | None = (
            list(roles) if roles is not None else None
        )
        self.client: Any = None
        self.sdk_classes: dict[str, Any] | None = None
        self.chat_sink: RichLogSink | None = None
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
        height: 1fr;
        border: tall $primary;
        border-title-color: $primary;
        padding: 0 1;
        background: $background;
        scrollbar-color: $primary $background;
    }

    .col-status {
        height: 1;
        padding: 0 1;
        background: $background;
        color: $secondary;
    }

    .col-input {
        height: 3;
        border: tall $secondary;
        background: $background;
        background-tint: $background;
    }

    Input#master-input {
        dock: bottom;
        height: 3;
        border: tall $primary;
        border-title-color: $primary;
        background: $background;
        background-tint: $background;
    }
    Input#master-input:focus {
        background: $background;
        background-tint: $background;
        border: tall $primary;
    }

    Footer { dock: bottom; background: $background; }
    """

    def __init__(
        self,
        *,
        mode: str,
        model: str | None,
        cols: int,
        system_prompt: str | None = None,
        roles: list[str] | None = None,
        col_models: dict[int, str] | None = None,
        col_efforts: dict[int, str] | None = None,
        col_roles: dict[int, list[str]] | None = None,
    ) -> None:
        super().__init__()
        self.mode = mode
        self.system_prompt = system_prompt
        self.roles = roles
        self.title = f"agentwire repl — fanout · {cols} cols · {mode}"
        col_models = col_models or {}
        col_efforts = col_efforts or {}
        col_roles = col_roles or {}
        self.columns: list[FanoutColumn] = [
            FanoutColumn(
                col=i,
                mode=mode,
                model=col_models.get(i, model),
                effort=col_efforts.get(i),
                roles=col_roles.get(i),
            )
            for i in range(cols)
        ]
        self._ctx: Any = None
        self._col_ctxs: dict[int, Any] = {}

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
                    chat.border_title = self._chat_title(col)
                    yield chat
                    yield FanoutStatusLine(self._format_status(col), classes="col-status", id=f"status-{col.col}")
                    yield Input(
                        id=f"col-input-{col.col}",
                        placeholder=f"> col {col.col + 1} only",
                        classes="col-input",
                    )
        yield Input(id="master-input", placeholder="> fan-out to all columns")
        yield Footer()

    async def on_mount(self) -> None:
        # Bind chat sinks to the just-mounted RichLogs (single sink per column;
        # streaming partials and snapshot turns share the same chat output).
        for col in self.columns:
            chat = self.query_one(f"#chat-{col.col}", RichLog)
            col.chat_sink = RichLogSink(chat)

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

        # Open one SDK client per column. Per-column role overrides need
        # their own session context so the system prompt picks up the
        # right role layering.
        for col in self.columns:
            col.sdk_classes = sdk_classes
            if col.roles_override is not None:
                col_ctx = load_session_context(
                    Path.cwd(), role_overrides=col.roles_override
                )
            else:
                col_ctx = self._ctx
            self._col_ctxs[col.col] = col_ctx
            options = build_options(
                ClaudeAgentOptions,
                col.mode,
                col.model,
                self.system_prompt,
                cwd=Path.cwd(),
                effort=col.effort,
                session_context=col_ctx,
            )
            col.client = ClaudeSDKClient(options=options)
            await col.client.__aenter__()

        # Banner inside each column so the user sees what each column is.
        for col in self.columns:
            assert col.chat_sink is not None
            roles_part = ""
            if col.roles_override:
                roles_part = f" · roles={','.join(col.roles_override)}"
            col.chat_sink.write(
                f"[col {col.col + 1} · {col.model} · effort={col.effort} · {col.mode}{roles_part} · ready]\n"
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

    # ------ inputs (master + per-column) ------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        if not text:
            return
        event.input.value = ""
        input_id = getattr(event.input, "id", "") or ""
        if input_id == "master-input":
            target_cols = list(self.columns)
        elif input_id.startswith("col-input-"):
            try:
                idx = int(input_id.removeprefix("col-input-"))
            except ValueError:
                return
            if not 0 <= idx < len(self.columns):
                return
            target_cols = [self.columns[idx]]
        else:
            return

        # Echo the user turn into each target column's chat.
        for col in target_cols:
            assert col.chat_sink is not None
            col.chat_sink.write(f"\n> {text}\n")
            col.chat_sink.flush()
            col.turn_count += 1
            self._refresh_status(col)
        # Fire one worker per target column.
        for col in target_cols:
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
        if col.chat_sink is None or col.sdk_classes is None:
            return
        msg = event.payload
        if msg is HEARTBEAT:
            col.stream_state.heartbeat(col.chat_sink)
            col.chat_sink.flush()
            return
        try:
            render_message(
                msg,
                AssistantMessage=col.sdk_classes["AssistantMessage"],
                UserMessage=col.sdk_classes["UserMessage"],
                SystemMessage=col.sdk_classes["SystemMessage"],
                ResultMessage=col.sdk_classes["ResultMessage"],
                out=col.chat_sink,
                stream_state=col.stream_state,
            )
            col.chat_sink.flush()
            ResultMessage = col.sdk_classes["ResultMessage"]
            if isinstance(msg, ResultMessage):
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

    def _chat_title(self, col: FanoutColumn) -> str:
        roles = ""
        if col.roles_override:
            roles = f" · {','.join(col.roles_override)}"
        return f"col {col.col + 1} · {col.model} · {col.effort}{roles}"

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


def parse_col_overrides(
    raw: list[str] | None, *, max_col: int, value_split: bool = False
) -> dict[int, Any]:
    """Parse `N=value` repeated args into `{col_index: value}`.

    `--col-model 0=claude-opus-4-7 --col-model 2=claude-sonnet-4-6` →
    `{0: "claude-opus-4-7", 2: "claude-sonnet-4-6"}`. Indices are
    user-facing 0-based. When `value_split=True`, comma-splits the value
    into a list (used for roles, which can be multi-valued).
    """
    out: dict[int, Any] = {}
    for entry in raw or []:
        if "=" not in entry:
            raise ValueError(
                f"invalid column override {entry!r}: expected 'INDEX=VALUE'"
            )
        idx_str, _, value = entry.partition("=")
        try:
            idx = int(idx_str)
        except ValueError as e:
            raise ValueError(
                f"invalid column index {idx_str!r} in {entry!r}: not an integer"
            ) from e
        if idx < 0 or idx >= max_col:
            raise ValueError(
                f"column index {idx} in {entry!r} out of range [0,{max_col})"
            )
        if value_split:
            out[idx] = [v.strip() for v in value.split(",") if v.strip()]
        else:
            out[idx] = value
    return out


async def run_fanout_repl(
    *,
    mode: str,
    model: str | None,
    cols: int,
    system_prompt: str | None = None,
    roles: list[str] | None = None,
    col_models_raw: list[str] | None = None,
    col_efforts_raw: list[str] | None = None,
    col_roles_raw: list[str] | None = None,
) -> int:
    """Entry point invoked from `agentwire repl --view fanout --cols N`."""
    if cols < 2:
        raise ValueError("fanout requires --cols >= 2")
    if cols > 6:
        raise ValueError("fanout caps at 6 columns; pick fewer for usable widths")
    col_models = parse_col_overrides(col_models_raw, max_col=cols)
    col_efforts = parse_col_overrides(col_efforts_raw, max_col=cols)
    col_roles = parse_col_overrides(col_roles_raw, max_col=cols, value_split=True)
    app = FanoutREPL(
        mode=mode, model=model, cols=cols,
        system_prompt=system_prompt, roles=roles,
        col_models=col_models,
        col_efforts=col_efforts,
        col_roles=col_roles,
    )
    await app.run_async()
    return 0
