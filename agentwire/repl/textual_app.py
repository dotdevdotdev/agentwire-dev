"""Textual-based rendering layer for `agentwire repl` (Phase 2A — layout).

Layout (Phase 2A: proportional chat=6 / action=2 / input=3):

    ┌─ Header ──────────────────────────────────┐
    │                                           │
    ├─ ChatLog (RichLog, fr=6) ─────────────────┤
    │  > previous user turn                     │
    │  assistant text                           │
    │  [→ tool call]                            │
    │  [← result]                               │
    │  [done · ...]                             │
    ├─ CurrentAction (RichLog, fr=2, title) ────┤
    │  ╭─ Current action ─────────────────────╮ │
    │  │ [thinking: ...] live stream          │ │
    │  │ [writing X input · 4.2 KB live tick] │ │
    │  │ […still working · 5s]                │ │
    │  ╰──────────────────────────────────────╯ │
    ├─ Input (Input, dock=bottom) ──────────────┤
    │  > tell me about prompt caching           │
    └─ Footer ──────────────────────────────────┘

Event flow:
    user types → SubmittableTextArea posts Submitted → on_input_submitted
    → app.run_worker(_run_turn(text), exclusive=True)
    → worker iterates client.receive_response() wrapped in _heartbeat_iter
    → each message is post_message(SdkEvent(msg)) (thread-safe)
    → on_sdk_event renders via render_message(out=_RichLogSink) on UI thread

Workers MUST use `self.post_message(...)` — never call RichLog.write directly.

Phase 2A scope: split chat into chat + CurrentAction subpanes with
proportional weights. Partial-stream events (thinking, byte counter,
heartbeat) route to the action pane via a dedicated `_ActionSink` that
supports proper in-place line updates (clear-and-rewrite). The action
pane is cleared on every ResultMessage (turn complete) so the next
turn's partials start fresh. Finalized snapshot Messages stay in chat
via `_RichLogSink` as before.

The 120-second silent gap of the legacy REPL is fully solved here:
thinking and byte counter tick live in their own docked subpane, with
clean in-place updates (not the choppy multi-line emission of Phase
1B's single-sink approach).

See `docs/missions/agentwire-repl-textual.md` for the full plan.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Footer, Header, Input, Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from agentwire.repl import persistence
from agentwire.repl.app import (
    BANNER,
    _persist_sdk_message,
)
from agentwire.sdk import (
    DEFAULT_MODEL,
    FULL_TOOLS,
    HEARTBEAT,
    RESTRICTED_TOOLS,
    StreamRenderState,
    build_options,
    heartbeat_iter,
    render_message,
)
from agentwire.sdk.client import _mcp_enabled
from agentwire.sdk.render import format_tool_input
from agentwire.sdk.sinks.textual import ActionSink, RichLogSink
from agentwire.repl.commands import (
    COMMANDS,
    CONTINUE,
    EXIT,
    RESTART,
    RESUME,
    dispatch_command,
)
from agentwire.repl.context import load_session_context
from agentwire.repl.mentions import expand_mentions
from agentwire.repl.state import (
    ReplState,
    reset_for_restart,
    track_result,
    track_system_init,
)


# ----- agentwire brand theme ------------------------------------------------
#
# Mirrors the dotdev/agentwire brand palette from agentwire-website's
# `src/app/globals.css`:
#   --background: #000000          flat black
#   --foreground: #e2e8f0          near-white
#   --primary:    #00ff88          neon green
#   --secondary:  #00d4ff          neon cyan (accent)
#   --muted/card: #0a0a0a          near-black surface
#   --accent:     #1a1a1a          slightly elevated panel
#   --border:     #27272a          subtle line
#   --destructive: #dc2626         red
#
# Textual `Theme` keys map onto these:
#   primary    → neon green   (main brand)
#   secondary  → neon cyan    (accent)
#   accent     → neon cyan    (titlebars, focus)
#   background → #000000
#   foreground → #e2e8f0
#   surface    → #0a0a0a      (status line, modal interior)
#   panel      → #1a1a1a      (button, elevated container)
#   success    → neon green   (matches brand)
#   warning    → amber        (in-progress markers)
#   error      → red          (destructive / failures)

AGENTWIRE_THEME_DEFAULTS: dict[str, str] = {
    "primary": "#00ff88",
    "secondary": "#00d4ff",
    "accent": "#00d4ff",
    "foreground": "#e2e8f0",
    "background": "#000000",
    "surface": "#0a0a0a",
    "panel": "#1a1a1a",
    "success": "#00ff88",
    "warning": "#fbbf24",
    "error": "#dc2626",
}

# Variables (theme-token level) that derive from the palette. The user can
# override via `repl.theme.<key>` in `~/.agentwire/config.yaml` too.
AGENTWIRE_THEME_VARIABLES: dict[str, str] = {
    "header-foreground": "#00ff88",
    "header-background": "#000000",
    "footer-key-foreground": "#00d4ff",
    "footer-foreground": "#e2e8f0",
    "footer-background": "#000000",
    "footer-description-foreground": "#b8b8b8",
    "border-blurred": "#27272a",
}


def build_agentwire_theme(overrides: dict[str, str] | None = None) -> Theme:
    """Construct the agentwire brand `Theme` with optional user overrides.

    `overrides` is a flat dict from `~/.agentwire/config.yaml`'s
    `repl.theme.*` entries. Keys map onto either Theme palette attributes
    (primary, secondary, …) or Textual variable names (header-foreground,
    footer-key-foreground, …). Unknown keys are ignored.
    """
    overrides = overrides or {}
    palette = {**AGENTWIRE_THEME_DEFAULTS}
    variables = {**AGENTWIRE_THEME_VARIABLES}
    for key, value in overrides.items():
        if not value:
            continue
        if key in palette:
            palette[key] = value
        else:
            # Anything not in the palette is treated as a variable override
            # (e.g. header-foreground, footer-key-foreground).
            variables[key] = value
    return Theme(
        name="agentwire",
        dark=True,
        variables=variables,
        **palette,
    )


# Stable instance for tests and code that doesn't need overrides.
AGENTWIRE_THEME = build_agentwire_theme()


# ----- SDK event message ----------------------------------------------------


class SdkEvent(Message):
    """One SDK message (or _HEARTBEAT sentinel) handed off from the worker
    thread to the UI thread for rendering."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        super().__init__()


