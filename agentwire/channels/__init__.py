"""AgentWire channels — outbound-only notification integrations."""

from .base import (
    Channel,
    ChannelRegistry,
    ChannelResult,
    NotificationError,
    SendOnlyChannel,
)

# Auto-register built-in channels
from . import email  # noqa: F401
from . import quo  # noqa: F401

__all__ = [
    "Channel",
    "ChannelRegistry",
    "ChannelResult",
    "NotificationError",
    "SendOnlyChannel",
]
