"""Per-turn streaming state for claude-agent-sdk partial messages.

`StreamRenderState` walks the SDK's StreamEvent payloads (delivered when
`include_partial_messages=True`) and routes each delta to the right place:
thinking text streams inline (dim), tool_use byte counter ticks live, the
final AssistantMessage snapshot then knows what's already been shown.

The state machine itself is sink-agnostic: it calls `out.write()` and
formatting helpers on whatever object is passed in. The Textual REPL
gives it a RichLog-backed sink for the action pane; print mode gives it
stdout.
"""

from __future__ import annotations

from typing import Any

from agentwire.sdk.render import (
    close_style,
    flush,
    format_bytes,
    open_style,
    styled,
)


class StreamRenderState:
    """Per-turn bookkeeping for partial-message rendering.

    The SDK delivers StreamEvents (TypedDict with a `uuid` + `event` payload)
    when `include_partial_messages=True`. We render thinking_delta and
    text_delta deltas inline so the user sees progress; the snapshot
    AssistantMessage at message_stop then skips re-rendering anything we
    already showed.
    """

    def __init__(self) -> None:
        self.streamed_text = False
        self.streamed_thinking = False
        self.open_block: str | None = None  # "thinking" | "text" | "tool_use" | None
        self._heartbeat_count = 0
        self._heartbeat_started_inline = False
        self._tool_use_name: str | None = None
        self._tool_use_bytes: int = 0
        # Tool-call collapse: deferred [→ Tool args] writes, keyed by
        # tool_use_id. Folded into one line when the matching tool_result
        # arrives. Unmatched at end of turn → flushed as-is.
        self.pending_tool_uses: dict[str, dict] = {}

    @property
    def partials_active(self) -> bool:
        return self.streamed_text or self.streamed_thinking or self.open_block is not None

    def handle_partial(self, event: dict, out: Any) -> None:
        """Render one StreamEvent.event payload."""
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        if etype in ("content_block_start", "content_block_delta", "message_delta"):
            self._consume_heartbeat(out)

        if etype == "content_block_start":
            block = event.get("content_block") or {}
            btype = block.get("type")
            if btype == "thinking":
                out.write(open_style(out, "dim") + "[thinking: ")
                self.open_block = "thinking"
                self.streamed_thinking = True
            elif btype == "text":
                out.write("\n")
                self.open_block = "text"
                self.streamed_text = True
            elif btype == "tool_use":
                name = block.get("name") or "tool"
                self._tool_use_name = name
                self._tool_use_bytes = 0
                self.open_block = "tool_use"
                out.write(
                    open_style(out, "dim yellow")
                    + f"[writing {name} input · 0 bytes"
                )
                flush(out)

        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "thinking_delta" and self.open_block == "thinking":
                text = delta.get("thinking", "") or ""
                if text:
                    out.write(text.replace("\n", " "))
                    flush(out)
            elif dtype == "text_delta" and self.open_block == "text":
                out.write(delta.get("text", "") or "")
                flush(out)
            elif dtype == "input_json_delta" and self.open_block == "tool_use":
                partial = delta.get("partial_json") or ""
                self._tool_use_bytes += len(partial)
                out.write(
                    "\r\033[K"
                    + open_style(out, "dim yellow")
                    + f"[writing {self._tool_use_name} input · "
                    + format_bytes(self._tool_use_bytes)
                )
                flush(out)

        elif etype == "content_block_stop":
            if self.open_block == "thinking":
                out.write("]" + close_style(out, "dim") + "\n")
            elif self.open_block == "text":
                out.write("\n")
            elif self.open_block == "tool_use":
                out.write(
                    "\r\033[K"
                    + styled(
                        out,
                        f"[wrote {self._tool_use_name} input · "
                        f"{format_bytes(self._tool_use_bytes)}]",
                        "cyan",
                    )
                    + "\n"
                )
                self._tool_use_name = None
                self._tool_use_bytes = 0
            self.open_block = None

    def close_open_block(self, out: Any) -> None:
        """Force-close any in-flight open block (e.g. before snapshot render)."""
        if self.open_block == "thinking":
            out.write("]" + close_style(out, "dim") + "\n")
        elif self.open_block == "text":
            out.write("\n")
        elif self.open_block == "tool_use":
            out.write(
                "\r\033[K"
                + styled(
                    out,
                    f"[wrote {self._tool_use_name} input · "
                    f"{format_bytes(self._tool_use_bytes)}]",
                    "cyan",
                )
                + "\n"
            )
            self._tool_use_name = None
            self._tool_use_bytes = 0
        self.open_block = None

    def reset_for_next_assistant_turn(self) -> None:
        """Snapshot rendered → flags reset so the next assistant turn (within
        the same SDK call, if it tool-uses then text-replies) is unambiguous."""
        self.streamed_text = False
        self.streamed_thinking = False
        self.open_block = None
        self._heartbeat_count = 0
        self._heartbeat_started_inline = False
        self._tool_use_name = None
        self._tool_use_bytes = 0

    def flush_pending_tool_uses(self, out: Any) -> None:
        """Emit any deferred tool_use lines whose tool_result never arrived."""
        for pending in self.pending_tool_uses.values():
            name = pending["name"]
            summary = pending["summary"]
            line = f"[→ {name}{' ' + summary if summary else ''}]"
            out.write(styled(out, line, "bold cyan") + "\n")
        self.pending_tool_uses.clear()

    def heartbeat(self, out: Any) -> None:
        """Called when the SDK has been silent past `idle_timeout` seconds."""
        self._heartbeat_count += 1
        elapsed = self._heartbeat_count * 5
        if self.open_block == "tool_use":
            return
        if self.open_block is not None:
            out.write("·")
            flush(out)
            return
        if not self._heartbeat_started_inline:
            out.write(open_style(out, "dim") + f"[…still working · {elapsed}s")
            self._heartbeat_started_inline = True
        else:
            out.write(f" · {elapsed}s")
        flush(out)

    def _consume_heartbeat(self, out: Any) -> None:
        if self._heartbeat_started_inline:
            out.write("]" + close_style(out, "dim") + "\n")
            self._heartbeat_started_inline = False
            self._heartbeat_count = 0