class TurnFinished(Message):
    """Worker signals turn complete (or cancelled). UI re-enables input."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        super().__init__()


# ----- Command palette ModalScreen (Phase 3C) ------------------------------


class CommandPalette(ModalScreen[str | None]):
    """Ctrl+P fuzzy picker for slash commands.

    Reads from `agentwire.repl.commands.COMMANDS` (the existing registry)
    so newly added slash commands appear here automatically. Returns the
    selected command name (e.g. `/cost`) on dismiss, or None on Esc.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    DEFAULT_CSS = """
    CommandPalette {
        align: center middle;
    }
    CommandPalette > Vertical {
        background: $surface;
        border: thick $secondary;
        width: 70;
        height: 22;
    }
    CommandPalette Input {
        margin: 1;
        border: none;
    }
    CommandPalette OptionList {
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        # Build a unique-by-Command list so aliases don't double-list.
        seen: set[str] = set()
        self._commands: list[tuple[str, str]] = []
        for name, cmd in COMMANDS.items():
            if cmd.name in seen:
                continue
            seen.add(cmd.name)
            self._commands.append((cmd.name, cmd.summary))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Input(id="palette-input", placeholder="Filter commands…")
            yield OptionList(id="palette-list")

    def on_mount(self) -> None:
        self.query_one("#palette-input", Input).focus()
        self._refilter("")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "palette-input":
            self._refilter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "palette-input":
            self._accept_highlighted()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        # Mouse click on an option.
        opt_id = event.option.id
        if opt_id:
            self.dismiss(opt_id)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _refilter(self, query: str) -> None:
        olist = self.query_one("#palette-list", OptionList)
        olist.clear_options()
        q = query.lower().lstrip("/")
        for name, summary in self._commands:
            score = self._fuzzy_score(name, summary, q)
            if score < 0:
                continue
            label = f"{name}  —  {summary}"
            olist.add_option(Option(label, id=name))
        # Highlight the first match by default.
        if olist.option_count > 0:
            olist.highlighted = 0

    @staticmethod
    def _fuzzy_score(name: str, summary: str, q: str) -> int:
        """Return a score (>=0 = match, -1 = no match).

        Matches are: empty query, substring of name (best), substring of
        summary (lower priority). A leading `/` on the query is dropped so
        users typing `/co` and `co` both match `/cost`.
        """
        q = q.lower().lstrip("/")
        if not q:
            return 0
        n = name.lower().lstrip("/")
        s = summary.lower()
        if n.startswith(q):
            return 0
        if q in n:
            return 1
        if q in s:
            return 2
        return -1

    def _accept_highlighted(self) -> None:
        olist = self.query_one("#palette-list", OptionList)
        idx = olist.highlighted
        if idx is None or idx < 0 or idx >= olist.option_count:
            self.dismiss(None)
            return
        opt = olist.get_option_at_index(idx)
        self.dismiss(opt.id)


# ----- Permission ModalScreen (Phase 2C) -----------------------------------


class PermissionPrompt(ModalScreen[str]):
    """Centered modal for `sdk-prompted` mode tool-permission decisions.

    Replaces Phase 1C's inline y/n/a placeholder. Returns one of:
      'allow' | 'deny' | 'always'
    via `push_screen_wait`.
    """

    BINDINGS = [
        Binding("y", "decide('allow')", "Allow"),
        Binding("n", "decide('deny')", "Deny"),
        Binding("a", "decide('always')", "Always"),
        Binding("escape", "decide('deny')", "Deny", show=False),
    ]

    DEFAULT_CSS = """
    PermissionPrompt {
        align: center middle;
    }
    PermissionPrompt > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 80;
        height: auto;
    }
    PermissionPrompt Label {
        margin-bottom: 1;
        color: $primary;
    }
    PermissionPrompt Static.tool-line {
        color: $foreground;
        margin-bottom: 1;
    }
    PermissionPrompt Static.help-line {
        color: $foreground 60%;
    }
    PermissionPrompt Center {
        margin-top: 1;
    }
    PermissionPrompt Button {
        margin: 0 1;
    }
    """

    def __init__(self, tool_name: str, summary: str = "") -> None:
        super().__init__()
        self._tool_name = tool_name
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Allow {self._tool_name}?")
            tool_line = self._tool_name + ((" " + self._summary) if self._summary else "")
            yield Static(tool_line, classes="tool-line")
            yield Static(
                "y = allow once · n = deny · a = always allow this session · esc = deny",
                classes="help-line",
            )
            with Center():
                yield Button("Allow (y)", variant="success", id="allow")
                yield Button("Deny (n)", variant="error", id="deny")
                yield Button("Always (a)", variant="primary", id="always")

    def action_decide(self, decision: str) -> None:
        self.dismiss(decision)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Button id matches the decision string verbatim.
        self.dismiss(event.button.id or "deny")


# ----- Transcript scrubber ModalScreen (Phase 3D) --------------------------


class TranscriptScrubber(ModalScreen[None]):
    """Read-only viewer for the current session's prior turns.

    Triggered by `/scrub`. Lists each `user_input` event from the transcript
    JSONL, with a 100-char preview. Esc / button to close. Selecting a turn
    is a no-op for now (Phase 3D ships read-only; future scroll-to-turn is
    separate).
    """

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("q", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    TranscriptScrubber {
        align: center middle;
    }
    TranscriptScrubber > Vertical {
        background: $surface;
        border: thick $secondary;
        width: 100;
        height: 30;
        padding: 0 1;
    }
    TranscriptScrubber Label {
        margin: 1 0;
        color: $primary;
    }
    TranscriptScrubber OptionList {
        height: 1fr;
    }
    """

    def __init__(self, transcript_path: Path | None) -> None:
        super().__init__()
        self._path = transcript_path

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Transcript — prior turns this session (Esc to close)")
            yield OptionList(id="scrub-list")

    def on_mount(self) -> None:
        olist = self.query_one("#scrub-list", OptionList)
        if self._path is None or not Path(self._path).exists():
            olist.add_option(Option("(no transcript)"))
            return
        import json
        try:
            lines = Path(self._path).read_text().splitlines()
        except Exception as exc:
            olist.add_option(Option(f"(read error: {exc})"))
            return
        n = 0
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "user_input":
                n += 1
                text = (ev.get("text") or "").replace("\n", " ")
                preview = text[:100] + ("..." if len(text) > 100 else "")
                olist.add_option(Option(f"{n:>3}. {preview}"))
        if n == 0:
            olist.add_option(Option("(no user turns yet)"))

    def action_close(self) -> None:
        self.dismiss(None)


