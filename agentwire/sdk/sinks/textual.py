"""Textual-RichLog-backed sinks for SDK rendering.

`RichLogSink` adapts `render_message`'s ANSI write stream into a Textual
`RichLog` widget. Long lines are wrapped manually with continuation
markers (`| ` for top-level bullets, `  | ` for indented children) so
wrapped content stays visually connected to its parent bullet — RichLog's
built-in wrap puts continuation lines flush-left with no indent, which
made it impossible to tell where one bullet ended and the next began.
"""

from __future__ import annotations

import re

from rich.text import Text
from textual.widgets import RichLog


# Match contiguous CSI/SGR ANSI sequences (e.g. "\x1b[2m", "\x1b[1;36m\x1b[0m").
_ANSI_RUN_RE = re.compile(r"(?:\x1b\[[0-9;]*m)+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _split_ansi(text: str) -> tuple[str, str, str]:
    """Peel off leading + trailing ANSI runs around the visible content.

    Returns `(open_codes, plain_body, close_codes)`. Lines emitted by our
    renderer are uniformly styled (open + content + close), so this
    captures the styling we need to re-apply to each wrapped chunk.
    """
    open_match = _ANSI_RUN_RE.match(text)
    open_codes = open_match.group(0) if open_match else ""

    # Trailing ANSI: take the last run if it ends at the very end of `text`.
    close_codes = ""
    matches = list(_ANSI_RUN_RE.finditer(text))
    if matches and matches[-1].end() == len(text):
        close_codes = matches[-1].group(0)

    body = text
    if open_codes:
        body = body[len(open_codes):]
    if close_codes:
        body = body[: len(body) - len(close_codes)]
    return open_codes, body, close_codes


def wrap_with_continuation(text: str, width: int) -> list[str]:
    """Wrap `text` to `width` columns with bullet-aware continuation markers.

    Top-level bullets (`- ...`) wrap to `| ...` continuations. Indented
    children (`  · ...`) wrap to `  | ...`. Plain text wraps to a `  `
    hang. ANSI styling on the source line is re-applied to each chunk.

    `width` is the visible character cell count available; if zero or
    too small to fit a continuation marker we return the line unchanged.
    """
    if width < 6:
        return [text]
    open_codes, body, close_codes = _split_ansi(text)
    if len(body) <= width:
        return [text]

    if body.startswith("- "):
        cont_prefix = "| "
    elif body.startswith("  · "):
        cont_prefix = "  | "
    else:
        cont_prefix = "  "

    chunks: list[str] = []
    remaining = body
    first = True
    while len(remaining) > width:
        avail = width if first else width - len(cont_prefix)
        if avail <= 0:
            break
        # Prefer a space break in the back half of the window so we don't
        # split words mid-character on long URLs / paths. If no good break
        # exists, hard-cut at `avail`.
        space_idx = remaining.rfind(" ", 0, avail + 1)
        if space_idx > avail // 2:
            split_at = space_idx
            consume_space = True
        else:
            split_at = avail
            consume_space = False
        chunk_body = remaining[:split_at].rstrip()
        prefix = "" if first else cont_prefix
        chunks.append(open_codes + prefix + chunk_body + close_codes)
        remaining = remaining[split_at + (1 if consume_space else 0):]
        first = False

    if remaining:
        prefix = "" if first else cont_prefix
        chunks.append(open_codes + prefix + remaining + close_codes)

    return chunks


class RichLogSink:
    """Adapter so `render_message(out=...)` can target a `RichLog`.

    Buffers writes until newline, then emits each line — wrapping long
    lines to the widget's content width with continuation markers.
    """

    def __init__(self, log: RichLog) -> None:
        self._log = log
        self._buf = ""

    def write(self, s: str) -> None:
        if not s:
            return
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)

    def flush(self) -> None:
        if self._buf:
            self._emit(self._buf)
            self._buf = ""

    def isatty(self) -> bool:
        return True

    def _content_width(self) -> int:
        try:
            return max(0, self._log.content_size.width)
        except Exception:
            return 0

    def _emit(self, text: str) -> None:
        if text == "":
            self._log.write("")
            return
        width = self._content_width()
        if not width:
            self._log.write(Text.from_ansi(text))
            return
        for chunk in wrap_with_continuation(text, width):
            self._log.write(Text.from_ansi(chunk))
