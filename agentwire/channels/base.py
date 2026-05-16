"""Channel base classes and registry for AgentWire (outbound-only)."""

from dataclasses import dataclass


class NotificationError(Exception):
    """Base exception for channel/notification errors."""

    pass


@dataclass
class ChannelResult:
    """Result of a channel send operation."""

    success: bool
    message_id: str | int | None = None
    error: str | None = None


class ChannelRegistry:
    """Registry for channel classes.

    Channels register themselves via the @ChannelRegistry.register("name")
    decorator and expose their config under channels.{config_key}: in YAML.
    """

    _channels: dict[str, type] = {}

    @classmethod
    def register(cls, name: str):
        """Decorator to register a channel class."""

        def decorator(channel_cls):
            cls._channels[name] = channel_cls
            return channel_cls

        return decorator

    @classmethod
    def get(cls, name: str):
        """Get a registered channel class by name."""
        return cls._channels.get(name)

    @classmethod
    def all(cls) -> dict[str, type]:
        """Return all registered channels."""
        return dict(cls._channels)

    @classmethod
    def resolve_config(cls, name: str, data: dict) -> dict:
        """Resolve config for a channel from YAML data dict.

        Reads channels.{config_key} for the given channel. Returns an empty
        dict if the channel is not registered or has no config section.
        """
        channel_cls = cls._channels.get(name)
        if not channel_cls:
            return {}

        config_key = getattr(channel_cls, "config_key", name)
        return data.get("channels", {}).get(config_key, {}) or {}


class Channel:
    """Base class for all channels."""

    name: str = ""
    channel_type: str = ""
    config_class = None
    config_key: str = ""

    def __init__(self, config=None):
        self.config = config


class SendOnlyChannel(Channel):
    """Stateless outbound-only channel."""

    channel_type = "send_only"

    async def send(self, text: str, **kwargs) -> ChannelResult:
        """Send a message through this channel."""
        raise NotImplementedError
