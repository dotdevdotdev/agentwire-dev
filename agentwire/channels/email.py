"""Email channel — send-only via Resend API.

Supports branded HTML templates with markdown body, attachments,
and configurable branding images.
"""

import base64
import os
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import resend
except ImportError:
    resend = None

from .base import (
    ChannelRegistry,
    ChannelResult,
    NotificationError,
    SendOnlyChannel,
)


class EmailConfigError(NotificationError):
    """Raised when email configuration is missing or invalid."""

    pass


@dataclass
class EmailConfig:
    """Email notification configuration (Resend).

    The API key is env-only: RESEND_API_KEY, normally a line in
    ~/.agentwire/.env (docs/wiki/security/secrets.md). It is never read
    from config.yaml.
    """

    from_address: str = ""
    default_to: str = ""
    banner_image_url: str = ""
    echo_image_url: str = ""
    echo_small_url: str = ""
    logo_image_url: str = ""
    api_key: str = field(init=False, default="")

    def __post_init__(self):
        self.api_key = os.environ.get("RESEND_API_KEY", "")


# Playful greetings from Echo
GREETINGS = [
    "Hey there! 👋",
    "Psst! Got something for you...",
    "Hoot hoot! 🦉",
    "Quick update for you!",
    "Echo here with news...",
    "Hey! Just popping in...",
    "Got a moment? Here's an update!",
    "Fresh from the wire! ⚡",
]


@dataclass
class Attachment:
    """Email attachment."""

    filename: str
    content: bytes  # Raw bytes
    content_type: Optional[str] = None


@dataclass
class EmailResult:
    """Result of sending an email."""

    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


def _is_html_content(text: str) -> bool:
    """Check if text appears to be HTML content."""
    if not text:
        return False

    html_patterns = [
        r'<(h[1-6]|p|div|span|table|tr|td|th|ul|ol|li|a|strong|em|br|hr)\b',
        r'<[a-zA-Z][^>]*style\s*=',
        r'<!DOCTYPE',
        r'<html',
    ]

    for pattern in html_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True

    return False


def _markdown_to_html(text: str) -> str:
    """Convert markdown to HTML. Returns unchanged if already HTML."""
    if not text:
        return ""

    if _is_html_content(text):
        return text

    import markdown

    return markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "nl2br"],
    )


