"""Sink protocol — the duck-typed write target for SDK message rendering.

Every sink implements `write(str)`, `flush()`, `isatty() -> bool`. Some
also implement `clear()` for live-update sinks (e.g., the action pane in
the Textual REPL re-paints on each delta). The protocol is intentionally
minimal: it matches stdout/StringIO/RichLog-wrapper duck-types so any of
them can be passed to `render_message` interchangeably.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Sink(Protocol):
    """Minimal write target for SDK rendering.

    Anything that satisfies write/flush/isatty can be a sink: stdout,
    StringIO, a wrapper around a Textual RichLog, a websocket pusher.
    `clear()` is optional and only meaningful for live-update sinks
    (action pane, single-line status row).
    """

    def write(self, s: str) -> int | None: ...
    def flush(self) -> None: ...
    def isatty(self) -> bool: ...
