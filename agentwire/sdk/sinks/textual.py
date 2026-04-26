"""Textual-RichLog-backed sinks for SDK rendering.

`RichLogSink` adapts `render_message`'s ANSI write stream into a Textual
`RichLog` widget (append-only, line-at-a-time). `ActionSink` does the same
but supports live in-place updates — re-paints the whole widget on every
delta — used by the CurrentAction pane where the byte counter ticks live
and thinking text streams character by character.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import RichLog


class RichLogSink:
    """Adapter so `render_message(out=...)` can target a `RichLog`.

    The renderer writes ANSI-bearing strings; we buffer until newline, then
    emit each line as `Text.from_ansi(line)` so Rich parses styles faithfully.

    `\\r\\033[K` in the input is the byte-counter "discard previous in-flight
    line" signal — we honor it as "drop everything in the buffer up to and
    including the last reset", which collapses repeated counter ticks to a
    single visible line on close.
    """

    _CR_CLEAR = "\r\x1b[K"

    def __init__(self, log: RichLog) -> None:
        self._log = log
        self._buf = ""

    def write(self, s: str) -> None:
        if not s:
            return
        if self._CR_CLEAR in s:
            # Discard any in-flight buffer content + everything in `s` before
            # the last reset — the byte counter is announcing a fresh value.
            self._buf = ""
            s = s.rsplit(self._CR_CLEAR, 1)[1]
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


class ActionSink:
    """Streaming sink for the CurrentAction subpane.

    Unlike `RichLogSink` (chat — append-only, buffer-until-newline), the
    action pane shows live-updating content. We maintain our own list of
    "finalized" lines plus a buffer for the in-flight current line, and
    rewrite the entire RichLog on each update via clear()+write loop.

    For an action pane with O(20) lines and a turn with O(1000) deltas,
    the cost is ~20K writes — well within Textual's render budget.

    `clear()` is called from the app on each ResultMessage (turn complete)
    so the next turn's partials start with a clean pane.
    """

    _CR_CLEAR = "\r\x1b[K"

    def __init__(self, log: RichLog) -> None:
        self._log = log
        self._finalized: list[str] = []
        self._current = ""

    def write(self, s: str) -> None:
        if not s:
            return
        if self._CR_CLEAR in s:
            s = s.rsplit(self._CR_CLEAR, 1)[1]
            self._current = ""
        self._current += s
        while "\n" in self._current:
            line, self._current = self._current.split("\n", 1)
            self._finalized.append(line)
        self._refresh()

    def flush(self) -> None:
        self._refresh()

    def isatty(self) -> bool:
        return True

    def clear(self) -> None:
        """Wipe the pane — called at end of every turn (ResultMessage)."""
        self._finalized.clear()
        self._current = ""
        try:
            self._log.clear()
        except AttributeError:
            pass

    def _refresh(self) -> None:
        try:
            self._log.clear()
        except AttributeError:
            return
        for line in self._finalized:
            if line == "":
                self._log.write("")
            else:
                self._log.write(Text.from_ansi(line))
        if self._current:
            self._log.write(Text.from_ansi(self._current))
