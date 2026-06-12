"""Quo (formerly OpenPhone) channel — send-only SMS via Quo API.

No external dependencies — uses urllib from stdlib.
API docs: https://www.quo.com/docs/mdx/api-reference/messages/send-a-text-message
"""

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from .base import (
    ChannelRegistry,
    ChannelResult,
    NotificationError,
    SendOnlyChannel,
)


class QuoConfigError(NotificationError):
    """Raised when Quo configuration is missing or invalid."""

    pass


@dataclass
class QuoConfig:
    """Quo (OpenPhone) SMS configuration.

    The API key is env-only: QUO_API_KEY (or legacy OPENPHONE_API_KEY),
    normally a line in ~/.agentwire/.env (docs/wiki/security/secrets.md).
    It is never read from config.yaml.
    """

    from_number: str = ""  # E.164 format (+1...) or phone number ID (PNxxx)
    default_to: str = ""
    api_key: str = field(init=False, default="")

    def __post_init__(self):
        self.api_key = os.environ.get("QUO_API_KEY", "") or os.environ.get("OPENPHONE_API_KEY", "")


API_URL = "https://api.openphone.com/v1/messages"


def _get_quo_config() -> QuoConfig:
    """Get Quo config from channels registry."""
    from agentwire.config import get_config

    config = get_config()
    quo_config = config.channels.get("quo")
    if quo_config:
        return quo_config
    return QuoConfig()


def send_quo_sms(
    body: str,
    to: Optional[str] = None,
    from_number: Optional[str] = None,
) -> ChannelResult:
    """Send an SMS via Quo (OpenPhone) API.

    Args:
        body: Message text (1-1600 chars).
        to: Recipient phone number in +E.164 format.
        from_number: Sender phone number or ID. Uses config if not specified.

    Returns:
        ChannelResult with success status.
    """
    quo_config = _get_quo_config()

    if not quo_config.api_key:
        raise QuoConfigError(
            "Quo API key not configured. "
            "Add QUO_API_KEY=... to ~/.agentwire/.env "
            "(see docs/wiki/security/secrets.md)"
        )

    recipient = to or quo_config.default_to
    if not recipient:
        raise QuoConfigError(
            "No recipient specified and no default_to configured in channels.quo"
        )

    sender = from_number or quo_config.from_number
    if not sender:
        raise QuoConfigError(
            "No from_number configured in channels.quo"
        )

    payload = {
        "content": body[:1600],
        "from": sender,
        "to": [recipient],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": quo_config.api_key,
            "Content-Type": "application/json",
            "User-Agent": "agentwire",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            msg_id = result.get("data", {}).get("id", "")
            return ChannelResult(success=True, message_id=msg_id)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:300] if e.fp else ""
        # Parse error details if available
        try:
            err = json.loads(body_text)
            error_msg = err.get("message", "") or err.get("error", "")
            error_code = err.get("code", "")
            detail = f"HTTP {e.code}: {error_msg}"
            if error_code:
                detail += f" (code: {error_code})"
        except Exception:
            detail = f"HTTP {e.code}: {body_text}"
        return ChannelResult(success=False, error=detail)
    except Exception as e:
        return ChannelResult(success=False, error=str(e))


def cmd_quo(args) -> int:
    """CLI handler for quo command."""
    body = args.body
    if not body and not sys.stdin.isatty():
        body = sys.stdin.read()

    if not body:
        print("Error: No message body provided. Use --body or pipe content.", file=sys.stderr)
        return 1

    try:
        result = send_quo_sms(body=body, to=getattr(args, "to", None))

        if result.success:
            if not getattr(args, "quiet", False):
                print(f"Quo SMS sent (id: {result.message_id})")
            return 0
        else:
            print(f"Error sending Quo SMS: {result.error}", file=sys.stderr)
            return 1

    except QuoConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


@ChannelRegistry.register("quo")
class QuoChannel(SendOnlyChannel):
    """Quo (OpenPhone) SMS send-only channel.

    Uses the Quo REST API directly — no external dependencies.
    """

    name = "quo"
    config_class = QuoConfig
    config_key = "quo"

    async def send(self, text: str, **kwargs) -> ChannelResult:
        return send_quo_sms(body=text, to=kwargs.get("to"))
