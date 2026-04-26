"""Renderer + format helpers for claude-agent-sdk message streams.

Surface-agnostic — `render_message` writes to any object with `write(str)` /
`flush()` / `isatty()`. The Textual REPL passes RichLog-backed sinks; print
mode passes stdout; the workflow runner doesn't render at all (it consumes
events.py instead).
"""

from __future__ import annotations

import asyncio
from io import StringIO
from typing import Any

from agentwire.sdk.errors import classify as _classify_sdk_error


# Sentinel yielded by _heartbeat_iter when the inner async iter has stalled
# past idle_timeout. The render loop watches for this and prints a heartbeat.
HEARTBEAT = object()


async def heartbeat_iter(async_iter, idle_timeout: float):
    """Yield items from `async_iter`; yield `HEARTBEAT` when idle.

    `client.receive_response()` is an async iterator that may sit silent for
    tens of seconds while the model thinks or writes a long tool input.
    On each idle timeout we yield `HEARTBEAT` so the renderer can show
    progress, but we keep the underlying `__anext__` task pending — using
    `asyncio.wait_for` would cancel it and lose the pending event.
    """
    iterator = async_iter.__aiter__()
    pending: asyncio.Task | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=idle_timeout)
            if pending in done:
                try:
                    yield pending.result()
                except StopAsyncIteration:
                    pending = None
                    return
                pending = None
            else:
                yield HEARTBEAT
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


def flush(out: Any) -> None:
    """Best-effort flush — StringIO has flush, real stdout has flush, fakes may not."""
    f = getattr(out, "flush", None)
    if callable(f):
        f()


# --- Color / style ----------------------------------------------------------
#
# Visual hierarchy goal: the user's eye should land on the assistant's actual
# answer first; everything bracketed is metadata and should recede. We use
# Rich for styling because:
#   1. It's the rendering layer Textual is built on, so the markup we write
#      here translates 1:1 to RichLog content.
#   2. It handles TTY detection, NO_COLOR, color-system fallback, etc., so we
#      don't have to.
#
# Style scheme (Rich style strings — translate to Textual identically):
#   thinking      → dim                (secondary; reasoning noise)
#   tool_progress → dim yellow         (in-flight; "byte counter ticking")
#   tool_done     → cyan               (closed; "wrote N KB")
#   tool_call     → bold cyan          ([→ Tool args] — the action)
#   tool_result   → green              ([← result: ...])
#   heartbeat     → dim                ([…still working · 5s])
#   agent_meta    → dim cyan           ([agent started · ...])
#   done          → dim green          ([done · tok · cost · time])
#   error         → bold red           ([error · ...])
#
# Assistant text + thinking content stay uncolored — default fg is the
# brightest, which is what we want for the actual reading material.

try:
    from rich.console import Console as _RichConsole

    _STYLE_BUF = StringIO()
    _STYLE_CONSOLE = _RichConsole(
        file=_STYLE_BUF,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        markup=True,
        emoji=False,
        soft_wrap=True,
    )
    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover — rich is a required dep but stay safe
    _RICH_AVAILABLE = False


_STYLE_CACHE: dict[str, tuple[str, str]] = {}


def _ansi_pair(style: str) -> tuple[str, str]:
    """Return `(open, close)` ANSI sequences for a Rich style string."""
    if not _RICH_AVAILABLE or not style:
        return ("", "")
    cached = _STYLE_CACHE.get(style)
    if cached is not None:
        return cached
    _STYLE_BUF.seek(0)
    _STYLE_BUF.truncate()
    # Sentinel \x00 splits open codes from close codes in Rich's output.
    _STYLE_CONSOLE.print(f"[{style}]\x00[/{style}]", end="")
    raw = _STYLE_BUF.getvalue()
    open_, _sep, close = raw.partition("\x00")
    pair = (open_, close)
    _STYLE_CACHE[style] = pair
    return pair


def styled(out: Any, text: str, style: str) -> str:
    """Wrap `text` in ANSI codes for `style` if `out` is a TTY, else plain."""
    if not style:
        return text
    if not getattr(out, "isatty", lambda: False)():
        return text
    open_, close = _ansi_pair(style)
    return f"{open_}{text}{close}"