def _render_email_template(
    subject: str,
    body: str,
    attachments: Optional[list[Attachment]] = None,
    greeting: Optional[str] = None,
    banner_image_url: Optional[str] = None,
    echo_image_url: Optional[str] = None,
    echo_small_url: Optional[str] = None,
    logo_image_url: Optional[str] = None,
) -> str:
    """Render the branded email HTML template."""
    try:
        from jinja2 import Environment, PackageLoader, select_autoescape
    except ImportError:
        return f"<p>{body}</p>"  # Fallback if jinja2 not installed

    env = Environment(
        loader=PackageLoader("agentwire", "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )

    template = env.get_template("email_notification.html")

    body_html = _markdown_to_html(body)

    if greeting is None:
        greeting = random.choice(GREETINGS)

    return template.render(
        subject=subject,
        body_html=body_html,
        greeting=greeting,
        attachments=attachments or [],
        banner_image_url=banner_image_url,
        echo_image_url=echo_image_url,
        echo_small_url=echo_small_url,
        logo_image_url=logo_image_url,
    )


def _get_email_config() -> EmailConfig:
    """Get email config from channels registry or legacy path."""
    from agentwire.config import get_config

    config = get_config()
    email_config = config.channels.get("email")
    if email_config:
        return email_config
    return EmailConfig()


def _normalize_recipients(to: Optional[str | list[str]], default_to: str) -> list[str]:
    """Flatten `to` (str | list[str]) into a deduped list, splitting on commas.

    Accepts: None, "a@x.com", "a@x.com,b@y.com", ["a@x.com"], ["a,b", "c"].
    Falls back to `default_to` (also comma-split) when `to` is None/empty.
    Preserves order, drops duplicates and empty entries.
    """
    raw: list[str] = []
    if not to:
        raw = [default_to] if default_to else []
    elif isinstance(to, str):
        raw = [to]
    else:
        raw = list(to)

    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        for part in str(entry).split(","):
            addr = part.strip()
            if addr and addr not in seen:
                seen.add(addr)
                out.append(addr)
    return out


def send_email(
    to: Optional[str | list[str]] = None,
    subject: str = "",
    body: str = "",
    attachments: Optional[list[Path | str]] = None,
    from_address: Optional[str] = None,
    greeting: Optional[str] = None,
    plain_text: bool = False,
) -> EmailResult:
    """Send a branded email via Resend.

    Args:
        to: Recipient email(s). Accepts a single address, a comma-separated
            string, or a list of either. Uses config default_to if not specified.
        subject: Email subject line.
        body: Email body (markdown supported, converted to HTML).
        attachments: List of file paths to attach.
        from_address: Sender address. Uses config from_address if not specified.
        greeting: Custom greeting. Random if not specified.
        plain_text: If True, send as plain text only (no HTML template).

    Returns:
        EmailResult with success status and message_id or error.

    Raises:
        EmailConfigError: If required configuration is missing.
    """
    email_config = _get_email_config()

    # Validate API key
    if not email_config.api_key:
        raise EmailConfigError(
            "Email API key not configured. "
            "Add RESEND_API_KEY=re_... to ~/.agentwire/.env "
            "(see docs/wiki/security/secrets.md)"
        )

    # Determine recipients (list form — Resend's `to` field accepts an array)
    recipients = _normalize_recipients(to, email_config.default_to)
    if not recipients:
        raise EmailConfigError(
            "No recipient specified and no default_to configured in "
            "channels.email.default_to"
        )

    # Determine sender
    sender = from_address or email_config.from_address
    if not sender:
        raise EmailConfigError(
            "No sender address configured. "
            "Set channels.email.from_address in ~/.agentwire/config.yaml"
        )

    # Configure Resend
    if resend is None:
        return EmailResult(success=False, error="resend package not installed. Install: pip install resend")

    resend.api_key = email_config.api_key

    # Process attachments
    attachment_objects: list[Attachment] = []
    resend_attachments = []

    if attachments:
        for attachment_path in attachments:
            path = Path(attachment_path)
            if not path.exists():
                return EmailResult(success=False, error=f"Attachment not found: {path}")

            content = path.read_bytes()
            attachment_objects.append(Attachment(filename=path.name, content=content))

            resend_attachments.append({
                "filename": path.name,
                "content": base64.b64encode(content).decode("utf-8"),
            })

    # Build email params
    email_subject = subject or "(no subject)"

    params: dict = {
        "from": sender,
        "to": recipients,
        "subject": email_subject,
    }

    if plain_text:
        params["text"] = body
    else:
        html_content = _render_email_template(
            subject=email_subject,
            body=body,
            attachments=attachment_objects,
            greeting=greeting,
            banner_image_url=email_config.banner_image_url or None,
            echo_image_url=email_config.echo_image_url or None,
            echo_small_url=email_config.echo_small_url or None,
            logo_image_url=email_config.logo_image_url or None,
        )
        params["html"] = html_content
        params["text"] = body

    if resend_attachments:
        params["attachments"] = resend_attachments

    # Send email
    try:
        response = resend.Emails.send(params)
        message_id = response.get("id") if isinstance(response, dict) else str(response)
        return EmailResult(success=True, message_id=message_id)
    except resend.exceptions.ResendError as e:
        return EmailResult(success=False, error=str(e))
    except Exception as e:
        return EmailResult(success=False, error=f"Unexpected error: {e}")


def cmd_email(args) -> int:
    """CLI handler for email command."""
    body = args.body
    if not body and not sys.stdin.isatty():
        body = sys.stdin.read()

    if not body:
        print("Error: No message body provided. Use --body or pipe content.", file=sys.stderr)
        return 1

    subject = args.subject or "[AgentWire] Notification"

    attachments = None
    if hasattr(args, "attach") and args.attach:
        attachments = args.attach if isinstance(args.attach, list) else [args.attach]

    try:
        result = send_email(
            to=args.to,
            subject=subject,
            body=body,
            attachments=attachments,
            plain_text=getattr(args, "plain", False),
        )

        if result.success:
            if not getattr(args, "quiet", False):
                print(f"Email sent (id: {result.message_id})")
            return 0
        else:
            print(f"Error sending email: {result.error}", file=sys.stderr)
            return 1

    except EmailConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


@ChannelRegistry.register("email")
class EmailChannel(SendOnlyChannel):
    """Email send-only channel via Resend API."""

    name = "email"
    config_class = EmailConfig
    config_key = "email"

    async def send(self, text: str, **kwargs) -> ChannelResult:
        result = send_email(
            body=text,
            to=kwargs.get("to"),
            subject=kwargs.get("subject"),
        )
        return ChannelResult(
            success=result.success,
            message_id=result.message_id,
            error=result.error,
        )