# ----- StatusLine widget ----------------------------------------------------


_SPARK_BARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    """Render a unicode-block sparkline from `values`.

    Empty input → "". All zeros → flat baseline. Each value scales relative
    to the peak in the series.
    """
    if not values:
        return ""
    peak = max(values)
    if peak <= 0:
        return _SPARK_BARS[0] * len(values)
    out: list[str] = []
    span = len(_SPARK_BARS) - 1
    for v in values:
        ratio = v / peak
        idx = max(0, min(span, int(ratio * span)))
        out.append(_SPARK_BARS[idx])
    return "".join(out)


class StatusLine(Static):
    """Single-line widget showing running totals + tunables.

    Renders one of two formats depending on whether any turns have completed:
    - pre-turn: `{mode} · {model} · effort={e} · thinking={t}`
    - post-turn: `{N turns} · {tok} tok · ${cost:.4f} · effort={e} · thinking={t}`

    `refresh_from_state(state)` is called by the App on every SdkEvent so
    the line stays current with cost/token totals + slash-command tunable
    changes (`/effort`, `/thinking`).
    """

    DEFAULT_CSS = """
    StatusLine {
        height: 1;
        padding: 0 1;
        color: $secondary;
        background: $background;
    }
    """

    def refresh_from_state(self, state: ReplState | None) -> None:
        if state is None:
            self.update("")
            return
        if state.turn_count == 0:
            self.update(
                f"{state.mode} · {state.model} · effort={state.effort} · "
                f"thinking={state.thinking_mode}"
            )
            return
        total = state.total_input_tokens + state.total_output_tokens
        plural = "s" if state.turn_count != 1 else ""
        spark = _sparkline(state.turn_costs)
        spark_part = f" {spark}" if spark else ""
        self.update(
            f"{state.turn_count} turn{plural}{spark_part} · "
            f"{total} tok ({state.total_input_tokens} in / {state.total_output_tokens} out) · "
            f"${state.total_cost_usd:.4f} · effort={state.effort} · "
            f"thinking={state.thinking_mode}"
        )


# ----- the App --------------------------------------------------------------


