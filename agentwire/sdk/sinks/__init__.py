"""Sinks: where SDK event streams get rendered.

A sink is anything that satisfies the `Sink` protocol — a duck-typed
write/flush/isatty interface plus an optional `clear()`. Concrete
implementations live alongside (textual.py, jsonl.py, ...). Composite
views compose multiple sinks: one column = one sink.
"""

from agentwire.sdk.sinks.base import Sink

__all__ = ["Sink"]
