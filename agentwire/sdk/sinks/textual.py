"""Textual-RichLog-backed sinks for SDK rendering.

`RichLogSink` adapts `render_message`'s ANSI write stream into a Textual
`RichLog` widget (append-only, line-at-a-time). After the bullet/indent
redesign, this is the only sink we need — the previous `ActionSink` for
live in-place updates is gone (the CurrentAction pane it served was
removed; everything streams into chat now).
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import RichLog


class RichLogSink:
    """Adapter so `render_message(out=...)` can target a `RichLog`.

    The renderer writes ANSI-bearing strings; we buffer until newline, then
    emit each line as `Text.from_ansi(line)` so Rich parses styles faithfully.
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

    def _emit(self, text: str) -> None:
        if text == "":
            self._log.write("")
        else:
            self._log.write(Text.from_ansi(text))