class AgentwireREPL(App):
    """Textual REPL skeleton — Phase 1B.

    Phase 1B carries chat rendering + slash dispatch + SDK streaming.
    Phase 1C wires persistence, @-mentions, permission prompts, /clear+/resume
    lifecycle. Phase 2 splits the chat region into chat+CurrentAction.
    """

    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }

    /* Chat — main conversation area, wrapped in the brand neon green. */
    #chat {
        height: 6fr;
        border: tall $primary;
        padding: 0 1;
        background: $background;
        scrollbar-color: $primary $surface;
    }

    /* CurrentAction — live partial stream, wrapped in the brand cyan accent.
       Flat-black background to match chat — no near-black surface. */
    #action {
        height: 2fr;
        border: tall $secondary;
        border-title-color: $secondary;
        padding: 0 1;
        background: $background;
        scrollbar-color: $secondary $background;
    }

    /* Input — neon green border, flat-black inside.
       Textual's default Input uses `background: $surface` (#0a0a0a) and
       adds a 5% foreground tint on focus — both override flat black, so
       we squash them with explicit values. */
    Input {
        background: $background;
        background-tint: $background;
    }
    Input:focus {
        background: $background;
        background-tint: $background;
        border: tall $primary;
    }
    #input {
        dock: bottom;
        height: 3;
        border: tall $primary;
        background: $background;
    }

    /* Header (title bar) — flat black with neon-green app title. */
    Header {
        dock: top;
        background: $background;
        color: $primary;
    }
    HeaderTitle {
        background: $background;
        color: $primary;
    }
    HeaderIcon {
        background: $background;
    }

    Footer {
        dock: bottom;
        background: $background;
    }
    """

    BINDINGS = [
        Binding("ctrl+d", "quit", "Exit"),
        Binding("ctrl+c", "cancel_turn", "Cancel turn"),
        Binding("ctrl+p", "agentwire_palette", "Command palette"),
        Binding("tab", "complete_mention", "Complete @mention", show=False),
    ]

    def __init__(
        self,
        *,
        mode: str = "bypass",
        model: str | None = None,
        system_prompt: str | None = None,
        session_name: str | None = None,
        resume: str | None = None,
        roles: list[str] | None = None,
        seed_message: str | None = None,
    ) -> None:
        super().__init__()
        self._cfg = dict(
            mode=mode,
            model=model,
            system_prompt=system_prompt,
            session_name=session_name,
            resume=resume,
            roles=roles,
            seed_message=seed_message,
        )
        self.state: ReplState | None = None
        self._client = None
        self._client_ctx = None
        self._sink: RichLogSink | None = None  # chat — finalized snapshot messages
        self._action_sink: ActionSink | None = None  # action — live partials
        self._stream_state = StreamRenderState()
        self._sdk_classes: dict[str, Any] | None = None
        self._saved_claudecode: str | None = None
        self._exit_code = 0
        self._transcript = None
        self._ctx = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield RichLog(id="chat", markup=False, highlight=False, wrap=True, auto_scroll=True)
        yield RichLog(id="action", markup=False, highlight=False, wrap=True, auto_scroll=True)
        yield StatusLine(id="status")
        yield Input(id="input", placeholder="> ")
        yield Footer()

    # ------ lifecycle ------

    async def on_mount(self) -> None:
        # Build + apply the agentwire brand theme, merging any per-user
        # overrides from `~/.agentwire/config.yaml` (`repl.theme.*`). Other
        # built-in themes remain available via `/theme <name>`.
        try:
            overrides: dict[str, str] = {}
            try:
                from agentwire.config import get_config
                cfg = get_config()
                overrides = dict(getattr(cfg.repl, "theme", {}) or {})
            except Exception:
                # Config load is best-effort — fall back to defaults silently.
                pass
            theme = build_agentwire_theme(overrides)
            self.register_theme(theme)
            self.theme = "agentwire"
        except Exception:
            # Older Textual versions or theme-system changes shouldn't crash
            # the app — fall back to the default theme.
            pass

        chat = self.query_one("#chat", RichLog)
        action = self.query_one("#action", RichLog)
        action.border_title = "Current action"
        self._sink = RichLogSink(chat)
        self._action_sink = ActionSink(action)
        self.query_one("#input", Input).focus()

        # Header content — set after we know the mode/model.
        self.title = "agentwire repl"
        model_short = (self._cfg["model"] or DEFAULT_MODEL).replace("claude-", "")
        self.sub_title = f"{self._cfg['mode']} · {model_short}"
        try:
            await self._open_session()
        except Exception as exc:
            self._sink.write(f"[startup error: {exc}]\n")
            self._sink.flush()
            return
        # Initial status line after state is built.
        self._refresh_status()

        if self._cfg["seed_message"]:
            self.run_worker(
                self._run_turn(self._cfg["seed_message"]),
                exclusive=True,
                name="sdk-turn",
            )

    async def on_unmount(self) -> None:
        try:
            if self._client_ctx is not None:
                await self._client_ctx.__aexit__(None, None, None)
        except Exception:
            # Never raise during shutdown.
            pass
        if self._transcript is not None and self.state is not None:
            try:
                persistence.finalize(self._transcript, self.state)
                self._transcript.close()
            except Exception:
                pass
        if self._saved_claudecode is not None:
            os.environ["CLAUDECODE"] = self._saved_claudecode

    async def _open_session(self) -> None:
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKClient,
                ResultMessage,
                SystemMessage,
                UserMessage,
            )
        except ImportError as exc:
            raise RuntimeError(f"claude-agent-sdk not installed: {exc}")

        self._sdk_classes = {
            "ClaudeAgentOptions": ClaudeAgentOptions,
            "ClaudeSDKClient": ClaudeSDKClient,
            "AssistantMessage": AssistantMessage,
            "UserMessage": UserMessage,
            "SystemMessage": SystemMessage,
            "ResultMessage": ResultMessage,
        }

        # Resume lookup — find the prior session's last sdk_session_id.
        resume_sdk_session_id: str | None = None
        if self._cfg["resume"]:
            prior = persistence.load_session(self._cfg["resume"])
            if prior is None:
                raise RuntimeError(
                    f"no session named {self._cfg['resume']!r} under "
                    f"{persistence.DEFAULT_REPL_HOME}"
                )
            ids = prior.get("sdk_session_ids") or []
            if ids:
                resume_sdk_session_id = ids[-1]

        # Build state.
        mode = self._cfg["mode"]
        placeholder = (RESTRICTED_TOOLS if mode == "restricted" else FULL_TOOLS).copy()
        self.state = ReplState(
            mode=mode,
            model=self._cfg["model"] or DEFAULT_MODEL,
            allowed_tools=placeholder,
        )

        # Roles + voice.
        self._ctx = load_session_context(Path.cwd(), role_overrides=self._cfg["roles"])
        self.state.role_names = list(self._ctx.role_names)
        self.state.voice = self._ctx.voice

        # Build options + open client. can_use_tool only in sdk-prompted.
        can_use_tool = self._make_can_use_tool() if mode == "prompted" else None
        options = build_options(
            ClaudeAgentOptions,
            mode,
            self._cfg["model"],
            self._cfg["system_prompt"],
            cwd=Path.cwd(),
            resume_sdk_session_id=resume_sdk_session_id,
            effort=self.state.effort,
            thinking_mode=self.state.thinking_mode,
            can_use_tool=can_use_tool,
            session_context=self._ctx,
        )
        self.state.allowed_tools = (
            list(options.allowed_tools)
            if hasattr(options, "allowed_tools")
            else list(getattr(options, "kwargs", {}).get("allowed_tools", []))
        )

        # Transcript persistence (mirrors legacy REPL).
        self._transcript = persistence.create_session(
            mode=mode,
            model=self._cfg["model"] or DEFAULT_MODEL,
            allowed_tools=self.state.allowed_tools,
            name=self._cfg["session_name"],
        )
        self.state.session_dir = str(self._transcript.session_dir)
        self.state.transcript_name = self._transcript.name

        # Banner into chat.
        self._render_banner()
        if resume_sdk_session_id:
            self._sink.write(
                f"Resuming {self._cfg['resume']!r} "
                f"(sdk session {resume_sdk_session_id[:8]}…)\n"
            )
        self._sink.write(f"[transcript → {self._transcript.session_dir}]\n\n")
        self._sink.flush()

        # CLAUDECODE env nesting fix (mirrors _run_interactive).
        self._saved_claudecode = os.environ.pop("CLAUDECODE", None)

        # Open the SDK client.
        self._client_ctx = ClaudeSDKClient(options=options)
        self._client = await self._client_ctx.__aenter__()

    def _render_banner(self) -> None:
        assert self._sink is not None
        assert self.state is not None
        model_display = self._cfg["model"] or f"{DEFAULT_MODEL} (default)"
        self._sink.write(BANNER.format(mode=self._cfg["mode"], model=model_display))
        self._sink.write(
            "Interactive mode (Textual). Enter to send · Alt+Enter for newline · "
            "Ctrl+D to exit · Ctrl+C to cancel.\n"
            "Type /help for commands.\n"
        )
        if _mcp_enabled():
            self._sink.write(
                "agentwire MCP server attached — /tools to see what's wired in.\n"
            )
        if self.state.role_names:
            self._sink.write(f"Roles: {', '.join(self.state.role_names)}\n")
        if self.state.voice:
            self._sink.write(f"Voice: {self.state.voice}\n")
        self._sink.write("\n")
        self._sink.flush()

    def _make_can_use_tool(self):
        """Permission callback for `sdk-prompted` mode.

        Phase 2C: pushes a centered `PermissionPrompt` ModalScreen and
        awaits the dismiss value ('allow' | 'deny' | 'always'). The modal
        captures keyboard (y/n/a/esc) and clicks; the SDK loop is paused
        on `push_screen_wait` until the user decides.
        """
        async def _can_use_tool(tool_name: str, tool_input: dict, ctx: Any):
            from claude_agent_sdk import (
                PermissionResultAllow,
                PermissionResultDeny,
            )

            if tool_name in self.state.always_allow_tools:
                return PermissionResultAllow()

            summary = format_tool_input(tool_name, tool_input or {})
            try:
                decision = await self.push_screen_wait(
                    PermissionPrompt(tool_name=tool_name, summary=summary)
                )
            except Exception as exc:
                # If the modal can't be pushed (e.g. app shutting down),
                # default to deny rather than allowing silently.
                self._sink.write(f"[permission modal error: {exc}]\n")
                self._sink.flush()
                return PermissionResultDeny(message=f"modal error: {exc}")

            decision = (decision or "deny").lower()
            if decision == "always":
                self.state.always_allow_tools.add(tool_name)
                self._sink.write(
                    f"[allow · {tool_name} now always allowed this session]\n"
                )
                self._sink.flush()
                return PermissionResultAllow()
            if decision == "allow":
                self._sink.write(f"[allow · {tool_name}]\n")
                self._sink.flush()
                return PermissionResultAllow()
            self._sink.write(f"[deny · {tool_name}]\n")
            self._sink.flush()
            return PermissionResultDeny(message="user denied")

        return _can_use_tool

    async def _restart_session(self) -> None:
        """Close+reopen the SDK client for /clear, /resume, /effort, /thinking."""
        assert self.state is not None
        assert self._sdk_classes is not None

        next_resume_id = self.state.pending_resume_sdk_session_id
        self.state.pending_resume_sdk_session_id = None

        # Close current client first.
        try:
            if self._client_ctx is not None:
                await self._client_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        self._client = None
        self._client_ctx = None

        # Rebuild options with possibly-new effort/thinking/resume.
        can_use_tool = (
            self._make_can_use_tool() if self._cfg["mode"] == "prompted" else None
        )
        options = build_options(
            self._sdk_classes["ClaudeAgentOptions"],
            self._cfg["mode"],
            self._cfg["model"],
            self._cfg["system_prompt"],
            cwd=Path.cwd(),
            resume_sdk_session_id=next_resume_id,
            effort=self.state.effort,
            thinking_mode=self.state.thinking_mode,
            can_use_tool=can_use_tool,
            session_context=self._ctx,
        )

        # Record the restart.
        if self._transcript is not None:
            self._transcript.write_event({
                "type": "restart",
                "resume_sdk_session_id": next_resume_id,
            })
        reset_for_restart(self.state)

        # Reopen.
        self._client_ctx = self._sdk_classes["ClaudeSDKClient"](options=options)
        self._client = await self._client_ctx.__aenter__()

    # ------ input handling ------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.value = ""

        if not text:
            return
        assert self._sink is not None

        # Echo the user turn into chat for readability.
        for line in text.splitlines():
            self._sink.write(f"> {line}\n")
        self._sink.flush()

        # Textual-only slash commands intercepted before dispatch_command.
        if text.startswith("/layout"):
            self._handle_layout(text[len("/layout"):].strip())
            return
        if text.startswith("/theme"):
            self._handle_theme(text[len("/theme"):].strip())
            return
        if text == "/scrub" or text.startswith("/scrub "):
            self._handle_scrub()
            return

        if text.startswith("/"):
            action = dispatch_command(text, self.state, self._sink)
            self._sink.flush()
            # /effort and /thinking mutate state — reflect immediately.
            self._refresh_status()
            if action == EXIT:
                self.exit(self._exit_code)
                return
            if action in (RESTART, RESUME):
                self.run_worker(
                    self._restart_session(),
                    exclusive=True,
                    name="sdk-restart",
                )
            return

        # Expand @path mentions before sending (mirrors legacy REPL).
        expanded_text, expansions = expand_mentions(text, cwd=Path.cwd())
        if expansions:
            count = len(expansions)
            self._sink.write(
                f"[expanded {count} mention"
                f"{'s' if count != 1 else ''}: "
                f"{', '.join(e.raw for e in expansions)}]\n"
            )
            self._sink.flush()

        # Persist the user turn before firing the SDK.
        if self._transcript is not None:
            event_data: dict = {"type": "user_input", "text": text}
            if expansions:
                event_data["expanded_text"] = expanded_text
                event_data["mentions"] = [
                    {"raw": e.raw, "target": e.target} for e in expansions
                ]
            self._transcript.write_event(event_data)

        self.run_worker(
            self._run_turn(expanded_text),
            exclusive=True,
            name="sdk-turn",
        )

    def action_cancel_turn(self) -> None:
        # Cancel any in-flight worker. exclusive=True on the next run also
        # cancels, but Ctrl+C should be eager.
        worker = next(
            (w for w in self.workers if w.name == "sdk-turn" and w.is_running),
            None,
        )
        if worker is not None:
            worker.cancel()

    # ------ SDK turn worker ------

    async def _run_turn(self, text: str) -> None:
        assert self._client is not None
        assert self._sink is not None
        assert self._sdk_classes is not None
        try:
            await self._client.query(text)
            async for message in heartbeat_iter(
                self._client.receive_response(), idle_timeout=5.0
            ):
                self.post_message(SdkEvent(message))
        except asyncio.CancelledError:
            self.post_message(TurnFinished(error="cancelled"))
            raise
        except Exception as exc:
            self.post_message(TurnFinished(error=f"{type(exc).__name__}: {exc}"))
            return
        self.post_message(TurnFinished())

    # ------ rendering on the UI thread ------

    def on_sdk_event(self, event: SdkEvent) -> None:
        assert self._sink is not None
        assert self._action_sink is not None
        assert self._sdk_classes is not None
        msg = event.payload
        if msg is HEARTBEAT:
            # Heartbeats live in the action pane (live indicator).
            self._stream_state.heartbeat(self._action_sink)
            self._action_sink.flush()
            return
        try:
            render_message(
                msg,
                AssistantMessage=self._sdk_classes["AssistantMessage"],
                UserMessage=self._sdk_classes["UserMessage"],
                SystemMessage=self._sdk_classes["SystemMessage"],
                ResultMessage=self._sdk_classes["ResultMessage"],
                out=self._sink,
                action_out=self._action_sink,
                stream_state=self._stream_state,
            )
            self._sink.flush()
            self._action_sink.flush()
            if self._transcript is not None:
                _persist_sdk_message(
                    msg,
                    self._transcript,
                    self._sdk_classes["AssistantMessage"],
                    self._sdk_classes["UserMessage"],
                    self._sdk_classes["SystemMessage"],
                    self._sdk_classes["ResultMessage"],
                )
            if isinstance(msg, self._sdk_classes["SystemMessage"]):
                track_system_init(self.state, msg)
                if self.state.session_id and self._transcript is not None:
                    persistence.record_session_id(
                        self._transcript, self.state.session_id
                    )
            elif isinstance(msg, self._sdk_classes["ResultMessage"]):
                track_result(self.state, msg)
                if getattr(msg, "is_error", False):
                    self._exit_code = 1
                # Turn complete — clear the action pane so the next turn's
                # partials start fresh.
                self._action_sink.clear()
            self._refresh_status()
        except Exception as exc:
            self._sink.write(f"[render error: {exc}]\n")
            self._sink.flush()

    # ------ Command palette (Phase 3C) ------

    def action_agentwire_palette(self) -> None:
        """Ctrl+P opens a fuzzy picker over the slash command registry."""
        def _on_dismiss(selected: str | None) -> None:
            if selected is None:
                return
            # Push the selected command name into the input so the user can
            # add args (e.g. /resume <name>) before submitting.
            try:
                inp = self.query_one("#input", Input)
            except Exception:
                return
            inp.value = selected + " "
            inp.cursor_position = len(inp.value)
            inp.focus()

        self.push_screen(CommandPalette(), _on_dismiss)

    # ------ @-mention autocomplete (Phase 3B) ------

    _MENTION_PREVIEW_CAP = 8

    def on_input_changed(self, event: Input.Changed) -> None:
        """Update the action pane with @-mention candidates as the user types."""
        if self._action_sink is None:
            return
        prefix = self._current_at_prefix(event.value, event.input.cursor_position)
        if prefix is None:
            # Not in an @-mention context — only clear if we previously
            # populated candidates (avoid clobbering live SDK output).
            if getattr(self, "_last_mention_prefix", None) is not None:
                self._action_sink.clear()
                self._last_mention_prefix = None
            return

        self._last_mention_prefix = prefix
        matches = self._glob_mention_matches(prefix)
        self._action_sink.clear()
        if not matches:
            self._action_sink.write(f"[mentions @{prefix}: no matches in {Path.cwd().name}/]\n")
        else:
            shown = matches[: self._MENTION_PREVIEW_CAP]
            self._action_sink.write(
                "[mentions: " + " · ".join(shown) + "]\n"
            )
            if len(matches) > self._MENTION_PREVIEW_CAP:
                self._action_sink.write(
                    f"[+{len(matches) - self._MENTION_PREVIEW_CAP} more — keep typing to filter, Tab to accept first]\n"
                )
            else:
                self._action_sink.write("[Tab to accept first match]\n")

    @staticmethod
    def _current_at_prefix(text: str, cursor: int) -> str | None:
        """Return the @-prefix the cursor is currently inside, or None."""
        if not text:
            return None
        # Look at the text up to the cursor; find the last `@` after a
        # whitespace boundary (or start).
        head = text[:cursor]
        if "@" not in head:
            return None
        at_idx = head.rfind("@")
        # Must be preceded by whitespace or start-of-string (mirror mentions.py).
        if at_idx > 0 and not head[at_idx - 1].isspace():
            return None
        # Path chars only (no spaces) — terminate at first whitespace.
        rest = head[at_idx + 1:]
        if any(c.isspace() for c in rest):
            return None
        return rest

    def _glob_mention_matches(self, prefix: str) -> list[str]:
        """Glob-match files under cwd whose path starts with `prefix`.

        Empty prefix → top files in cwd by mtime. Caps at 50 results.
        """
        cwd = Path.cwd()
        if not prefix:
            try:
                entries = sorted(
                    [p for p in cwd.iterdir() if not p.name.startswith(".")],
                    key=lambda p: -p.stat().st_mtime,
                )[:50]
                return [p.name + ("/" if p.is_dir() else "") for p in entries]
            except Exception:
                return []

        # Match prefix against names + paths under cwd. Glob with `**` would
        # be too broad; instead walk the prefix's parent.
        pat_parent = Path(prefix).parent
        pat_name = Path(prefix).name
        search_dir = cwd / pat_parent
        if not search_dir.exists():
            return []
        try:
            candidates = []
            for p in search_dir.iterdir():
                if p.name.startswith("."):
                    continue
                if p.name.startswith(pat_name):
                    rel = p.relative_to(cwd)
                    candidates.append(str(rel) + ("/" if p.is_dir() else ""))
            candidates.sort(key=lambda s: (0 if s.endswith("/") else 1, s.lower()))
            return candidates[:50]
        except Exception:
            return []

    def action_complete_mention(self) -> None:
        """Tab in the input completes the current @prefix to the top match."""
        try:
            inp = self.query_one("#input", Input)
        except Exception:
            return
        prefix = self._current_at_prefix(inp.value, inp.cursor_position)
        if prefix is None:
            return
        matches = self._glob_mention_matches(prefix)
        if not matches:
            return
        # Replace the @prefix with the top match.
        head = inp.value[:inp.cursor_position]
        tail = inp.value[inp.cursor_position:]
        at_idx = head.rfind("@")
        new_head = head[:at_idx + 1] + matches[0]
        inp.value = new_head + tail
        inp.cursor_position = len(new_head)
        # Clear the action pane preview now that the mention is locked in.
        if self._action_sink is not None:
            self._action_sink.clear()
            self._last_mention_prefix = None

    # ------ Textual-only slash commands (Phase 2D + 3D) ------

    def _handle_scrub(self) -> None:
        """`/scrub` — open a read-only viewer of prior turns this session."""
        path = None
        if self._transcript is not None:
            try:
                path = Path(self._transcript.session_dir) / "transcript.jsonl"
            except Exception:
                path = None
        self.push_screen(TranscriptScrubber(path))

    def _handle_layout(self, args: str) -> None:
        """`/layout` — adjust the chat / action proportional weights at runtime.

        Usage:
          /layout            # show current weights
          /layout chat=8 action=1
        """
        assert self._sink is not None
        if not args:
            chat = self.query_one("#chat", RichLog)
            action = self.query_one("#action", RichLog)
            self._sink.write(
                f"[layout: chat={chat.styles.height} action={action.styles.height}]\n"
            )
            self._sink.flush()
            return

        # Parse chat=N action=M tokens.
        chat_n: int | None = None
        action_n: int | None = None
        for tok in args.split():
            if "=" not in tok:
                continue
            key, _, val = tok.partition("=")
            try:
                n = int(val)
            except ValueError:
                continue
            if key.lower() == "chat":
                chat_n = n
            elif key.lower() == "action":
                action_n = n

        if chat_n is None and action_n is None:
            self._sink.write("[/layout: expected chat=N or action=N — nothing changed]\n")
            self._sink.flush()
            return

        try:
            if chat_n is not None and chat_n > 0:
                self.query_one("#chat", RichLog).styles.height = f"{chat_n}fr"
            if action_n is not None and action_n > 0:
                self.query_one("#action", RichLog).styles.height = f"{action_n}fr"
            self._sink.write(
                f"[layout updated · "
                f"chat={chat_n if chat_n is not None else 'unchanged'} · "
                f"action={action_n if action_n is not None else 'unchanged'}]\n"
            )
        except Exception as exc:
            self._sink.write(f"[/layout error: {exc}]\n")
        self._sink.flush()

    def _handle_theme(self, args: str) -> None:
        """`/theme` — switch Textual themes at runtime.

        Usage:
          /theme               # show current theme + list available
          /theme <name>        # set theme
        """
        assert self._sink is not None
        if not args:
            current = getattr(self, "theme", None) or "(default)"
            available = self._available_themes()
            self._sink.write(f"[theme: {current}]\n")
            self._sink.write(f"[available: {', '.join(available)}]\n")
            self._sink.flush()
            return

        try:
            self.theme = args
            self._sink.write(f"[theme set: {args}]\n")
        except Exception as exc:
            self._sink.write(f"[/theme error: {exc}]\n")
        self._sink.flush()

    def _available_themes(self) -> list[str]:
        # Textual exposes `App.available_themes` as a property in 0.80+.
        try:
            return sorted(self.available_themes.keys())
        except (AttributeError, TypeError):
            # Fallback list of built-in themes for older Textual versions.
            return [
                "textual-dark", "textual-light",
                "nord", "gruvbox", "catppuccin-mocha", "dracula",
                "tokyo-night", "monokai", "flexoki",
                "catppuccin-latte", "solarized-light",
            ]

    def _refresh_status(self) -> None:
        try:
            self.query_one("#status", StatusLine).refresh_from_state(self.state)
        except Exception:
            pass

    def on_turn_finished(self, event: TurnFinished) -> None:
        assert self._sink is not None
        if event.error == "cancelled":
            self._sink.write("[turn cancelled]\n")
            self._sink.flush()
        elif event.error:
            self._sink.write(f"[turn error: {event.error}]\n")
            self._sink.flush()
        # Refocus the input so the next turn can start typing right away.
        try:
            self.query_one("#input", Input).focus()
        except Exception:
            pass


# ----- entry point ----------------------------------------------------------


async def run_textual_repl(
    *,
    mode: str = "bypass",
    model: str | None = None,
    system_prompt: str | None = None,
    session_name: str | None = None,
    resume: str | None = None,
    roles: list[str] | None = None,
    seed_message: str | None = None,
) -> int:
    app = AgentwireREPL(
        mode=mode,
        model=model,
        system_prompt=system_prompt,
        session_name=session_name,
        resume=resume,
        roles=roles,
        seed_message=seed_message,
    )
    await app.run_async()
    return app._exit_code