def open_style(out: Any, style: str) -> str:
    """Open ANSI for `style` if `out` is a TTY (used to span multiple writes)."""
    if not style or not getattr(out, "isatty", lambda: False)():
        return ""
    return _ansi_pair(style)[0]


def close_style(out: Any, style: str) -> str:
    """Close ANSI for `style` (matches `open_style`)."""
    if not style or not getattr(out, "isatty", lambda: False)():
        return ""
    return _ansi_pair(style)[1]


def format_bytes(n: int) -> str:
    """Human-readable byte count: `42 bytes`, `1.2 KB`, `3.4 MB`."""
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def block_type(block: Any) -> str:
    if hasattr(block, "type"):
        return getattr(block, "type") or ""
    if isinstance(block, dict):
        return block.get("type", "")
    return {
        "TextBlock": "text",
        "ToolUseBlock": "tool_use",
        "ThinkingBlock": "thinking",
        "ToolResultBlock": "tool_result",
    }.get(type(block).__name__, "")


def block_attr(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def format_tool_input(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Write", "Edit"):
        fp = inp.get("file_path", "")
        if fp:
            return str(fp)
    if name == "Bash":
        cmd = inp.get("command", "") or ""
        if cmd:
            return cmd if len(cmd) <= 80 else cmd[:77] + "..."
    if name in ("Grep", "Glob"):
        return str(inp.get("pattern", "") or "")
    if name == "WebFetch":
        return str(inp.get("url", "") or "")
    if name == "WebSearch":
        return str(inp.get("query", "") or "")
    rendered = str(inp)
    return rendered if len(rendered) <= 80 else rendered[:77] + "..."


def format_tool_result(content: Any) -> str:
    if content is None:
        return "(no content)"
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = ""
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                text = b.get("text", "") or ""
                break
            if hasattr(b, "text"):
                text = str(getattr(b, "text", "") or "")
                break
        if not text:
            text = str(content)
    else:
        text = str(content)
    text = text.replace("\n", " ")
    return text if len(text) <= 120 else text[:117] + "..."


def render_message(
    message: Any,
    AssistantMessage: Any,
    UserMessage: Any,
    SystemMessage: Any,
    ResultMessage: Any,
    out: Any,
    stream_state: Any = None,
    action_out: Any = None,
) -> None:
    """Render one SDK message to the terminal.

    Compact, human-readable output matching the spirit of pi's JSONL but for
    a terminal reader. Tools like `Read`/`Bash` show the target; tool results
    show a one-line preview.

    `stream_state` carries cross-message bookkeeping for the partial-message
    stream: when partial events have already streamed text/thinking content
    for an assistant turn, the final AssistantMessage that arrives at
    message_stop would otherwise re-render everything. We track which
    content indices were streamed and skip them.

    `action_out`: optional separate sink for partial-stream events. The
    Textual REPL passes a dedicated CurrentAction RichLog here so live
    thinking, byte counters, and heartbeats render in their own docked
    subpane while finalized snapshot messages go to the chat pane via `out`.
    When `action_out` is None or the same object as `out`, the legacy
    behavior is preserved: everything goes to one sink.
    """
    if action_out is None:
        action_out = out
    dual_panes = action_out is not out

    # StreamEvent is a dataclass with `event`/`uuid`. Render thinking/text/
    # tool-input deltas inline so users see real-time progress. Detection
    # by duck-type to keep render_message decoupled from the SDK class.
    if hasattr(message, "event") and hasattr(message, "uuid") and not hasattr(message, "content"):
        if stream_state is not None:
            payload = getattr(message, "event", None)
            if isinstance(payload, dict):
                stream_state.handle_partial(payload, action_out)
        return

    # Any non-partial message arriving — close out a pending heartbeat line.
    if stream_state is not None:
        stream_state._consume_heartbeat(action_out)

    if isinstance(message, SystemMessage):
        if getattr(message, "subtype", None) == "init":
            data = getattr(message, "data", {}) or {}
            model = data.get("model", "") or ""
            sid = (data.get("session_id") or data.get("sessionId") or "")[:8]
            parts = [p for p in [model, f"session {sid}" if sid else ""] if p]
            out.write(styled(out, f"[agent started · {' · '.join(parts)}]", "dim cyan") + "\n")
        return

    if isinstance(message, AssistantMessage):
        if stream_state is not None and stream_state.partials_active:
            stream_state.close_open_block(action_out)
        for block in getattr(message, "content", []) or []:
            btype = block_type(block)
            if btype == "text":
                if (
                    not dual_panes
                    and stream_state is not None
                    and stream_state.streamed_text
                ):
                    continue
                text = block_attr(block, "text", "") or ""
                if text:
                    out.write(text)
                    if not text.endswith("\n"):
                        out.write("\n")
            elif btype == "tool_use":
                name = block_attr(block, "name", "") or ""
                tool_id = block_attr(block, "id", "") or ""
                inp = block_attr(block, "input", {}) or {}
                summary = format_tool_input(name, inp)
                if stream_state is not None and tool_id:
                    stream_state.pending_tool_uses[tool_id] = {
                        "name": name,
                        "summary": summary,
                    }
                else:
                    line = f"[→ {name}{' ' + summary if summary else ''}]"
                    out.write(styled(out, line, "bold cyan") + "\n")
            elif btype == "thinking":
                if (
                    not dual_panes
                    and stream_state is not None
                    and stream_state.streamed_thinking
                ):
                    continue
                thinking = block_attr(block, "thinking", "") or ""
                first = thinking.split("\n", 1)[0].strip()
                if first:
                    preview = first if len(first) <= 80 else first[:77] + "..."
                    out.write(styled(out, f"[thinking: {preview}]", "dim") + "\n")
        if stream_state is not None:
            stream_state.reset_for_next_assistant_turn()
        return

    if isinstance(message, UserMessage):
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                btype = block_type(block)
                if btype == "tool_result":
                    result_content = block_attr(block, "content", None)
                    preview = format_tool_result(result_content)
                    is_err = bool(block_attr(block, "is_error", False))
                    tool_use_id = block_attr(block, "tool_use_id", "") or ""

                    pending = None
                    if stream_state is not None and tool_use_id:
                        pending = stream_state.pending_tool_uses.pop(
                            tool_use_id, None
                        )

                    if pending is not None and not is_err:
                        name = pending["name"]
                        summary = pending["summary"]
                        parts = [name]
                        if summary:
                            parts.append(summary)
                        if preview:
                            parts.append(preview)
                        line = f"[{' · '.join(parts)}]"
                        out.write(styled(out, line, "cyan") + "\n")
                    elif pending is not None and is_err:
                        name = pending["name"]
                        summary = pending["summary"]
                        call = f"[→ {name}{' ' + summary if summary else ''}]"
                        out.write(styled(out, call, "bold cyan") + "\n")
                        out.write(styled(out, f"[← error: {preview}]", "red") + "\n")
                    else:
                        style = "red" if is_err else "green"
                        label = "error" if is_err else "result"
                        out.write(
                            styled(out, f"[← {label}: {preview}]", style) + "\n"
                        )
        return

    if isinstance(message, ResultMessage):
        if stream_state is not None and stream_state.pending_tool_uses:
            stream_state.flush_pending_tool_uses(out)
        usage = getattr(message, "usage", {}) or {}
        cost = getattr(message, "total_cost_usd", None)
        duration = getattr(message, "duration_ms", None)
        parts: list[str] = []
        if isinstance(usage, dict):
            in_tok = usage.get("input_tokens", 0) or 0
            out_tok = usage.get("output_tokens", 0) or 0
            if in_tok or out_tok:
                parts.append(f"{in_tok}+{out_tok} tok")
        if cost is not None:
            parts.append(f"${cost:.4f}")
        if duration is not None:
            parts.append(f"{duration / 1000:.1f}s")
        suffix = f" · {' · '.join(parts)}" if parts else ""
        if getattr(message, "is_error", False):
            err = getattr(message, "result", None) or "unknown error"
            category = _classify_sdk_error("ResultMessage", str(err))
            out.write(styled(out, f"[error · {category}{suffix}] {err}", "bold red") + "\n")
        else:
            out.write(styled(out, f"[done{suffix}]", "dim green") + "\n")
        return
