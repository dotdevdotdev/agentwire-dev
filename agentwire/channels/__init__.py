"""AgentWire channels — outbound-only notification integrations."""

# Auto-register built-in channels
from . import (
    email,  # noqa: F401
    push,  # noqa: F401
    quo,  # noqa: F401
)
from .base import (
    Channel,
    ChannelRegistry,
    ChannelResult,
    NotificationError,
    SendOnlyChannel,
)

__all__ = [
    "Channel",
    "ChannelRegistry",
    "ChannelResult",
    "NotificationError",
    "SendOnlyChannel",
]
