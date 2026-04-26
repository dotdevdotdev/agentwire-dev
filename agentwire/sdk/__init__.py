"""Agentwire SDK primitives.

Reusable building blocks that wrap claude-agent-sdk for both interactive
(Textual REPL) and headless (workflow runner) consumers. See
docs/missions/agentwire-sdk-primitives.md for the architecture.
"""

from agentwire.sdk.client import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    DEFAULT_THINKING_MODE,
    FULL_TOOLS,
    PERMISSION_MODE_MAP,
    RESTRICTED_TOOLS,
    build_options,
    thinking_config,
)
from agentwire.sdk.errors import classify
from agentwire.sdk.render import (
    HEARTBEAT,
    heartbeat_iter,
    render_message,
)
from agentwire.sdk.sinks.base import Sink
from agentwire.sdk.state import StreamRenderState

__all__ = [
    "DEFAULT_EFFORT",
    "DEFAULT_MODEL",
    "DEFAULT_THINKING_MODE",
    "FULL_TOOLS",
    "HEARTBEAT",
    "PERMISSION_MODE_MAP",
    "RESTRICTED_TOOLS",
    "Sink",
    "StreamRenderState",
    "build_options",
    "classify",
    "heartbeat_iter",
    "render_message",
    "thinking_config",
]
