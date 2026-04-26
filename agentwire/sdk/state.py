"""Per-turn streaming state for claude-agent-sdk partial messages.

`StreamRenderState` walks the SDK's StreamEvent payloads (delivered when
`include_partial_messages=True`) and emits bullet-formatted output to a
single chat sink. Each major chunk (thinking, text, tool_use+result) is
its own top-level `- bullet`; continuation lines indent under it as
`  · child` so the chat stays visually clean even with content streaming.

Tool-input live byte counters are intentionally not rendered — they were
useful only with a dedicated action subpane, and we don't have one
anymore. Tool calls render at snapshot time in `render.py` instead.
"""

from __future__ import annotations

from typing import Any

from agentwire.sdk.render import (
    flush,
    styled,
)


class StreamRenderState:
    """Per-turn bookkeeping for partial-message rendering.

    The SDK delivers StreamEvents (TypedDict with a `uuid` + `event` payload)
    when `include_partial_messages=True`. We render thinking_delta and
    text_delta deltas in bullet/indent form so the user sees progress; the
    snapshot AssistantMessage at message_stop then skips re-rendering
    anything we already showed.
    """

    def __init__(self) -> None:
        self.streamed_text = False
        self.streamed_thinking = False
        self.open_block: str | None = None  # "thinking" | "text" | None
        self._heartbeat_count = 0
        # Buffer for the current logical line under the open block.
        self._line_buf = ""
        # First line of an open block goes on the same line as `- thinking:`;
        # subsequent lines are emitted as `  · <line>` continuations.
        self._first_line_emitted = False
        # Tool-call collapse: tool_uses snapshotted in render_message keep
        # an entry here so the matching tool_result emits as `  · result:`
        # under the previously-rendered `- ToolName args` line.
        self.pending_tool_uses: dict[str, dict] = {}

    @property
    def partials_active(self) -> bool:
        return self.streamed_text or self.streamed_thinking or self.open_block is not None

    def handle_partial(self, event: dict, out: Any) -> None:
        """Render one StreamEvent.event payload to bullet/indent format."""
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        if etype in ("content_block_start", "content_block_delta", "message_delta"):
            self._consume_heartbeat(out)

        if etype == "content_block_start":
            block = event.get("content_block") or {}
            btype = block.get("type")
            if btype == "thinking":
                self.open_block = "thinking"
                self.streamed_thinking = True
                self._line_buf = ""
                self._first_line_emitted = False
            elif btype == "text":
                self.open_block = "text"
                self.streamed_text = True
                self._line_buf = ""
                self._first_line_emitted = False
            # tool_use: skip — we render the snapshot in render.py once the
            # full args dict is available.

        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "thinking_delta" and self.open_block == "thinking":
                text = delta.get("thinking", "") or ""
                if text:
                    self._buffer_and_emit_lines(out, text, kind="thinking")
            elif dtype == "text_delta" and self.open_block == "text":
                text = delta.get("text", "") or ""
                if text:
                    self._buffer_and_emit_lines(out, text, kind="text")
            # input_json_delta: skip — no live byte counter without action pane.

        elif etype == "content_block_stop":
            self._flush_open_block(out)

    def _buffer_and_emit_lines(self, out: Any, text: str, *, kind: str) -> None:
        """Append text to line buffer; emit complete lines on newlines."""
        self._line_buf += text
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            self._emit_line(out, line, kind=kind)

    def _emit_line(self, out: Any, line: str, *, kind: str) -> None:
        line = line.rstrip()
        if not line and self._first_line_emitted:
            return  # skip empty continuation lines
        if kind == "thinking":
            if not self._first_line_emitted:
                out.write(styled(out, f"- thinking: {line}", "dim") + "\n")
                self._first_line_emitted = True
            else:
                out.write(styled(out, f"  · {line}", "dim") + "\n")
        elif kind == "text":
            # Plain assistant text — no bullet, just the answer text.
            out.write(line + "\n")
            self._first_line_emitted = True
        flush(out)

    def _flush_open_block(self, out: Any) -> None:
        """Emit any buffered remainder, then close the block."""
        if self.open_block in ("thinking", "text") and self._line_buf:
            self._emit_line(out, self._line_buf, kind=self.open_block)
        elif self.open_block == "thinking" and not self._first_line_emitted:
            # Empty thinking block — emit just the bullet so the user knows
            # the model thought (briefly).
            out.write(styled(out, "- thinking:", "dim") + "\n")
            flush(out)
        self._line_buf = ""
        self._first_line_emitted = False
        self.open_block = None

    def close_open_block(self, out: Any) -> None:
        """Force-close any in-flight open block (before snapshot render)."""
        self._flush_open_block(out)

    def reset_for_next_assistant_turn(self) -> None:
        """Snapshot rendered → flags reset so the next assistant turn (within
        the same SDK call, if it tool-uses then text-replies) is unambiguous."""
        self.streamed_text = False
        self.streamed_thinking = False
        self.open_block = None
        self._heartbeat_count = 0
        self._line_buf = ""
        self._first_line_emitted = False

    def flush_pending_tool_uses(self, out: Any) -> None:
        """Emit indented placeholders for tool_uses whose result never arrived.

        Each entry got its `- ToolName args` bullet at snapshot time; we
        just clear the pending registry. The bullet remains in the chat
        without a `  · result:` child — that itself is the signal that the
        tool call didn't complete.
        """
        self.pending_tool_uses.clear()

    def heartbeat(self, out: Any) -> None:
        """Called when the SDK has been silent past `idle_timeout` seconds.

        Emits a single dim bullet per tick. Slightly noisy on long quiet
        turns, but the alternative (silent gap) was the original 120s
        bug we shipped streaming-visibility to fix.
        """
        self._heartbeat_count += 1
        elapsed = self._heartbeat_count * 5
        out.write(styled(out, f"- still working · {elapsed}s", "dim") + "\n")
        flush(out)

    def _consume_heartbeat(self, out: Any) -> None:
        # Heartbeats are now self-contained bullet lines, so a real event
        # arriving doesn't need to "close" anything. Reset the counter so
        # subsequent gaps start fresh.
        self._heartbeat_count = 0
